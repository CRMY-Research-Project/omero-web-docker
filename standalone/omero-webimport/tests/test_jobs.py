"""Unit tests for the async-import job registry (disk-backed, no OMERO)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from omero_webimport import jobs  # noqa: E402


@pytest.fixture(autouse=True)
def staging(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBIMPORT_STAGING_DIR", str(tmp_path))
    return tmp_path


def test_create_job_is_running():
    job = jobs.create_job("alice")
    assert job["status"] == jobs.STATUS_RUNNING
    assert job["username"] == "alice"
    assert jobs.get_job(job["id"])["status"] == jobs.STATUS_RUNNING


def test_finish_job_done_with_images():
    job = jobs.create_job("alice")
    out = jobs.finish_job(job["id"], image_ids=[1, 2, 3], warning="w")
    assert out["status"] == jobs.STATUS_DONE
    assert out["image_ids"] == [1, 2, 3]
    assert out["warning"] == "w"
    assert jobs.get_job(job["id"])["status"] == jobs.STATUS_DONE


def test_finish_job_failed_sets_error():
    job = jobs.create_job("bob")
    out = jobs.finish_job(job["id"], error="boom")
    assert out["status"] == jobs.STATUS_FAILED
    assert out["error"] == "boom"
    assert out["image_ids"] == []


def test_get_job_rejects_bad_id():
    assert jobs.get_job("../../etc/passwd") is None
    assert jobs.get_job("NOTHEX") is None
    assert jobs.get_job("") is None
    assert jobs.get_job("0" * 32) is None  # well-formed but missing


def test_finish_unknown_job_returns_none():
    assert jobs.finish_job("0" * 32, image_ids=[1]) is None


def test_cleanup_old_jobs_removes_stale(staging):
    job = jobs.create_job("alice")
    path = os.path.join(str(staging), "jobs", job["id"] + ".json")
    assert os.path.exists(path)
    old = os.stat(path).st_mtime - 48 * 3600
    os.utime(path, (old, old))
    jobs.cleanup_old_jobs(max_age_seconds=24 * 3600)
    assert not os.path.exists(path)


def test_cleanup_keeps_recent(staging):
    job = jobs.create_job("alice")
    jobs.cleanup_old_jobs(max_age_seconds=24 * 3600)
    assert jobs.get_job(job["id"]) is not None
