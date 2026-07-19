"""Disk-backed job registry for asynchronous imports.

`complete_upload` used to run the OMERO import synchronously, blocking the
HTTP request until the (potentially many-minute) import finished. This
module lets the import run in a background thread while the request
returns immediately with a job id the UI can poll.

State is stored as JSON on disk (under the staging volume) rather than in
memory, because OMERO.web runs several gunicorn workers: the request that
polls a job's status may land on a different worker than the one running
the import thread, so the state must be shared, not per-process.
"""

import json
import os
import re
import time
import uuid

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def _staging_dir():
    return os.environ.get("WEBIMPORT_STAGING_DIR",
                          "/opt/omero/web/webimport_staging")


def _jobs_dir():
    path = os.path.join(_staging_dir(), "jobs")
    os.makedirs(path, exist_ok=True)
    return path


def _job_path(job_id):
    # ids are server-generated uuid4 hex; reject anything else so a
    # crafted id can never escape the jobs directory
    if not re.fullmatch(r"[0-9a-f]{32}", job_id or ""):
        return None
    return os.path.join(_jobs_dir(), "%s.json" % job_id)


def _write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def create_job(username):
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "username": username,
        "status": STATUS_RUNNING,
        "created": time.time(),
        "updated": time.time(),
        "image_ids": [],
        "warning": None,
        "error": None,
    }
    _write(_job_path(job_id), job)
    return job


def get_job(job_id):
    path = _job_path(job_id)
    if path is None or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def finish_job(job_id, image_ids=None, warning=None, error=None):
    job = get_job(job_id)
    if job is None:
        return None
    job["status"] = STATUS_FAILED if error else STATUS_DONE
    job["image_ids"] = image_ids or []
    job["warning"] = warning
    job["error"] = error
    job["updated"] = time.time()
    _write(_job_path(job_id), job)
    return job


def cleanup_old_jobs(max_age_seconds=24 * 3600):
    """Drop finished job files older than max_age_seconds."""
    try:
        cutoff = time.time() - max_age_seconds
        for entry in os.listdir(_jobs_dir()):
            if not entry.endswith(".json"):
                continue
            path = os.path.join(_jobs_dir(), entry)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass
