"""Unit tests for per-file import orchestration (pure - no OMERO)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omero_webimport import batch, jobs  # noqa: E402


def fake_importer(fail=(), chunks=(40, 100)):
    """An import_one that reports byte progress, then returns one image id
    per file (1000 + position) - or raises for names listed in ``fail``."""
    calls = []

    def import_one(name, path, progress):
        calls.append(name)
        for sent in chunks:
            progress(sent)
        if name in fail:
            raise RuntimeError("bad file")
        return [1000 + len(calls)]
    return import_one, calls


def collect():
    events = []
    return events, (lambda index, **fields: events.append((index, fields)))


def test_every_file_imports_in_order():
    import_one, calls = fake_importer()
    files = [("a.svs", "/s/0.part", 100), ("b.svs", "/s/1.part", 100),
             ("c.svs", "/s/2.part", 100)]
    results = batch.import_files(files, import_one, min_interval=0)
    assert calls == ["a.svs", "b.svs", "c.svs"]
    assert [r["image_ids"] for r in results] == [[1001], [1002], [1003]]
    assert all(r["error"] is None for r in results)


def test_one_failure_does_not_stop_the_rest():
    # the regression behind "only the first file imports": an exception on
    # one file must not abort the files after it
    import_one, calls = fake_importer(fail=("a.svs",))
    files = [("a.svs", "/s/0.part", 100), ("b.svs", "/s/1.part", 100)]
    results = batch.import_files(files, import_one, min_interval=0)
    assert calls == ["a.svs", "b.svs"]
    assert results[0]["error"] == batch.FAILED_MESSAGE
    assert results[0]["image_ids"] == []
    assert results[1]["error"] is None and results[1]["image_ids"]


def test_events_walk_each_file_through_its_states():
    import_one, _ = fake_importer(fail=("b.svs",))
    events, on_event = collect()
    batch.import_files([("a.svs", "p0", 100), ("b.svs", "p1", 100)],
                       import_one, on_event=on_event, min_interval=0)
    states = [(i, f["state"]) for i, f in events if "state" in f]
    assert states == [
        (0, jobs.FILE_TRANSFERRING), (0, jobs.FILE_PROCESSING),
        (0, jobs.FILE_DONE),
        (1, jobs.FILE_TRANSFERRING), (1, jobs.FILE_PROCESSING),
        (1, jobs.FILE_FAILED),
    ]
    # the partial tick (40 of 100 bytes) is relayed as plain progress
    assert (0, {"sent": 40}) in events
    done = [f for i, f in events if f.get("state") == jobs.FILE_DONE][0]
    assert done["image_ids"] == [1001] and done["sent"] == 100


def test_progress_ticks_are_throttled_but_processing_always_passes():
    import_one, _ = fake_importer(chunks=(10, 20, 30, 100))
    events, on_event = collect()
    batch.import_files([("a.svs", "p0", 100)], import_one,
                       on_event=on_event, min_interval=3600)
    plain = [f for _, f in events if "state" not in f]
    # within one huge interval only the first partial tick is relayed...
    assert plain == [{"sent": 10}]
    # ...yet the switch to processing (all bytes in) is never dropped
    assert any(f.get("state") == jobs.FILE_PROCESSING for _, f in events)


def test_summarize_partial_and_total_failure():
    ok = {"name": "a.svs", "image_ids": [1, 2], "error": None}
    bad = {"name": "b.svs", "image_ids": [], "error": "x"}
    ids, failed, warning = batch.summarize([ok, bad])
    assert ids == [1, 2] and failed == ["b.svs"]
    assert warning == "1 of 2 files failed to import: b.svs"
    # all failed: no warning (the caller reports a hard error instead)
    assert batch.summarize([bad]) == ([], ["b.svs"], None)
    assert batch.summarize([ok]) == ([1, 2], [], None)
