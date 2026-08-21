"""Integration tests for the pending:<name> branch of /jobs/{id}/start and
/jobs/{id}/remove - see docs/superpowers/specs/2026-08-21-defer-job-
creation-until-lookup-design.md.
"""
import time

import pytest

from app.db import Job
from app.watcher import list_pending, start_watcher


@pytest.fixture(autouse=True)
def _running_watcher(isolated_dirs):
    """list_pending() only reports anything while a watcher's checker
    loop/observer is actually running (see app/watcher.py's module-level
    _activity dict) - autouse so every test below gets one pointed at its
    isolated inbox without repeating the start/stop boilerplate.
    """
    observer, stop_event = start_watcher()
    yield
    stop_event.set()
    observer.stop()
    observer.join(timeout=5)


def _settle_and_get_pending_id(isolated_dirs, name: str, monkeypatch, content=b"x" * 100):
    from app.config import config

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.1)
    (isolated_dirs["inbox"] / name).write_bytes(content)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        pending = list_pending()
        if pending:
            return pending[0].id
        time.sleep(0.1)
    raise AssertionError(f"{name} never settled")


def test_start_on_pending_entry_creates_job_and_enqueues(client, isolated_dirs, monkeypatch):
    monkeypatch.setattr("app.web.routes.start_job", lambda job_id: None)
    pending_id = _settle_and_get_pending_id(isolated_dirs, "book.mp3", monkeypatch)

    resp = client.post(f"/jobs/{pending_id}/start")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    job = Job.get_by_id(body["job_id"])
    assert job.status == Job.STATUS_QUEUED
    assert job.source_path == str(isolated_dirs["inbox"] / "book.mp3")
    assert list_pending() == []


def test_start_on_already_claimed_pending_entry_404s(client, isolated_dirs, monkeypatch):
    monkeypatch.setattr("app.web.routes.start_job", lambda job_id: None)
    pending_id = _settle_and_get_pending_id(isolated_dirs, "book.mp3", monkeypatch)

    first = client.post(f"/jobs/{pending_id}/start")
    assert first.status_code == 200

    second = client.post(f"/jobs/{pending_id}/start")
    assert second.status_code == 404


def test_remove_on_pending_entry_archives_source_and_creates_no_job(client, isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "archive")
    pending_id = _settle_and_get_pending_id(isolated_dirs, "book.mp3", monkeypatch)

    resp = client.post(f"/jobs/{pending_id}/remove")

    assert resp.status_code == 200
    assert not (isolated_dirs["inbox"] / "book.mp3").exists()
    assert (isolated_dirs["archive"] / "book.mp3").exists()
    assert Job.select().count() == 0
    assert list_pending() == []


def test_pending_entry_name_with_space_round_trips(client, isolated_dirs, monkeypatch):
    """Regression test for the URL-encoding gotcha found during design:
    app.js concatenates the id into the request URL with no encoding, so
    the id itself must already be URL-safe by the time it's embedded in
    the page.
    """
    monkeypatch.setattr("app.web.routes.start_job", lambda job_id: None)
    pending_id = _settle_and_get_pending_id(isolated_dirs, "The calamity Club.mp3", monkeypatch)

    assert " " not in pending_id  # would break app.js's unencoded string concatenation
    assert pending_id == "pending:The%20calamity%20Club.mp3"

    resp = client.post(f"/jobs/{pending_id}/start")

    assert resp.status_code == 200
    job = Job.get_by_id(resp.json()["job_id"])
    assert job.source_path == str(isolated_dirs["inbox"] / "The calamity Club.mp3")
