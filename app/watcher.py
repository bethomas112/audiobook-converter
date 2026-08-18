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
        return config.INBOX_DIR / rel.parts[0] if rel.parts else None

    def on_any_event(self, event):
        entry = self._top_level_entry(event.src_path)
        if entry is not None:
            with self._lock:
                self._activity[entry] = time.time()


def _known_source_paths() -> set:
    """All source paths that have ever had a Job created for them, so a
    kept (non-cleaned-up) source in the inbox isn't re-queued forever.
    """
    return {Path(j.source_path) for j in Job.select(Job.source_path)}


def _settle_checker_loop(activity: dict, lock: threading.Lock, stop_event: threading.Event):
    with lock:
        for entry in config.INBOX_DIR.iterdir():
            activity.setdefault(entry, time.time())

    while not stop_event.is_set():
        stop_event.wait(1)
        now = time.time()
        known = _known_source_paths()

        with lock:
            snapshot = list(activity.items())

        for entry, last_seen in snapshot:
            if not entry.exists():
                with lock:
                    activity.pop(entry, None)
                continue
            if entry in known:
                with lock:
                    activity.pop(entry, None)
                continue
            if now - last_seen < config.SETTLE_WINDOW_SEC:
                continue

            job = Job.create(source_path=str(entry))
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
