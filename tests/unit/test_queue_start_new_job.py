"""Unit tests for app/queue.py's start_new_job() - the single place a Job
first comes into existence, called both from the "Find this book" route
(app/web/routes.py) and from the watcher's AUTO_START_PROCESSING path
(app/watcher.py).
"""
from app.db import Job
from app import queue as queue_mod


def test_start_new_job_creates_queued_job_and_enqueues_detection(isolated_dirs, monkeypatch):
    calls = []
    monkeypatch.setattr(queue_mod, "start_job", lambda job_id: calls.append(job_id))

    source = isolated_dirs["inbox"] / "book.mp3"
    source.write_bytes(b"x" * 100)

    job = queue_mod.start_new_job(source)

    assert job.status == Job.STATUS_QUEUED
    assert job.source_path == str(source)
    assert calls == [job.id]


def test_start_new_job_persists_before_returning(isolated_dirs, monkeypatch):
    monkeypatch.setattr(queue_mod, "start_job", lambda job_id: None)

    source = isolated_dirs["inbox"] / "book.mp3"
    source.write_bytes(b"x" * 100)

    job = queue_mod.start_new_job(source)

    reloaded = Job.get_by_id(job.id)
    assert reloaded.status == Job.STATUS_QUEUED
    assert reloaded.source_path == str(source)
