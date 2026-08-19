"""Integration tests for the conversion-queue dispatcher logic in
app/queue.py (confirm_metadata/dispatch_next/reorder_queue/cancel_job/
requeue_job) - ARCHITECTURE.md's "The conversion queue" section.

Deliberately does NOT use the huey_immediate fixture: with immediate mode,
confirming a job runs it to full completion synchronously, which collapses
away the very window this file needs to inspect (multiple jobs sitting
"ready" at once). Huey's default (non-immediate) mode with no consumer
process running means process_job(next_job.id) just enqueues silently and
returns - dispatch_next()'s own status flip to "processing" still happens
synchronously, which is the part these tests actually exercise.
"""
from app.db import Job
from app import queue as queue_mod

META = {"asin": "", "title": "T", "author": "", "narrator": "", "series": "",
        "series_index": "", "year": "", "genre": "", "description": "", "cover_url": ""}


def _make_awaiting_job(path):
    return Job.create(source_path=path, status=Job.STATUS_AWAITING_METADATA_CONFIRM)


def test_confirming_first_job_starts_it_immediately(isolated_dirs):
    job = _make_awaiting_job("/a")
    queue_mod.confirm_metadata(job.id, META)

    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_PROCESSING


def test_confirming_second_job_while_first_processes_leaves_it_ready(isolated_dirs):
    job1 = _make_awaiting_job("/a")
    job2 = _make_awaiting_job("/b")

    queue_mod.confirm_metadata(job1.id, META)
    queue_mod.confirm_metadata(job2.id, META)

    job1 = Job.get_by_id(job1.id)
    job2 = Job.get_by_id(job2.id)
    assert job1.status == Job.STATUS_PROCESSING
    assert job2.status == Job.STATUS_READY
    assert job2.queue_order == 2


def test_only_one_job_processing_at_a_time(isolated_dirs):
    jobs = [_make_awaiting_job(f"/{i}") for i in range(3)]
    for j in jobs:
        queue_mod.confirm_metadata(j.id, META)

    statuses = [Job.get_by_id(j.id).status for j in jobs]
    assert statuses.count(Job.STATUS_PROCESSING) == 1
    assert statuses.count(Job.STATUS_READY) == 2


def test_reorder_up_swaps_queue_order_with_previous_ready_job(isolated_dirs):
    job1 = _make_awaiting_job("/a")
    job2 = _make_awaiting_job("/b")
    job3 = _make_awaiting_job("/c")
    for j in (job1, job2, job3):
        queue_mod.confirm_metadata(j.id, META)
    # job1 is processing; job2 (order 2) and job3 (order 3) are ready.

    queue_mod.reorder_queue(job3.id, "up")

    job2 = Job.get_by_id(job2.id)
    job3 = Job.get_by_id(job3.id)
    assert job3.queue_order == 2
    assert job2.queue_order == 3


def test_reorder_processing_job_is_a_noop(isolated_dirs):
    """The currently-processing job isn't part of the reorderable set - it's
    already running.
    """
    job1 = _make_awaiting_job("/a")
    queue_mod.confirm_metadata(job1.id, META)
    before = Job.get_by_id(job1.id).queue_order

    queue_mod.reorder_queue(job1.id, "down")

    assert Job.get_by_id(job1.id).queue_order == before


def test_reorder_beyond_the_ends_is_a_noop(isolated_dirs):
    job1 = _make_awaiting_job("/a")
    job2 = _make_awaiting_job("/b")
    queue_mod.confirm_metadata(job1.id, META)
    queue_mod.confirm_metadata(job2.id, META)
    before = Job.get_by_id(job2.id).queue_order

    queue_mod.reorder_queue(job2.id, "up")  # job2 is the only ready job - nothing above it

    assert Job.get_by_id(job2.id).queue_order == before


def test_cancel_ready_job_does_not_disturb_the_processing_job(isolated_dirs):
    job1 = _make_awaiting_job("/a")
    job2 = _make_awaiting_job("/b")
    queue_mod.confirm_metadata(job1.id, META)
    queue_mod.confirm_metadata(job2.id, META)

    queue_mod.cancel_job(job2.id)

    assert Job.get_by_id(job1.id).status == Job.STATUS_PROCESSING
    job2 = Job.get_by_id(job2.id)
    assert job2.status == Job.STATUS_CANCELLED
    assert job2.queue_order is None


def test_cancel_processing_job_sets_flag_without_changing_status(isolated_dirs):
    """Can't stop a running conversion from here directly (it's in the
    other process) - only flip cancel_requested for the worker's own
    should_cancel() polling to notice.
    """
    job = _make_awaiting_job("/a")
    queue_mod.confirm_metadata(job.id, META)

    queue_mod.cancel_job(job.id)

    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_PROCESSING
    assert job.cancel_requested is True


def test_requeue_puts_cancelled_job_back_at_the_end(isolated_dirs):
    job1 = _make_awaiting_job("/a")
    job2 = _make_awaiting_job("/b")
    job3 = _make_awaiting_job("/c")
    queue_mod.confirm_metadata(job1.id, META)  # processing
    queue_mod.confirm_metadata(job2.id, META)  # ready, order 2
    queue_mod.cancel_job(job2.id)
    queue_mod.confirm_metadata(job3.id, META)  # ready, order 3

    queue_mod.requeue_job(job2.id)

    job2 = Job.get_by_id(job2.id)
    job3 = Job.get_by_id(job3.id)
    assert job2.status == Job.STATUS_READY
    assert job2.queue_order > job3.queue_order


def test_dispatch_next_is_a_noop_when_nothing_is_ready(isolated_dirs):
    # No jobs at all - must not raise.
    queue_mod.dispatch_next()
    assert Job.select().count() == 0


def test_remove_job_hides_without_deleting_row(isolated_dirs):
    job = Job.create(source_path="/a", status=Job.STATUS_DONE)
    queue_mod.remove_job(job.id)

    job = Job.get_by_id(job.id)
    assert job.dismissed is True
    # Row still exists (see app/db.py's comment on `dismissed` for why
    # deleting it outright would make the watcher re-detect the source).
    assert Job.select().where(Job.id == job.id).exists()
