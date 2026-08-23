"""Regression test for the lookup/conversion Huey split.

Before this split, start_job (detection + metadata search) and
process_job (conversion) were both @huey.task()s on the same
single-worker consumer. Since Huey runs one worker's tasks strictly
FIFO, a newly-dropped book's start_job would sit behind an unrelated
in-progress process_job for however long that conversion took - live
container logs showed a conversion running 1057s with a start_job queued
right behind it, executing in 0.7s the instant the worker finally freed
up. The fix is two independent Huey instances (app/queue.py's huey and
lookup_huey), each with its own consumer process (see run.sh), so a
lookup is never stuck behind a conversion. This test guards against that
split being accidentally collapsed back into one queue.
"""
from app import queue as queue_mod


def test_start_job_and_process_job_run_on_separate_huey_instances():
    assert queue_mod.start_job.huey is queue_mod.lookup_huey
    assert queue_mod.process_job.huey is queue_mod.huey
    assert queue_mod.lookup_huey is not queue_mod.huey


def test_lookup_and_conversion_queues_are_independently_named():
    # Both instances share one huey.db file (Huey namespaces rows by queue
    # name), so what actually keeps their tasks from competing for the
    # same worker is the queue name, not the file - assert that directly.
    assert queue_mod.huey.name != queue_mod.lookup_huey.name
