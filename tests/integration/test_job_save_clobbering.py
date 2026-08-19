"""Regression tests for a bug found while writing the cancellation
integration test in test_pipeline_process_job.py:

Job.append_log() used to delegate to touch_and_save(), a FULL row save
that re-persists every field on the in-memory Job instance - including
whatever stale value that instance held for fields OTHER code changed out
of band since the instance was loaded. Job.save_progress() already had a
comment explaining why it deliberately avoids this ("so it can't clobber
fields another save() call touched in between, like an appended log
line") - but append_log() itself had exactly that vulnerability.

Concretely: cancel_job() sets cancel_requested=True on a job that's
actively processing. process_job holds one Job instance for the entire
job run and calls job.append_log() repeatedly as the pipeline progresses
(e.g. right before conversion starts, in convert.py's bitrate-warning
log lines). Before the fix, any such append_log() call after cancellation
landed would silently write the *stale* (pre-cancellation) value of
cancel_requested back over the real one - meaning a cancel request could
be silently dropped depending on exactly when it arrived relative to the
next log line, defeating the "cancel mid-conversion" feature the
architecture doc describes.

The fix (app/db.py): append_log() now does a narrow save (only log +
updated_at), matching save_progress()'s existing pattern. That in turn
required two other call sites - which had been relying on append_log()'s
old full-save side effect to also persist fields they'd just set - to add
an explicit touch_and_save() of their own:
  - app/queue.py: start_job()'s except branch (status, error_message)
  - app/queue.py: cancel_job()'s STATUS_READY branch (status, queue_order)
"""
from app.db import Job
from app import queue as queue_mod
from app.pipeline import metadata as metadata_mod


def test_append_log_does_not_clobber_a_concurrently_set_field(isolated_dirs):
    """Direct reproduction of the bug: simulate an out-of-band DB write
    (what cancel_job() does from the web process) landing between when a
    Job instance is loaded and when append_log() is next called on it.
    """
    job = Job.create(source_path="/x")
    stale_handle = Job.get_by_id(job.id)  # simulates process_job's long-lived instance

    Job.update(cancel_requested=True).where(Job.id == job.id).execute()

    stale_handle.append_log("some unrelated log line")

    fresh = Job.get_by_id(job.id)
    assert fresh.cancel_requested is True
    assert "some unrelated log line" in fresh.log


def test_start_job_failure_persists_status_and_error_past_the_log_line(isolated_dirs, monkeypatch, huey_immediate):
    """Reproduces the exact call site that broke when append_log() was
    first narrowed: start_job's except branch sets status=FAILED and
    error_message, then logs - both must actually reach the DB.
    """
    def boom(*a, **k):
        raise RuntimeError("simulated detection failure")

    monkeypatch.setattr(queue_mod.detect, "detect", boom)

    source = isolated_dirs["inbox"] / "book.mp3"
    source.write_bytes(b"fake")
    job = Job.create(source_path=str(source))

    queue_mod.start_job(job.id)

    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_FAILED
    assert "simulated detection failure" in job.error_message
    assert "Failed during detection" in job.log


def test_cancel_ready_job_persists_status_and_queue_order(isolated_dirs, monkeypatch):
    """Reproduces the second call site: cancel_job() on a not-yet-started
    (READY) job sets status=CANCELLED and clears queue_order, then logs -
    both must actually reach the DB, not just the log line.
    """
    job = Job.create(source_path="/x", status=Job.STATUS_READY, queue_order=1)

    queue_mod.cancel_job(job.id)

    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_CANCELLED
    assert job.queue_order is None
    assert "Cancelled before conversion started" in job.log
