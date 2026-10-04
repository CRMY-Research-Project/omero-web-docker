"""Per-file import orchestration for the web importer.

Imports staged files one at a time so a batch reports progress file by
file, and one bad file cannot take the rest of the batch down with it.
Pure (no OMERO or Django imports): the view hands in an ``import_one``
callable that performs the real OMERO import, so this logic unit-tests
offline.

Includes:
    import_files - run the batch, emitting per-file state + progress events.
    summarize    - roll per-file results up into ids, failures and a warning.
"""

import time

from .jobs import (FILE_DONE, FILE_FAILED, FILE_PROCESSING,
                   FILE_TRANSFERRING)

# Shown to the user for a failed file; the traceback goes to the server log.
FAILED_MESSAGE = ("Import failed on the server; ask an administrator to "
                  "check the OMERO.web logs.")


def import_files(files, import_one, on_event=None, logger=None,
                 min_interval=0.5):
    """
    Import each staged file in turn, isolating per-file failures.

    Steps:
        1. Mark the file transferring and call ``import_one``.
        2. Relay its byte progress (throttled), switching the file to
           processing once every byte has reached OMERO.
        3. Record success with the new image ids, or failure, then move on.

    Args:
        files (list): ``(name, path, size)`` per staged file, in order.
        import_one (callable): ``import_one(name, path, progress)`` imports
            one file and returns its new image ids, raising on failure; it
            calls ``progress(sent_bytes)`` as bytes reach OMERO.
        on_event (callable, optional): ``on_event(index, **fields)`` hears
            every state change and progress tick. Defaults to None.
        logger (logging.Logger, optional): Receives per-file tracebacks.
            Defaults to None.
        min_interval (float, optional): Minimum seconds between two
            progress-only events for one file; state changes always pass.
            Defaults to 0.5.

    Returns:
        list: One ``{"name", "image_ids", "error"}`` dict per file, with
            ``error`` None on success.
    """
    results = []
    for index, (name, path, size) in enumerate(files):
        _emit(on_event, index, state=FILE_TRANSFERRING, sent=0)
        progress = _progress_relay(on_event, index, size, min_interval)
        try:
            image_ids = list(import_one(name, path, progress))
        except Exception:
            if logger is not None:
                logger.exception("Import of staged file %s (%s) failed",
                                 index, name)
            results.append({"name": name, "image_ids": [],
                            "error": FAILED_MESSAGE})
            _emit(on_event, index, state=FILE_FAILED, error=FAILED_MESSAGE)
            continue
        results.append({"name": name, "image_ids": image_ids, "error": None})
        _emit(on_event, index, state=FILE_DONE, image_ids=image_ids,
              sent=size)
    return results


def _progress_relay(on_event, index, size, min_interval):
    """Build the ``progress(sent)`` callback for one file.

    Ticks closer together than ``min_interval`` are dropped (each event is
    a job-file write), except the final one that flips the file to
    processing - a client must never miss that transition.
    """
    last = [None]   # monotonic time of the last relayed tick

    def progress(sent):
        if size and sent >= size:
            _emit(on_event, index, state=FILE_PROCESSING, sent=sent)
            return
        now = time.monotonic()
        if last[0] is not None and now - last[0] < min_interval:
            return
        last[0] = now
        _emit(on_event, index, sent=sent)
    return progress


def _emit(on_event, index, **fields):
    if on_event is not None:
        on_event(index, **fields)


def summarize(results):
    """
    Roll per-file results up for the job / HTTP response.

    Args:
        results (list): Per-file dicts from :func:`import_files`.

    Returns:
        tuple: ``(image_ids, failed_names, warning)`` - every new image id,
            the names that failed, and a one-line warning when only *some*
            failed (None when all succeeded or all failed).
    """
    image_ids = [i for r in results for i in r["image_ids"]]
    failed = [r["name"] for r in results if r["error"]]
    warning = None
    if failed and len(failed) < len(results):
        warning = "%d of %d files failed to import: %s" % (
            len(failed), len(results), ", ".join(failed))
    return image_ids, failed, warning
