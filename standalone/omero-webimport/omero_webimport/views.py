#
# Copyright (c) 2019 University of Dundee.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid

from django.shortcuts import render
from django.http import JsonResponse
from omeroweb.webclient.decorators import login_required

import omero.model

from . import jobs
from .util.import_library import ImportLibrary

logger = logging.getLogger(__name__)

# Per-file upload cap in MiB. Whole-slide images legitimately reach many
# GiB, so the default is deliberately generous - tune per deployment via
# the WEBIMPORT_MAX_FILE_MB environment variable.
MAX_FILE_MB = int(os.environ.get("WEBIMPORT_MAX_FILE_MB", 20480))

# Chunked-upload staging (TC-19: large uploads survive connection drops
# because the client retries individual chunks instead of one giant
# request). Staged files live here until complete_upload() imports them.
STAGING_DIR = os.environ.get("WEBIMPORT_STAGING_DIR",
                             "/opt/omero/web/webimport_staging")
MAX_CHUNK_MB = int(os.environ.get("WEBIMPORT_MAX_CHUNK_MB", 100))
STALE_UPLOAD_SECONDS = 24 * 3600


def _error(message, code, status):
    """Consistent JSON error shape for the upload UI.

    Never echoes raw exception text to the client - details go to the
    server log instead (the previous behaviour returned str(e), which
    leaked internal paths and OMERO session details).
    """
    return JsonResponse(
        {"success": False, "error": {"code": code, "message": message}},
        status=status,
    )


def _safe_filename(name):
    name = os.path.basename(name or "")
    name = re.sub(r"[^A-Za-z0-9. _-]+", "_", name)
    return name[:200] or "unnamed"


def _resolve_dataset(request, conn):
    """Validate the optional dataset_id target.

    Returns (dataset, error_response); exactly one is None.
    """
    dataset_id = request.POST.get("dataset_id")
    if not dataset_id:
        return None, None
    try:
        dataset_id = int(dataset_id)
    except ValueError:
        return None, _error("dataset_id must be an integer.",
                            "bad_dataset", 400)
    dataset = conn.getObject("Dataset", dataset_id)
    if dataset is None:
        return None, _error("Dataset %s was not found." % dataset_id,
                            "bad_dataset", 404)
    if not dataset.canLink():
        return None, _error(
            "You do not have permission to import into dataset %s."
            % dataset_id,
            "forbidden_dataset", 403)
    return dataset, None


def _link_images(conn, dataset, img_ids):
    """Link imported images to the dataset; returns a warning string on
    failure instead of failing the whole import."""
    try:
        links = []
        for iid in img_ids:
            link = omero.model.DatasetImageLinkI()
            link.setParent(omero.model.DatasetI(dataset.getId(), False))
            link.setChild(omero.model.ImageI(iid, False))
            links.append(link)
        conn.getUpdateService().saveArray(links, conn.SERVICE_OPTS)
        return None
    except Exception:
        logger.exception("Imported OK but linking to dataset %s failed",
                         dataset.getId())
        return ("Images were imported but could not be linked to "
                "dataset %s." % dataset.getId())


@login_required()
def index(request, conn=None, **kwargs):
    """
    Home page
    """
    template = "omero_webimport/index.html"
    return render(request, template, {})


@login_required()
def datasets(request, conn=None, **kwargs):
    """List datasets the current user can import into (target dropdown)."""
    data = [
        {"id": d.getId(), "name": d.getName() or "Unnamed dataset"}
        for d in conn.getObjects("Dataset")
        if d.canLink()
    ]
    data.sort(key=lambda d: d["name"].lower())
    return JsonResponse({"success": True, "datasets": data})


@login_required()
def submit_import(request, conn=None, **kwargs):
    """Single-request import - kept for small files and API clients.

    The web UI uses the chunked begin/chunk/complete flow below.
    """
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)

    # Collect files exactly as the UI submits them: file0..fileN
    files = []
    count = 0
    while request.FILES.get("file%s" % count) is not None:
        files.append(request.FILES.get("file%s" % count))
        count += 1

    if not files:
        return _error("No files were attached to the request.",
                      "no_files", 400)

    max_bytes = MAX_FILE_MB * 1024 * 1024
    for f in files:
        if f.size == 0:
            return _error("'%s' is empty." % f.name, "empty_file", 400)
        if f.size > max_bytes:
            return _error(
                "'%s' exceeds the %s MiB per-file limit."
                % (f.name, MAX_FILE_MB),
                "file_too_large", 413)

    dataset, err = _resolve_dataset(request, conn)
    if err is not None:
        return err

    try:
        def chunks_gen(open_file):
            for chunk in open_file.chunks():
                yield chunk

        import_lib = ImportLibrary(conn.c)
        rsp = import_lib.import_image(
            (f for f in files),
            (chunks_gen(f) for f in files),
            wait=True)
        img_ids = [p.image.id.val for p in rsp.pixels]
    except Exception:
        logger.exception("Import failed")
        return _error(
            "Import failed on the server; ask an administrator to check "
            "the OMERO.web logs.",
            "import_failed", 500)

    warning = None
    if dataset is not None and img_ids:
        warning = _link_images(conn, dataset, img_ids)

    payload = {"success": True, "image_ids": img_ids}
    if warning:
        payload["warning"] = warning
    return JsonResponse(payload)


# ---------------------------------------------------------------------------
# Chunked upload flow: begin/ -> chunk/ (repeat, retryable) -> complete/
# ---------------------------------------------------------------------------

def _upload_dir(upload_id):
    """Staging dir for an upload id; None if the id is malformed.

    Ids are server-generated uuid4 hex - rejecting anything else means a
    crafted id can never escape STAGING_DIR.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        return None
    return os.path.join(STAGING_DIR, upload_id)


def _meta_path(upload_dir):
    return os.path.join(upload_dir, "meta.json")


def _load_meta(upload_dir):
    try:
        with open(_meta_path(upload_dir), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_meta(upload_dir, meta):
    tmp = _meta_path(upload_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f)
    os.replace(tmp, _meta_path(upload_dir))


def _cleanup_stale_uploads():
    """Drop staging dirs older than STALE_UPLOAD_SECONDS (abandoned)."""
    try:
        if not os.path.isdir(STAGING_DIR):
            return
        cutoff = time.time() - STALE_UPLOAD_SECONDS
        for entry in os.listdir(STAGING_DIR):
            path = os.path.join(STAGING_DIR, entry)
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        logger.exception("Stale staging cleanup failed")


@login_required()
def begin_upload(request, conn=None, **kwargs):
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)
    _cleanup_stale_uploads()
    upload_id = uuid.uuid4().hex
    upload_dir = os.path.join(STAGING_DIR, upload_id)
    os.makedirs(upload_dir, exist_ok=True)
    _save_meta(upload_dir, {
        "username": conn.getUser().getName(),
        "created": time.time(),
        "files": {},
    })
    return JsonResponse({"success": True, "upload_id": upload_id,
                         "chunk_size_mb": MAX_CHUNK_MB})


@login_required()
def upload_chunk(request, conn=None, **kwargs):
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)

    upload_dir = _upload_dir(request.POST.get("upload_id", ""))
    if upload_dir is None or not os.path.isdir(upload_dir):
        return _error("Unknown upload_id.", "bad_upload", 404)
    meta = _load_meta(upload_dir)
    if meta is None:
        return _error("Upload metadata missing.", "bad_upload", 410)
    if meta["username"] != conn.getUser().getName():
        return _error("Not your upload.", "forbidden", 403)

    try:
        file_index = int(request.POST.get("file_index", ""))
        chunk_index = int(request.POST.get("chunk_index", ""))
    except ValueError:
        return _error("file_index and chunk_index must be integers.",
                      "bad_params", 400)

    chunk = request.FILES.get("chunk")
    if chunk is None:
        return _error("No chunk attached.", "no_chunk", 400)
    if chunk.size > MAX_CHUNK_MB * 1024 * 1024:
        return _error("Chunk exceeds %s MiB." % MAX_CHUNK_MB,
                      "chunk_too_large", 413)

    key = str(file_index)
    finfo = meta["files"].setdefault(key, {
        "name": _safe_filename(request.POST.get("filename", "")),
        "next_chunk": 0,
        "bytes": 0,
    })

    # Retried chunk we already have: acknowledge idempotently
    if chunk_index < finfo["next_chunk"]:
        return JsonResponse({"success": True,
                             "received_bytes": finfo["bytes"],
                             "duplicate": True})
    if chunk_index != finfo["next_chunk"]:
        return _error(
            "Out-of-order chunk %s (expected %s)."
            % (chunk_index, finfo["next_chunk"]),
            "out_of_order", 409)

    if finfo["bytes"] + chunk.size > MAX_FILE_MB * 1024 * 1024:
        return _error(
            "'%s' exceeds the %s MiB per-file limit."
            % (finfo["name"], MAX_FILE_MB),
            "file_too_large", 413)

    part_path = os.path.join(upload_dir, "%s.part" % file_index)
    with open(part_path, "ab") as f:
        for piece in chunk.chunks():
            f.write(piece)

    finfo["next_chunk"] += 1
    finfo["bytes"] += chunk.size
    _save_meta(upload_dir, meta)
    return JsonResponse({"success": True,
                         "received_bytes": finfo["bytes"]})


def _disk_chunks(path, buf=1024 * 1024):
    with open(path, "rb") as f:
        while True:
            block = f.read(buf)
            if not block:
                break
            yield block


def _do_import(client, names, paths):
    """Run the actual OMERO import; returns the list of new image ids."""
    import_lib = ImportLibrary(client)
    rsp = import_lib.import_image(
        iter(names),
        (_disk_chunks(p) for p in paths),
        wait=True)
    return [p.image.id.val for p in rsp.pixels]


def _run_import_job(job_id, session_uuid, host, port, names, paths,
                    upload_dir, dataset_id):
    """Background worker: rejoin the user's session and import.

    Runs in a daemon thread after the HTTP request has already returned,
    so it needs its own OMERO connection - it joins the existing session
    by UUID (acting as the same user) rather than reusing the request's
    conn, which is torn down when the request ends.
    """
    from omero.gateway import BlitzGateway
    gateway = None
    try:
        gateway = BlitzGateway(host=host, port=port, secure=True)
        if not gateway.connect(sUuid=session_uuid):
            jobs.finish_job(job_id, error="Could not rejoin the upload "
                            "session; it may have expired. Please retry.")
            return
        image_ids = _do_import(gateway.c, names, paths)
        warning = None
        if dataset_id is not None and image_ids:
            dataset = gateway.getObject("Dataset", dataset_id)
            if dataset is not None:
                warning = _link_images(gateway, dataset, image_ids)
        shutil.rmtree(upload_dir, ignore_errors=True)
        jobs.finish_job(job_id, image_ids=image_ids, warning=warning)
    except Exception:
        logger.exception("Async import failed (job %s, upload %s)",
                         job_id, os.path.basename(upload_dir))
        # keep the staged upload so the user can retry
        jobs.finish_job(job_id, error="Import failed on the server; ask an "
                        "administrator to check the OMERO.web logs. Your "
                        "staged upload is kept for retry.")
    finally:
        if gateway is not None:
            gateway.close()


@login_required()
def complete_upload(request, conn=None, **kwargs):
    if request.method != "POST":
        return _error("POST required.", "method_not_allowed", 405)

    upload_dir = _upload_dir(request.POST.get("upload_id", ""))
    if upload_dir is None or not os.path.isdir(upload_dir):
        return _error("Unknown upload_id.", "bad_upload", 404)
    meta = _load_meta(upload_dir)
    if meta is None:
        return _error("Upload metadata missing.", "bad_upload", 410)
    if meta["username"] != conn.getUser().getName():
        return _error("Not your upload.", "forbidden", 403)
    if not meta["files"]:
        return _error("No files were staged for this upload.",
                      "no_files", 400)

    dataset, err = _resolve_dataset(request, conn)
    if err is not None:
        return err

    ordered = sorted(meta["files"].items(), key=lambda kv: int(kv[0]))
    names, paths = [], []
    for index, finfo in ordered:
        path = os.path.join(upload_dir, "%s.part" % index)
        if not os.path.exists(path) or finfo["bytes"] == 0:
            return _error("File %s ('%s') has no staged data."
                          % (index, finfo["name"]), "incomplete", 400)
        names.append(finfo["name"])
        paths.append(path)

    # Synchronous fallback (WEBIMPORT_ASYNC=0): block until import done.
    # The default async path returns immediately and the UI polls.
    if os.environ.get("WEBIMPORT_ASYNC", "1") != "1":
        try:
            img_ids = _do_import(conn.c, names, paths)
        except Exception:
            logger.exception("Chunked import failed (upload %s)",
                             os.path.basename(upload_dir))
            return _error(
                "Import failed on the server; ask an administrator to "
                "check the OMERO.web logs. Your staged upload is kept "
                "for retry.", "import_failed", 500)
        warning = None
        if dataset is not None and img_ids:
            warning = _link_images(conn, dataset, img_ids)
        shutil.rmtree(upload_dir, ignore_errors=True)
        payload = {"success": True, "image_ids": img_ids}
        if warning:
            payload["warning"] = warning
        return JsonResponse(payload)

    # Async path: spawn a background thread that rejoins the session.
    jobs.cleanup_old_jobs()
    job = jobs.create_job(conn.getUser().getName())
    host = os.environ.get("OMEROHOST", "omeroserver")
    worker = threading.Thread(
        target=_run_import_job,
        args=(job["id"], conn._sessionUuid, host, 4064, names, paths,
              upload_dir, dataset.getId() if dataset is not None else None),
        daemon=True,
    )
    worker.start()
    return JsonResponse({"success": True, "job_id": job["id"],
                         "status_url": "status/%s/" % job["id"]},
                        status=202)


@login_required()
def import_status(request, job_id, conn=None, **kwargs):
    """Poll an async import job's status."""
    job = jobs.get_job(job_id)
    if job is None:
        return _error("Unknown job.", "not_found", 404)
    if job["username"] != conn.getUser().getName():
        return _error("Not your job.", "forbidden", 403)
    return JsonResponse({
        "success": True,
        "status": job["status"],
        "image_ids": job.get("image_ids", []),
        "warning": job.get("warning"),
        "error": job.get("error"),
    })
