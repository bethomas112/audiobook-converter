"""Watches INBOX_DIR for new top-level entries (a single audio file, or a
folder of them) and queues a Job once the entry has stopped changing for
SETTLE_WINDOW_SEC — so partial downloads/copies aren't picked up mid-write.

watchdog supplies the filesystem events (which reset an entry's "last
activity" timestamp); a lightweight polling loop does the actual settle-
window bookkeeping, since a dropped-off item can be a whole folder of
files still being written one at a time, not just a single file.
"""
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import config
from app.db import Job
from app.queue import start_job


# NAS-generated system directories that can appear inside a bind-mounted
# inbox alongside dotfiles - never a real drop-off, so ignored the same way.
# @eaDir is Synology DSM's thumbnail/index cache, auto-created in every
# folder on a DSM volume (including a folder mounted into this container).
_IGNORED_TOP_LEVEL_NAMES = {"@eaDir"}


def _is_ignored_top_level_name(name: str) -> bool:
    return name.startswith(".") or name in _IGNORED_TOP_LEVEL_NAMES


class _ActivityHandler(FileSystemEventHandler):
    def __init__(self, activity: dict, lock: threading.Lock):
        self._activity = activity
        self._lock = lock

    def _top_level_entry(self, path: str):
        p = Path(path)
        try:
            rel = p.relative_to(config.INBOX_DIR)
        except ValueError:
            return None
        if not rel.parts:
            return None
        top_name = rel.parts[0]
        if _is_ignored_top_level_name(top_name):
            # Never a real drop-off - would otherwise become a
            # permanently-stuck, unprocessable job (see
            # _IGNORED_TOP_LEVEL_NAMES above for the NAS-specific case).
            return None
        return config.INBOX_DIR / top_name

    def on_any_event(self, event):
        entry = self._top_level_entry(event.src_path)
        if entry is not None:
            with self._lock:
                self._activity[entry] = time.time()


def _known_sources() -> dict:
    """Every source path that's ever had a Job created for it, mapped to
    that job's recorded source_mtime (None if it doesn't have one - see
    Job.source_mtime). A kept (non-cleaned-up) source still at a known path
    with an unchanged mtime is the "not re-queued forever" case this
    guards; a path reused by a genuinely different file (different mtime)
    is not.
    """
    known = {}
    for j in Job.select(Job.source_path, Job.source_mtime):
        known[Path(j.source_path)] = j.source_mtime
    return known


def _safe_mtime(entry: Path):
    try:
        return entry.stat().st_mtime
    except OSError:
        return None


def _is_same_known_source(entry: Path, known_mtime) -> bool:
    # None means either "this path was never seen before" (not in `known`
    # at all - checked separately by the caller) or "a job exists for it
    # but its original mtime was never recorded" - the latter can't safely
    # be treated as "this is a new file," so it still blocks, same as
    # before mtime tracking existed.
    if known_mtime is None:
        return True
    return _safe_mtime(entry) == known_mtime


def _settle_checker_loop(activity: dict, lock: threading.Lock, stop_event: threading.Event):
    with lock:
        for entry in config.INBOX_DIR.iterdir():
            if _is_ignored_top_level_name(entry.name):
                continue
            activity.setdefault(entry, time.time())

    while not stop_event.is_set():
        stop_event.wait(1)
        now = time.time()
        known = _known_sources()

        with lock:
            snapshot = list(activity.items())

        for entry, last_seen in snapshot:
            if not entry.exists():
                with lock:
                    activity.pop(entry, None)
                continue
            if entry in known and _is_same_known_source(entry, known[entry]):
                with lock:
                    activity.pop(entry, None)
                continue
            if now - last_seen < config.SETTLE_WINDOW_SEC:
                continue

            job = Job.create(source_path=str(entry), source_mtime=_safe_mtime(entry))
            if config.AUTO_START_PROCESSING:
                job.append_log("Detected in inbox; auto-starting (AUTO_START_PROCESSING=true).")
                job.status = Job.STATUS_QUEUED
                job.touch_and_save()
                start_job(job.id)
            else:
                job.append_log("Detected in inbox; waiting for confirmation to start.")

            with lock:
                activity.pop(entry, None)


def start_watcher():
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    activity: dict = {}
    lock = threading.Lock()
    stop_event = threading.Event()

    observer = Observer()
    observer.schedule(_ActivityHandler(activity, lock), str(config.INBOX_DIR), recursive=True)
    observer.start()

    checker = threading.Thread(
        target=_settle_checker_loop, args=(activity, lock, stop_event), daemon=True
    )
    checker.start()

    return observer, stop_event
