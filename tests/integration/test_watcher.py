"""Integration tests for app/watcher.py against a real watchdog Observer
and real filesystem events - the settle-window behavior from the planning
doc (Section 2 step 2): "waits until it's fully written (no size/mtime
changes for some settle window) so partial downloads aren't picked up
mid-copy."
"""
import time

import pytest

from app.db import Job
from app.watcher import start_watcher


@pytest.fixture
def running_watcher(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.4)
    monkeypatch.setattr(config, "AUTO_START_PROCESSING", False)
    observer, stop_event = start_watcher()
    yield
    stop_event.set()
    observer.stop()
    observer.join(timeout=5)


def _wait_until(predicate, timeout=5.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_drop_in_not_queued_before_settle_window_elapses(isolated_dirs, running_watcher):
    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"x" * 1000)

    time.sleep(0.1)  # well under the 0.4s settle window
    assert Job.select().count() == 0


def test_drop_in_queued_after_settle_window_elapses(isolated_dirs, running_watcher):
    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"x" * 1000)

    assert _wait_until(lambda: Job.select().count() == 1, timeout=5.0)
    job = Job.select().first()
    assert job.source_path == str(isolated_dirs["inbox"] / "book.mp3")
    assert job.status == Job.STATUS_PENDING


def test_file_still_being_written_resets_settle_timer(isolated_dirs, running_watcher):
    """Simulates a slow copy: repeated writes to the same file should keep
    pushing the settle window out, not get queued mid-write.
    """
    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 100)

    # The watcher's settle checker only ticks once per second regardless of
    # SETTLE_WINDOW_SEC, so this needs to span more than one tick to prove a
    # mid-write tick didn't queue it prematurely.
    end = time.time() + 1.2
    while time.time() < end:
        with open(f, "ab") as fh:
            fh.write(b"y" * 100)
        time.sleep(0.15)

    # Right after the last write, it should NOT have queued yet.
    assert Job.select().count() == 0

    assert _wait_until(lambda: Job.select().count() == 1, timeout=5.0)


def test_folder_drop_in_waits_for_all_files_to_settle(isolated_dirs, running_watcher):
    folder = isolated_dirs["inbox"] / "Multi Book"
    folder.mkdir()
    (folder / "track1.mp3").write_bytes(b"x" * 100)

    time.sleep(0.2)
    (folder / "track2.mp3").write_bytes(b"y" * 100)  # resets the folder's settle timer

    time.sleep(0.1)
    assert Job.select().count() == 0

    assert _wait_until(lambda: Job.select().count() == 1, timeout=5.0)
    job = Job.select().first()
    assert job.source_path == str(folder)


def test_source_with_existing_job_is_not_requeued(isolated_dirs, running_watcher):
    """A "kept" (SOURCE_CLEANUP_MODE=keep) source still sitting in the
    inbox after its job finished must not be re-detected as a new drop-in.
    """
    f = isolated_dirs["inbox"] / "already_done.mp3"
    f.write_bytes(b"x" * 100)
    Job.create(source_path=str(f), status=Job.STATUS_DONE)

    time.sleep(1.5)  # comfortably past one checker tick and the settle window
    assert Job.select().where(Job.source_path == str(f)).count() == 1


def test_removed_source_path_is_freed_for_a_new_drop_off(isolated_dirs, running_watcher, monkeypatch):
    """Removing a job (see app/queue.py's remove_job) cleans up its source
    the same way a completed job's is cleaned up, per SOURCE_CLEANUP_MODE -
    so the inbox path it used to occupy is genuinely free afterward. A new,
    unrelated drop-off at that same name/path must be queued as new, not
    silently swallowed by the same-path dedup that exists to stop an
    untouched, still-present source from being re-queued forever.
    """
    from app.config import config
    from app import queue as queue_mod

    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "archive")

    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 100)
    assert _wait_until(lambda: Job.select().count() == 1, timeout=5.0)
    old_job = Job.select().first()
    queue_mod.remove_job(old_job.id)

    assert not f.exists()  # moved to ARCHIVE_DIR by remove_job's cleanup
    f.write_bytes(b"y" * 200)

    assert _wait_until(lambda: Job.select().where(Job.dismissed == False).count() == 1, timeout=5.0)  # noqa: E712
    new_job = Job.select().where(Job.dismissed == False).first()  # noqa: E712
    assert new_job.id != old_job.id
    assert new_job.source_path == str(f)


def test_dotfile_drop_in_is_never_queued(isolated_dirs, running_watcher):
    """A macOS Finder .DS_Store (or any dotfile/dot-directory) dropped into
    the inbox must never become a Job - it can never be usefully processed
    and would otherwise get permanently stuck in the "Needs Input" queue.
    """
    (isolated_dirs["inbox"] / ".DS_Store").write_bytes(b"x" * 100)

    time.sleep(1.0)  # comfortably past one checker tick and the settle window
    assert Job.select().count() == 0

    # A normal drop-off alongside it should still be picked up as usual.
    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"y" * 1000)
    assert _wait_until(lambda: Job.select().count() == 1, timeout=5.0)
    job = Job.select().first()
    assert job.source_path == str(isolated_dirs["inbox"] / "book.mp3")


def test_synology_eadir_drop_in_is_never_queued(isolated_dirs, running_watcher):
    """Synology DSM auto-creates an @eaDir folder in every directory on a
    DSM volume, including one bind-mounted in as the inbox - it must never
    become a Job, same as a dotfile.
    """
    (isolated_dirs["inbox"] / "@eaDir").mkdir()
    (isolated_dirs["inbox"] / "@eaDir" / "SYNOINDEX_THUMB_ME.txt").write_bytes(b"x" * 100)

    time.sleep(1.0)  # comfortably past one checker tick and the settle window
    assert Job.select().count() == 0

    # A normal drop-off alongside it should still be picked up as usual.
    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"y" * 1000)
    assert _wait_until(lambda: Job.select().count() == 1, timeout=5.0)
    job = Job.select().first()
    assert job.source_path == str(isolated_dirs["inbox"] / "book.mp3")


def test_preexisting_synology_eadir_in_inbox_is_never_queued(isolated_dirs, monkeypatch):
    """An @eaDir already sitting in the inbox before the watcher starts
    (picked up by the initial iterdir() scan) must also be ignored.
    """
    from app.config import config

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.4)
    monkeypatch.setattr(config, "AUTO_START_PROCESSING", False)

    (isolated_dirs["inbox"] / "@eaDir").mkdir()

    observer, stop_event = start_watcher()
    try:
        time.sleep(1.0)  # comfortably past one checker tick and the settle window
        assert Job.select().count() == 0
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)


def test_preexisting_dotfile_in_inbox_is_never_queued(isolated_dirs, monkeypatch):
    """A dotfile already sitting in the inbox before the watcher starts
    (picked up by the initial iterdir() scan) must also be ignored.
    """
    from app.config import config

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.4)
    monkeypatch.setattr(config, "AUTO_START_PROCESSING", False)

    (isolated_dirs["inbox"] / ".DS_Store").write_bytes(b"x" * 100)

    observer, stop_event = start_watcher()
    try:
        time.sleep(1.0)  # comfortably past one checker tick and the settle window
        assert Job.select().count() == 0
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)


def test_auto_start_processing_queues_and_starts_immediately(isolated_dirs, monkeypatch, huey_immediate):
    from app.config import config
    from app.pipeline import metadata as metadata_mod

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.3)
    monkeypatch.setattr(config, "AUTO_START_PROCESSING", True)
    monkeypatch.setattr(metadata_mod, "search", lambda *a, **k: [])
    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin: [])

    from tests.helpers import make_tone_mp3

    observer, stop_event = start_watcher()
    try:
        make_tone_mp3(isolated_dirs["inbox"] / "book.mp3", duration_sec=0.5)
        assert _wait_until(
            lambda: Job.select().count() == 1
            and Job.select().first().status in (Job.STATUS_AWAITING_METADATA_CONFIRM, Job.STATUS_FAILED),
            timeout=5.0,
        )
        job = Job.select().first()
        # With AUTO_START_PROCESSING, detection/metadata search must have
        # run without any manual "start" trigger from the UI.
        assert job.status == Job.STATUS_AWAITING_METADATA_CONFIRM
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)
