# Defer Job Creation Until Lookup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop creating a `Job` DB row the instant a file settles in the inbox; instead track unclaimed, settled entries purely in the watcher's existing in-memory state, and only create a `Job` at the moment "Find this book" is clicked (or `AUTO_START_PROCESSING` claims it automatically). This eliminates the class of bug where a file moved/deleted directly through the filesystem leaves an orphaned, stale `Job` row with no reconciliation.

**Architecture:** `app/watcher.py`'s settle-tracking dict becomes module-level state, exposed via two new functions (`list_pending()`, `claim_pending(name)`) that `app/web/routes.py` calls directly — safe because the watcher already runs as a background thread inside the same FastAPI process, not the separate Huey consumer process. A read-only `PendingEntry` stand-in mimics the subset of `Job`'s interface the `pending`-state templates read, so `_board_context()` can merge unclaimed filesystem entries into `needs_input` alongside real `Job` rows with no template changes. Both entry kinds share one URL scheme (`/jobs/{job_id}/...`) by widening `job_id` from `int` to `str` and branching on a `pending:<url-quoted name>` prefix.

**Tech Stack:** Python 3.12, FastAPI, Peewee/SQLite, Jinja2, watchdog, Huey, pytest.

**Spec:** [docs/superpowers/specs/2026-08-21-defer-job-creation-until-lookup-design.md](../specs/2026-08-21-defer-job-creation-until-lookup-design.md)

## Global Constraints

- No database migration - this plan removes a code path, adds none. Do not touch `app/db.py`'s `Job` model or `_add_missing_columns()`.
- `app.js` requires **zero changes**. If a task seems to need one, stop and re-check the id-encoding scheme (Task 4) before concluding `app.js` must change - it almost certainly doesn't.
- Every existing test that passes today on `main` must still pass after this plan, except the `app/watcher.py` tests explicitly rewritten in Task 2 (which test the *old*, now-removed "watcher creates a Job on settle" behavior and must be replaced, not merely patched).
- Run `source .venv/bin/activate` once per shell before any `pytest`/`python` command in this repo.

---

## File Structure

| File | Responsibility after this plan |
|---|---|
| `app/queue.py` | Add `start_new_job(source_path)` - the single place a `Job` first comes into existence. |
| `app/watcher.py` | Settle-window tracking (unchanged logic) exposed as module-level state; `PendingEntry` dataclass; `list_pending()`/`claim_pending()`; stops creating `Job` rows itself except via `start_new_job()` under `AUTO_START_PROCESSING`. |
| `app/web/routes.py` | `_board_context()` merges `list_pending()` into `needs_input`; `/fragments/panel/{job_id}`, `/jobs/{job_id}/start`, `/jobs/{job_id}/remove`, `/api/status` all widen `job_id` to `str` and branch on the `pending:` prefix. |
| `tests/integration/test_watcher.py` | Rewritten to assert against `list_pending()`/`claim_pending()` instead of `Job.select()`. |
| `tests/unit/test_queue_start_new_job.py` (new) | Unit coverage for `start_new_job()`. |
| `tests/integration/test_pending_routes.py` (new) | Route-level coverage for the `pending:` branch of `/jobs/{id}/start`, `/jobs/{id}/remove`, `/fragments/panel/{id}`, `/api/status`. |
| `ARCHITECTURE.md` | Job-lifecycle state diagram, the `dismissed`-field paragraph, and the Needs-Input table updated to reflect that `pending` is no longer a real `Job` status. |
| `tests/e2e/run_e2e.py` | Updated to claim a pending entry via the API instead of polling the DB directly for a `pending`-status row that will no longer exist. |

---

### Task 1: `queue.start_new_job()` - the single Job-creation entry point

**Files:**
- Modify: `app/queue.py`
- Test: `tests/unit/test_queue_start_new_job.py` (new)

**Interfaces:**
- Produces: `start_new_job(source_path: Path) -> Job`, importable as `from app.queue import start_new_job`. Creates a `Job` with `status=Job.STATUS_QUEUED`, logs one line, saves, and enqueues the `start_job` Huey task via `start_job(job.id)`. Returns the created `Job`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_queue_start_new_job.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_queue_start_new_job.py -v`
Expected: FAIL - `AttributeError: module 'app.queue' has no attribute 'start_new_job'`

- [ ] **Step 3: Write the implementation**

In `app/queue.py`, add this function directly above `def remove_job(job_id: int):` (search for that exact line to locate the insertion point):

```python
def start_new_job(source_path: Path) -> Job:
    """Creates a Job for a freshly-claimed inbox entry and immediately
    enqueues detection - the single place a Job first comes into being,
    whether triggered by a user's "Find this book" click (see
    app/web/routes.py's pending-entry branch of the /start route) or by
    AUTO_START_PROCESSING claiming a settled entry automatically the
    moment it settles (see app/watcher.py's _settle_checker_loop).
    """
    job = Job.create(source_path=str(source_path), status=Job.STATUS_QUEUED)
    job.append_log("Detected in inbox; waiting for confirmation to start.")
    job.touch_and_save()
    start_job(job.id)
    return job
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_queue_start_new_job.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/queue.py tests/unit/test_queue_start_new_job.py
git commit -m "Add start_new_job(): the single place a Job first comes into existence"
```

---

### Task 2: Watcher - module-level state, `PendingEntry`, `list_pending()`, `claim_pending()`

This is the core of the plan and the largest task. `app/watcher.py`'s `activity` dict and `Lock` move from locals closed over by `start_watcher()` to module-level globals, so `list_pending()`/`claim_pending()` (called from `app/web/routes.py`, a different module in the *same* process - see the spec's "Current architecture" section for why that's safe) can reach them. The settle-window *logic* itself does not change at all - only what happens once something settles.

**Files:**
- Modify: `app/watcher.py` (near-total rewrite of internals; behavior for anything *before* settling is unchanged)
- Modify: `tests/integration/test_watcher.py` (rewrite every test that currently asserts on `Job.select()` after settling)

**Interfaces:**
- Consumes: `start_new_job(source_path: Path) -> Job` from `app.queue` (Task 1).
- Produces:
  - `PendingEntry` dataclass with fields `id: str, source_path: str, created_at: datetime, status: str = "pending", title_guess: str | None = None, author_guess: str | None = None, selected_metadata: dict | None = None, candidates: list = field(default_factory=list), dismissed: bool = False, progress_pct: int = 0, progress_stage: str | None = None`.
  - `list_pending() -> list[PendingEntry]` - every settled, unclaimed entry, oldest-first.
  - `claim_pending(name: str) -> Path | None` - atomically removes and returns the entry, or `None` if it can't be claimed.

- [ ] **Step 1: Write the failing tests for `list_pending()`/`claim_pending()`**

Add these to `tests/integration/test_watcher.py` (keep the existing imports at the top; add `from app.watcher import claim_pending, list_pending, start_watcher` alongside the existing `from app.watcher import start_watcher`):

```python
def test_settled_entry_appears_in_list_pending_not_as_a_job(isolated_dirs, running_watcher):
    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"x" * 1000)

    def one_pending():
        pending = list_pending()
        return pending if len(pending) == 1 else None

    pending = _wait_until_value(one_pending, timeout=5.0)
    assert pending[0].source_path == str(isolated_dirs["inbox"] / "book.mp3")
    assert pending[0].id == "pending:book.mp3"
    assert Job.select().count() == 0  # no Job created just from settling


def test_deleting_settled_entry_removes_it_from_list_pending(isolated_dirs, running_watcher):
    """The exact scenario Brady reported: a file removed from the inbox
    through the filesystem, before "Find this book" was ever clicked,
    must disappear from what the UI shows - with no Job ever having
    existed to go stale.
    """
    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 1000)
    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)

    f.unlink()

    assert _wait_until(lambda: len(list_pending()) == 0, timeout=5.0)
    assert Job.select().count() == 0


def test_claim_pending_removes_entry_and_returns_its_path(isolated_dirs, running_watcher):
    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 1000)
    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)

    claimed = claim_pending("book.mp3")

    assert claimed == f
    assert list_pending() == []


def test_claim_pending_returns_none_for_unsettled_or_unknown_name(isolated_dirs, running_watcher):
    assert claim_pending("nope.mp3") is None

    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 1000)
    time.sleep(0.1)  # well under the 0.4s settle window set by running_watcher
    assert claim_pending("book.mp3") is None  # not settled yet


def test_claim_pending_is_not_reclaimable(isolated_dirs, running_watcher):
    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 1000)
    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)

    assert claim_pending("book.mp3") == f
    assert claim_pending("book.mp3") is None


def _wait_until_value(get_value, timeout=5.0, interval=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = get_value()
        if value is not None:
            return value
        time.sleep(interval)
    return None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_watcher.py -k "list_pending or claim_pending" -v`
Expected: FAIL - `ImportError: cannot import name 'claim_pending' from 'app.watcher'`

- [ ] **Step 3: Rewrite `app/watcher.py`**

Replace the entire file with:

```python
"""Watches INBOX_DIR for new top-level entries (a single audio file, or a
folder of them) and tracks when each one has stopped changing for
SETTLE_WINDOW_SEC — so partial downloads/copies aren't picked up
mid-write.

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

        with _lock:
            snapshot = list(_activity.items())

        for entry, last_seen in snapshot:
            if not entry.exists():
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
```

Note the `AUTO_START_PROCESSING` branch re-checks `_activity.get(entry) == last_seen` under the lock before claiming - this guards against a race where a filesystem event refreshed the entry's timestamp between the snapshot and this point (which would mean it's no longer actually settled by the freshest information).

- [ ] **Step 4: Update the rest of `tests/integration/test_watcher.py`**

Every remaining test in this file currently asserts `Job.select().count() == N` (or similar) once an entry settles - that assertion is now wrong, since settling no longer creates a `Job`. Read through the *entire* current file and, for each existing test, replace `Job.select()...` assertions about a *settled, not-yet-clicked* entry with `list_pending()` assertions instead. Tests that exercise `AUTO_START_PROCESSING` keep asserting on `Job.select()`, since that path still creates a real `Job` (via `start_new_job`, Task 1) - the only thing that changed there is `Job.append_log`'s message no longer distinguishes "auto-starting" vs "waiting for confirmation" (it's a single fixed message now - see Task 1's `start_new_job`), so if any test asserts on that exact log string, update the expected string to `"Detected in inbox; waiting for confirmation to start."`.

Concretely, rewrite these tests (same names, same intent, new assertions):

```python
def test_drop_in_not_queued_before_settle_window_elapses(isolated_dirs, running_watcher):
    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"x" * 1000)

    time.sleep(0.1)  # well under the 0.4s settle window
    assert list_pending() == []


def test_drop_in_appears_in_pending_after_settle_window_elapses(isolated_dirs, running_watcher):
    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 1000)

    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)
    assert list_pending()[0].source_path == str(f)


def test_file_still_being_written_resets_settle_timer(isolated_dirs, running_watcher):
    f = isolated_dirs["inbox"] / "book.mp3"
    f.write_bytes(b"x" * 100)

    end = time.time() + 1.2
    while time.time() < end:
        with open(f, "ab") as fh:
            fh.write(b"y" * 100)
        time.sleep(0.15)

    assert list_pending() == []
    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)


def test_folder_drop_in_waits_for_all_files_to_settle(isolated_dirs, running_watcher):
    folder = isolated_dirs["inbox"] / "Multi Book"
    folder.mkdir()
    (folder / "track1.mp3").write_bytes(b"x" * 100)

    time.sleep(0.2)
    (folder / "track2.mp3").write_bytes(b"y" * 100)  # resets the folder's settle timer

    time.sleep(0.1)
    assert list_pending() == []

    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)
    assert list_pending()[0].source_path == str(folder)
```

Delete `test_source_with_existing_job_is_not_requeued` entirely - it tested the old always-blocks-on-path dedup, which is unrelated to this change (that dedup logic lives in `_known_source_paths()` and is untouched by this plan - it only ever applied to entries that already have a `Job`, which pending entries by definition don't).

`test_removed_source_path_is_freed_for_a_new_drop_off` is also unrelated to this task's change (it exercises `queue_mod.remove_job` on an existing `Job`, not the pending-entry path) - leave it exactly as-is.

Rewrite the dotfile/`@eaDir` tests to check `list_pending()` instead of `Job.select()`:

```python
def test_dotfile_drop_in_is_never_pending(isolated_dirs, running_watcher):
    (isolated_dirs["inbox"] / ".DS_Store").write_bytes(b"x" * 100)

    time.sleep(1.0)
    assert list_pending() == []

    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"y" * 1000)
    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)
    assert list_pending()[0].source_path == str(isolated_dirs["inbox"] / "book.mp3")


def test_synology_eadir_drop_in_is_never_pending(isolated_dirs, running_watcher):
    (isolated_dirs["inbox"] / "@eaDir").mkdir()
    (isolated_dirs["inbox"] / "@eaDir" / "SYNOINDEX_THUMB_ME.txt").write_bytes(b"x" * 100)

    time.sleep(1.0)
    assert list_pending() == []

    (isolated_dirs["inbox"] / "book.mp3").write_bytes(b"y" * 1000)
    assert _wait_until(lambda: len(list_pending()) == 1, timeout=5.0)
    assert list_pending()[0].source_path == str(isolated_dirs["inbox"] / "book.mp3")


def test_preexisting_dotfile_in_inbox_is_never_pending(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.4)
    monkeypatch.setattr(config, "AUTO_START_PROCESSING", False)

    (isolated_dirs["inbox"] / ".DS_Store").write_bytes(b"x" * 100)

    observer, stop_event = start_watcher()
    try:
        time.sleep(1.0)
        assert list_pending() == []
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)


def test_preexisting_synology_eadir_in_inbox_is_never_pending(isolated_dirs, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.4)
    monkeypatch.setattr(config, "AUTO_START_PROCESSING", False)

    (isolated_dirs["inbox"] / "@eaDir").mkdir()

    observer, stop_event = start_watcher()
    try:
        time.sleep(1.0)
        assert list_pending() == []
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)
```

`test_auto_start_processing_queues_and_starts_immediately` keeps asserting on `Job.select()` (AUTO_START_PROCESSING still creates a real Job) - no change needed there, but double check it still passes given `start_new_job`'s fixed log message.

Update the module's top imports: add `from app.watcher import claim_pending, list_pending, start_watcher` (replacing the old `from app.watcher import start_watcher` line).

- [ ] **Step 5: Run the full watcher test file**

Run: `pytest tests/integration/test_watcher.py -v`
Expected: PASS (every test in the file)

- [ ] **Step 6: Run the full suite to check for fallout**

Run: `pytest -q`
Expected: failures only in `tests/integration/test_web_routes.py` and `tests/e2e/` (both addressed in later tasks) - everything else passes. If anything else fails, stop and investigate before continuing; it means something depends on watcher internals not accounted for in this plan.

- [ ] **Step 7: Commit**

```bash
git add app/watcher.py tests/integration/test_watcher.py
git commit -m "Stop creating a Job when an inbox entry settles; track it as PendingEntry until claimed"
```

---

### Task 3: `_board_context()` merges pending entries; `/fragments/panel/{job_id}` widened

**Files:**
- Modify: `app/web/routes.py`

**Interfaces:**
- Consumes: `list_pending() -> list[PendingEntry]` from `app.watcher` (Task 2).
- Produces: `_board_context()`'s `needs_input` now contains a mix of `Job` and `PendingEntry` objects; `GET /fragments/panel/{job_id}` accepts either a real int id or a `pending:<name>` id.

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_web_routes.py` (check the existing file's imports/fixtures first - it almost certainly already has a `client` fixture; match its style):

```python
def test_pending_entry_appears_in_rail_and_index(client, isolated_dirs, monkeypatch):
    from app.config import config
    from app.watcher import start_watcher

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.1)
    observer, stop_event = start_watcher()
    try:
        (isolated_dirs["inbox"] / "Some Book.mp3").write_bytes(b"x" * 100)
        import time

        deadline = time.time() + 5.0
        found = False
        while time.time() < deadline:
            resp = client.get("/")
            if "Some Book.mp3" in resp.text:
                found = True
                break
            time.sleep(0.1)
        assert found, "pending entry never appeared on the index page"
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)


def test_panel_fragment_for_pending_entry(client, isolated_dirs, monkeypatch):
    from app.config import config
    from app.watcher import claim_pending

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.1)
    (isolated_dirs["inbox"] / "Some Book.mp3").write_bytes(b"x" * 100)
    import time

    time.sleep(0.3)
    from app.watcher import list_pending

    pending = list_pending()
    assert len(pending) == 1
    resp = client.get(f"/fragments/panel/{pending[0].id}")
    assert resp.status_code == 200
    assert "Some Book.mp3" in resp.text
    # Viewing the panel must not consume the entry.
    assert claim_pending("Some Book.mp3") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_web_routes.py -k "pending_entry or panel_fragment_for_pending" -v`
Expected: FAIL - `test_panel_fragment_for_pending_entry` fails with a 422 (FastAPI rejects a non-integer `job_id` today); `test_pending_entry_appears_in_rail_and_index` fails because the entry never renders (not merged into `needs_input` yet).

- [ ] **Step 3: Implement**

In `app/web/routes.py`, find `_board_context()` (search for `def _board_context`). Add the import at the top of the file, alongside the other `app.watcher`/`app.queue` imports:

```python
from app.watcher import list_pending
```

Change the `needs_input` assignment inside `_board_context()` from:

```python
    needs_input = list(
        Job.select()
        .where(Job.dismissed == False, Job.status.in_(_NEEDS_INPUT_STATUSES))  # noqa: E712
        .order_by(Job.created_at)
    )
```

to:

```python
    needs_input_jobs = list(
        Job.select()
        .where(Job.dismissed == False, Job.status.in_(_NEEDS_INPUT_STATUSES))  # noqa: E712
        .order_by(Job.created_at)
    )
    needs_input = sorted(needs_input_jobs + list_pending(), key=lambda j: j.created_at)
```

Find `fragment_panel` (search for `def fragment_panel`) and replace it entirely:

```python
@router.get("/fragments/panel/{job_id}")
def fragment_panel(request: Request, job_id: str, _=Depends(require_auth)):
    job = _resolve_board_item(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    ctx = _board_context(request)
    ctx["job"] = job
    return templates.TemplateResponse("_panel.html", ctx)
```

Add `_resolve_board_item` directly above `_board_context` (search for `def _board_context` to find the insertion point):

```python
def _resolve_board_item(job_id: str):
    """Looks up either a real Job (plain integer id) or a not-yet-claimed
    PendingEntry ("pending:<url-quoted name>" id) for read-only display -
    does not claim/consume a pending entry. Returns None if neither
    resolves.
    """
    if job_id.startswith("pending:"):
        return next((entry for entry in list_pending() if entry.id == job_id), None)
    try:
        numeric_id = int(job_id)
    except ValueError:
        return None
    return Job.get_or_none(Job.id == numeric_id)
```

Add `import urllib.parse` to the top of `app/web/routes.py` alongside the other stdlib imports (check what's already imported first - likely `secrets` is already there; add `urllib.parse` near it).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_web_routes.py -k "pending_entry or panel_fragment_for_pending" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: same failures as after Task 2 (route-level `start`/`remove` for pending ids not implemented yet - Task 4), everything else still passes.

- [ ] **Step 6: Commit**

```bash
git add app/web/routes.py tests/integration/test_web_routes.py
git commit -m "Merge pending inbox entries into the board; widen panel fragment route"
```

---

### Task 4: `/jobs/{job_id}/start` and `/jobs/{job_id}/remove` - widen and branch on `pending:`

**Files:**
- Modify: `app/web/routes.py`
- Test: `tests/integration/test_pending_routes.py` (new)

**Interfaces:**
- Consumes: `claim_pending(name) -> Path | None` (Task 2), `start_new_job(source_path) -> Job` (Task 1), `archive.handle_source_cleanup` (already imported in `app/queue.py`; import it fresh in `routes.py` too).
- Produces: `POST /jobs/{job_id}/start` and `POST /jobs/{job_id}/remove` both accept `pending:<name>` ids. The `start` response body gains a `job_id` field (the real, newly-created integer id) - additive, `app.js` only checks `res.ok` today so this is backward compatible; `tests/e2e/run_e2e.py` (Task 6) needs it to continue polling the right row.

- [ ] **Step 1: Write the failing tests**

Create `tests/integration/test_pending_routes.py`:

```python
"""Integration tests for the pending:<name> branch of /jobs/{id}/start and
/jobs/{id}/remove - see docs/superpowers/specs/2026-08-21-defer-job-
creation-until-lookup-design.md.
"""
import time

from app.db import Job
from app.watcher import list_pending, start_watcher


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
```

Check `tests/integration/test_web_routes.py` for how its `client` fixture is defined/imported (it's used there already) and make sure `test_pending_routes.py` has access to the same fixture - if it's defined in a shared `conftest.py` under `tests/integration/`, nothing further is needed; if it's local to `test_web_routes.py`, move it to `tests/integration/conftest.py` so both files can use it (check first; don't duplicate it if you don't have to).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/integration/test_pending_routes.py -v`
Expected: FAIL - `pending_id`-shaped ids get rejected by FastAPI's `int` path converter (422), or `AttributeError`/`KeyError` on `job_id` in the response body.

- [ ] **Step 3: Implement**

In `app/web/routes.py`, replace the existing `start` route:

```python
@router.post("/jobs/{job_id}/start")
def start(job_id: str, _=Depends(require_auth)):
    if job_id.startswith("pending:"):
        name = urllib.parse.unquote(job_id[len("pending:"):])
        entry = claim_pending(name)
        if entry is None:
            raise HTTPException(status_code=404)
        job = start_new_job(entry)
        return JSONResponse({"ok": True, "job_id": job.id})

    try:
        numeric_id = int(job_id)
    except ValueError:
        raise HTTPException(status_code=404)
    job = Job.get_or_none(Job.id == numeric_id)
    if job is None:
        raise HTTPException(status_code=404)
    job.status = Job.STATUS_QUEUED
    job.touch_and_save()
    start_job(numeric_id)
    return JSONResponse({"ok": True, "job_id": job.id})
```

Replace the existing `remove` route:

```python
@router.post("/jobs/{job_id}/remove")
def remove(job_id: str, _=Depends(require_auth)):
    if job_id.startswith("pending:"):
        name = urllib.parse.unquote(job_id[len("pending:"):])
        entry = claim_pending(name)
        if entry is None:
            raise HTTPException(status_code=404)
        archive.handle_source_cleanup(entry, log=print)
        return JSONResponse({"ok": True})

    try:
        numeric_id = int(job_id)
    except ValueError:
        raise HTTPException(status_code=404)
    if Job.get_or_none(Job.id == numeric_id) is None:
        raise HTTPException(status_code=404)
    remove_job(numeric_id)
    return JSONResponse({"ok": True})
```

Add the needed imports at the top of `app/web/routes.py` (check what's already imported before adding - `archive` and `claim_pending`/`start_new_job` may need new import lines; `urllib.parse` was already added in Task 3):

```python
from app.pipeline import archive
from app.queue import start_new_job
from app.watcher import claim_pending
```

(If `archive` or similar names are already imported under a different alias, adjust the call sites above to match rather than introducing a duplicate import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/integration/test_pending_routes.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all tests pass now except `tests/e2e/` (not run by default - see `pytest.ini`'s `addopts`; confirm nothing outside `tests/e2e/` fails).

- [ ] **Step 6: Commit**

```bash
git add app/web/routes.py tests/integration/test_pending_routes.py
git commit -m "Widen /jobs/{id}/start and /remove to claim pending: entries"
```

---

### Task 5: `GET /api/status` includes pending entries

**Files:**
- Modify: `app/web/routes.py`
- Test: `tests/integration/test_pending_routes.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/integration/test_pending_routes.py`:

```python
def test_api_status_includes_pending_entry(client, isolated_dirs, monkeypatch):
    pending_id = _settle_and_get_pending_id(isolated_dirs, "book.mp3", monkeypatch)

    resp = client.get("/api/status")

    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert pending_id in ids
    row = next(row for row in resp.json() if row["id"] == pending_id)
    assert row["status"] == "pending"
    assert row["progress_pct"] == 0
    assert row["progress_stage"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/integration/test_pending_routes.py -k api_status_includes_pending -v`
Expected: FAIL - pending id missing from the response.

- [ ] **Step 3: Implement**

Find `def api_status` in `app/web/routes.py` and change:

```python
@router.get("/api/status")
def api_status(_=Depends(require_auth)):
    jobs = Job.select().where(Job.dismissed == False)  # noqa: E712
    return JSONResponse(
        [
            {
                "id": j.id,
                "status": j.status,
                "progress_pct": j.progress_pct,
                "progress_stage": j.progress_stage,
            }
            for j in jobs
        ]
    )
```

to:

```python
@router.get("/api/status")
def api_status(_=Depends(require_auth)):
    jobs = Job.select().where(Job.dismissed == False)  # noqa: E712
    rows = [
        {
            "id": j.id,
            "status": j.status,
            "progress_pct": j.progress_pct,
            "progress_stage": j.progress_stage,
        }
        for j in jobs
    ]
    rows += [
        {
            "id": entry.id,
            "status": entry.status,
            "progress_pct": entry.progress_pct,
            "progress_stage": entry.progress_stage,
        }
        for entry in list_pending()
    ]
    return JSONResponse(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/integration/test_pending_routes.py -k api_status_includes_pending -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: all pass (excluding `tests/e2e/`, as before).

- [ ] **Step 6: Commit**

```bash
git add app/web/routes.py tests/integration/test_pending_routes.py
git commit -m "Include pending inbox entries in /api/status"
```

---

### Task 6: Documentation - `ARCHITECTURE.md`, `README.md`, `tests/e2e/run_e2e.py`

This task exists because the user explicitly asked for correct documentation changes alongside the code, not as an afterthought - treat it with the same rigor as the code tasks, including verifying the e2e script actually works against the new behavior (or at minimum is internally consistent and correct by inspection, since it's excluded from the default test run and may not be runnable in this environment - see `tests/e2e/README.md` for what it needs).

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `README.md` (verify only - see Step 3)
- Modify: `tests/e2e/run_e2e.py`

- [ ] **Step 1: Update `ARCHITECTURE.md`'s state diagram and surrounding prose**

Find the `## Job lifecycle` section (search for that exact heading). Replace the mermaid diagram:

```
    [*] --> pending
    pending --> queued: "Find this book" / AUTO_START_PROCESSING
```

with:

```
    [*] --> queued: "Find this book" / AUTO_START_PROCESSING claims a settled inbox entry
```

(Every other transition in the diagram - `queued --> detecting` through `failed --> [*]` - is unchanged; only the entry point moves, since a `Job` row now never exists in `pending` status. Leave the rest of the diagram exactly as it is.)

Immediately after the diagram (before the `A separate dismissed boolean...` paragraph), add:

```markdown
Before a `Job` row exists at all, a settled-but-unclaimed inbox entry is
tracked purely in memory by `app/watcher.py` (`list_pending()`,
`claim_pending()`) and rendered as a `PendingEntry` stand-in - see that
module's docstring. A `Job` only comes into existence at the moment
something claims the entry (`app/queue.py`'s `start_new_job()`), whether
that's a user clicking "Find this book" or `AUTO_START_PROCESSING`
claiming it automatically. This means a file moved or deleted directly
through the filesystem before being claimed is never orphaned: there's no
row to go stale, since nothing was ever persisted for it.
```

Replace the `dismissed`-boolean paragraph (currently: `A separate dismissed boolean (not a status) can be set on a job in almost any state, to hide it from the UI without deleting its row — app/db.py's comment on that field explains why deleting outright would be a real bug (the watcher would re-detect the source file as new).`) with:

```markdown
A separate `dismissed` boolean (not a status) can be set on a job in
almost any state, to hide it from the UI without deleting its row -
`app/db.py`'s comment on that field explains why. This only matters for a
job that's already been claimed (see above) - an unclaimed entry has no
row to dismiss; removing one before it's ever looked up just cleans up
its source file directly (`app/web/routes.py`'s `pending:` branch of
`/jobs/{id}/remove`) with nothing left to hide.
```

Update the Needs-Input row of the statuses table:

```
| Needs Input | `pending`, `queued`, `detecting`, `awaiting_metadata_confirm`, `failed`, `cancelled` |
```

to:

```
| Needs Input | unclaimed `PendingEntry` stand-ins (rendered with status `pending`, but not a real `Job` status - see above), `queued`, `detecting`, `awaiting_metadata_confirm`, `failed`, `cancelled` |
```

- [ ] **Step 2: Check the "Processes" section for anything else that needs updating**

Re-read `ARCHITECTURE.md`'s `## Processes` section (lines ~8-56) in full. It already correctly describes the watcher running inside the web process - that doesn't change. Confirm nothing in it references the watcher creating `Job` rows directly (it shouldn't; the current text describes cross-process coordination generically). No change expected here, but verify by reading it rather than assuming.

- [ ] **Step 3: Check `README.md` for anything describing pending/drop-off behavior**

Search `README.md` for `pending`, `Needs Input`, `watcher`, and the "Drop-off" step of its "What it does" numbered list (step 1). This section describes user-facing behavior ("drop an audiobook... it shows up in the Needs Input queue as 'Waiting'") which is still accurate from a user's perspective after this change - nothing about what the user *sees* changes, only internal implementation. Confirm this by reading the relevant section; if it says anything implying a database row or persistence detail that's no longer true, fix it. If (as expected) it's already implementation-agnostic, no change is needed - state that explicitly in the commit message rather than silently skipping it.

- [ ] **Step 4: Update `tests/e2e/run_e2e.py`**

This script currently finds the watcher-created `pending` `Job` row by querying the DB directly, then POSTs to `/jobs/{id}/start`. Under the new design there's no `Job` row to find until something claims the entry. Find this block (search for `Waiting for the watcher to detect + settle the drop-off`):

```python
    log("=== Waiting for the watcher to detect + settle the drop-off ===")

    def find_job():
        rows = db_query(f"SELECT * FROM job WHERE source_path LIKE '%{source_book.name}%' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    job = wait_for(find_job, timeout=90, desc="job row created by watcher")
    job_id = job["id"]
    log(f"Job {job_id}: status={job['status']}")

    if job["status"] == "pending":
        log("=== POST /jobs/{id}/start ===")
        status, body = http("POST", f"/jobs/{job_id}/start")
        log(f"POST start -> {status} {body}")
        assert status == 200
```

Replace it with:

```python
    log("=== Waiting for the watcher to detect + settle the drop-off ===")

    def find_pending():
        status, body = http("GET", "/api/status")
        assert status == 200
        for row in body:
            if isinstance(row["id"], str) and row["id"].startswith("pending:") and source_book.name in row["id"]:
                return row
        return None

    pending_row = wait_for(find_pending, timeout=90, desc="pending entry reported by the watcher")
    log(f"Pending entry: id={pending_row['id']}")

    log("=== POST /jobs/{pending-id}/start ===")
    status, body = http("POST", f"/jobs/{pending_row['id']}/start")
    log(f"POST start -> {status} {body}")
    assert status == 200
    job_id = body["job_id"]
    log(f"Claimed as Job {job_id}")
```

Check `http()`'s existing implementation in this file (search for `def http`) to confirm it already returns the parsed JSON body as a Python object (not a raw string) - the code above assumes `body` is a dict for the `POST` response and a list of dicts for the `GET /api/status` response, matching every other use of `http()` already in this file. If `http()` instead returns a raw string, adjust the snippet above to `json.loads(body)` accordingly (check the top of the file for whether `json` is already imported).

Re-read the rest of `run_e2e.py` after this point (the `detection_done()` polling and everything after it) to confirm nothing else references the old `job["status"] == "pending"` check or assumes a `Job` row existed before the `POST start` call - fix anything that does.

- [ ] **Step 5: Commit**

```bash
git add ARCHITECTURE.md README.md tests/e2e/run_e2e.py
git commit -m "Update docs and e2e script for deferred Job creation"
```

(If Step 3 concluded no `README.md` change was needed, `git add README.md` will simply have nothing to stage - that's fine, don't force a change to justify the task.)

---

### Task 7: Full verification pass

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: every test passes (the default `pytest.ini` `addopts` already excludes `tests/e2e/` - see `tests/e2e/README.md` for how to run that one manually if Docker and a real audiobook file are available in this environment; if not, skip running it but do re-read `run_e2e.py` once more end-to-end to sanity-check the Task 6 changes by inspection).

- [ ] **Step 2: Re-read the spec's Testing section**

Open `docs/superpowers/specs/2026-08-21-defer-job-creation-until-lookup-design.md` and re-read the `## Testing` section. Confirm every bullet point there is covered by a test written in Tasks 1-5 above. If anything is missing, add it now, following the same TDD step pattern (failing test, implement/fix, passing test, commit) as the other tasks.

- [ ] **Step 3: Re-read the spec's "Open items for the implementer" section**

The spec explicitly flagged two things to verify at implementation time rather than design time:
1. Confirm there is no other `/jobs/{job_id}/...` route besides `start` and `remove` reachable from a pending entry's rendered UI. Re-check `app/web/templates/_panel.html`'s `pending` branch (the `{% if job.status == 'pending' %}` block) as it exists *now*, in the actual file, not just as quoted in the spec.
2. Confirm no other template beyond `_queue_item.html`/`_panel.html` reads a `Job`-specific attribute for a `pending`-status item that `PendingEntry` doesn't provide. Check `index.html` and `_rail.html` directly (these were not read line-by-line during design).

If either check finds something the plan didn't account for, fix it now and add a test for it before considering this plan complete.

- [ ] **Step 4: Final commit if Step 2 or 3 produced changes**

```bash
git add -A
git commit -m "Close out remaining spec-coverage gaps found during final verification"
```

(Skip this step entirely if Steps 2-3 found nothing to fix.)

---

## Execution Handoff

This plan will be executed by a single dispatched agent working through all seven tasks sequentially in one session, committing after each task as specified above. The user will review the resulting commits afterward rather than reviewing between tasks.
