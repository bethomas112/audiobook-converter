"""Watches INBOX_DIR for new top-level entries (a single audio file, or a
folder of them) and tracks when each one has stopped changing for
SETTLE_WINDOW_SEC — so partial downloads/copies aren't picked up mid-write.

watchdog supplies the filesystem events (which reset an entry's "last
activity" timestamp); a lightweight polling loop does the actual settle-
window bookkeeping. Once an entry settles, it does NOT automatically
become a Job - it just becomes visible to list_pending() below. A Job
only comes into existence when something actually claims the entry: a
user clicking "Find this book" (app/web/routes.py) or, if
AUTO_START_PROCESSING is set, this module claiming it for itself the
moment it settles. Both paths go through app.queue.start_new_job() -
that's the one place a Job first exists (see its own docstring).

_activity and _lock are module-level (not local to start_watcher()) so
list_pending()/claim_pending() can be called from app/web/routes.py - a
different module, but the SAME OS process. The watcher runs as a
background thread inside the FastAPI process itself (see app/main.py's
lifespan); only the Huey consumer that runs start_job/process_job is a
separate process. See ARCHITECTURE.md's "Processes" section.
"""
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.config import config
from app.db import Job
from app.queue import start_new_job

# NAS-generated system directories that can appear inside a bind-mounted
# inbox alongside dotfiles - never a real drop-off, so ignored the same way.
# @eaDir is Synology DSM's thumbnail/index cache, auto-created in every
# folder on a DSM volume (including a folder mounted into this container).
_IGNORED_TOP_LEVEL_NAMES = {"@eaDir"}


def _is_ignored_top_level_name(name: str) -> bool:
    return name.startswith(".") or name in _IGNORED_TOP_LEVEL_NAMES


# module-level so routes.py can reach the same state the watcher thread
# uses - see the module docstring.
_activity: dict = {}
_lock = threading.Lock()


@dataclass
class PendingEntry:
    """Read-only stand-in for a Job, representing a settled inbox entry
    with no Job row yet. Exposes exactly the subset of Job's interface
    the 'pending' branches of app/web/templates/_queue_item.html and
    _panel.html read - derived by reading both files directly; see the
    design doc's "Section 4" for the walkthrough.
    """

    id: str
    source_path: str
    created_at: datetime
    status: str = "pending"
    title_guess: str | None = None
    author_guess: str | None = None
    selected_metadata: dict | None = None
    candidates: list = field(default_factory=list)
    dismissed: bool = False
    progress_pct: int = 0
    progress_stage: str | None = None


def _pending_id(name: str) -> str:
    return "pending:" + urllib.parse.quote(name, safe="")


class _ActivityHandler(FileSystemEventHandler):
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
            # permanently-stuck, unclaimable entry (see
            # _IGNORED_TOP_LEVEL_NAMES above for the NAS-specific case).
            return None
        return config.INBOX_DIR / top_name

    def on_any_event(self, event):
        entry = self._top_level_entry(event.src_path)
        if entry is not None:
            with _lock:
                _activity[entry] = time.time()


def _known_source_paths() -> set:
    """Every historical job's source_path that still has something sitting
    at it right now, so a kept (non-cleaned-up) source in the inbox isn't
    re-surfaced as a pending entry forever. Checking existence live -
    rather than trusting Job.source_path to have been kept in sync with
    wherever SOURCE_CLEANUP_MODE cleanup (remove_job() and process_job()'s
    completion cleanup, both in app/queue.py) actually moved or deleted a
    source to - covers all three cleanup modes uniformly: a `keep`d
    source is still there (still blocks, unchanged from before cleanup-
    aware dedup existed); a `delete`d or `archive`d one no longer is
    (stops blocking), freeing that path for a genuinely new, unrelated
    drop-off with the same name, without needing every code path that
    moves or deletes a source to remember to update source_path correctly.

    This still matters after Jobs are created lazily (see module
    docstring): a completed job whose source was `keep`t sits in the
    inbox indefinitely, and would otherwise settle and reappear as a
    phantom PendingEntry - e.g. after a container restart, when the
    initial iterdir() scan below re-discovers it.
    """
    return {p for j in Job.select(Job.source_path) if (p := Path(j.source_path)).exists()}


def list_pending() -> list:
    """Every settled, unclaimed inbox entry, oldest-first (matching
    Job.created_at ordering for real jobs in _board_context()). Read-only
    - does not remove anything from tracking.
    """
    now = time.time()
    with _lock:
        snapshot = list(_activity.items())
    settled = [(entry, ts) for entry, ts in snapshot if now - ts >= config.SETTLE_WINDOW_SEC]
    settled.sort(key=lambda pair: pair[1])
    return [
        PendingEntry(id=_pending_id(entry.name), source_path=str(entry), created_at=datetime.utcfromtimestamp(ts))
        for entry, ts in settled
    ]


def claim_pending(name: str):
    """Atomically removes a settled entry from tracking and returns its
    Path, or None if it can't be claimed - already claimed by a
    concurrent request, not settled yet, or gone. This is the only place
    an entry moves from watcher-owned/ephemeral to Job-owned/persisted;
    the lock is what makes it safe against the checker loop noticing a
    deletion at the same moment.
    """
    entry = config.INBOX_DIR / name
    now = time.time()
    with _lock:
        last_seen = _activity.get(entry)
        if last_seen is None or now - last_seen < config.SETTLE_WINDOW_SEC:
            return None
        if not entry.exists():
            _activity.pop(entry, None)
            return None
        _activity.pop(entry, None)
        return entry


def _settle_checker_loop(stop_event: threading.Event):
    with _lock:
        for entry in config.INBOX_DIR.iterdir():
            if _is_ignored_top_level_name(entry.name):
                continue
            _activity.setdefault(entry, time.time())

    while not stop_event.is_set():
        stop_event.wait(1)
        now = time.time()
        known = _known_source_paths()

        with _lock:
            snapshot = list(_activity.items())

        for entry, last_seen in snapshot:
            if not entry.exists():
                with _lock:
                    _activity.pop(entry, None)
                continue
            if entry in known:
                # Already has a live Job (e.g. a SOURCE_CLEANUP_MODE=keep
                # completed job whose source is still sitting here) -
                # never surface it as a pending entry. See
                # _known_source_paths()'s docstring.
                with _lock:
                    _activity.pop(entry, None)
                continue
            if now - last_seen < config.SETTLE_WINDOW_SEC:
                continue
            if not config.AUTO_START_PROCESSING:
                # Settled and unclaimed - leave it in _activity so
                # list_pending() reports it and the existence check above
                # keeps applying to it every tick for as long as it sits
                # here (this is what makes a later filesystem deletion
                # self-correct with no extra code - see the design doc).
                continue

            # AUTO_START_PROCESSING: claim it for ourselves the moment it
            # settles, exactly like claim_pending() does, since there's no
            # user click to trigger the claim.
            with _lock:
                still_there = _activity.get(entry) == last_seen
                if still_there:
                    _activity.pop(entry, None)
            if still_there and entry.exists():
                start_new_job(entry)


def start_watcher():
    config.INBOX_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        _activity.clear()
    stop_event = threading.Event()

    observer = Observer()
    observer.schedule(_ActivityHandler(), str(config.INBOX_DIR), recursive=True)
    observer.start()

    checker = threading.Thread(target=_settle_checker_loop, args=(stop_event,), daemon=True)
    checker.start()

    return observer, stop_event
