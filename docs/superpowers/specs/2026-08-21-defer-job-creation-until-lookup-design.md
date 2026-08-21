# Defer Job creation until "Find this book" is clicked

Status: approved by Brady, ready for an implementation plan.

## Problem

Today, the watcher creates a `Job` row (status `pending`) the instant a
top-level inbox entry settles — before the user has made any decision about
it. If that file is then moved or deleted directly through the filesystem
(not through the app), the `Job` row is left orphaned: the "Needs Input"
list keeps showing it, with no indication anything changed, until the user
clicks something and it fails with whatever error touching the missing
path happens to produce.

Separately, `app/pipeline/detect.py`'s `detect()` has no explicit handling
for a source path that no longer exists: it checks `is_file()` then
`is_dir()`, and if neither matches, falls off the end of the function and
implicitly returns `None`. The caller (`start_job` in `app/queue.py`) then
crashes on `None.source_type` with a raw `AttributeError`, which is caught
by `start_job`'s broad `except Exception` and does correctly mark the job
`failed` — but with a confusing raw Python error message instead of
something like "source no longer exists."

## Decision

Stop creating a `Job` row when a file settles in the inbox. Instead, the
watcher tracks settled-but-unclaimed entries in the same in-memory
structure it already uses for settle-window bookkeeping, and the web layer
renders them alongside real `Job` rows via a lightweight read-only
stand-in. A `Job` row only gets created at the moment "Find this book" is
clicked — immediately before enqueueing the Huey `start_job` task, which is
the earliest point real, worth-persisting state is about to be produced,
and the earliest point a value needs to cross the process boundary between
the web server and the separate Huey consumer process.

This was chosen over two more aggressive alternatives, both rejected:

- **Cache detection/search results in memory only, create the Job at
  confirm time instead of at "start" time.** Rejected: `start_job` is a
  Huey task (`@huey.task()` in `app/queue.py`), which runs in the Huey
  consumer — a completely separate OS process from the web server (see
  `app/main.py`'s module docstring and `ARCHITECTURE.md`'s "Processes"
  section). The two processes only ever coordinate through the shared
  SQLite DB; there is no in-memory or IPC channel between them. Detection
  results (title/author guess, metadata candidates, probed source
  duration, chapters preview) are produced in the consumer process and
  have to be persisted somewhere for the web process's next poll to see
  them, regardless of which lifecycle point creates the row. Moving that
  storage out of the DB and into some other cache doesn't reduce
  complexity, it relocates the same problem to a second storage mechanism
  — while losing the DB's free durability across tab closures and
  container restarts (which happen routinely - NAS reboots, redeploys).
- **Fix only `detect()`'s missing-path handling, without touching Job
  creation timing.** This is a real, independent bug (see Non-Goals - it's
  still worth fixing) but doesn't address the actual complaint: a `Job`
  that's never been acted on shouldn't be able to go stale at all. Fixing
  only the error message still leaves a phantom entry sitting in the UI
  until the user clicks it.

## Current architecture (context the implementer needs)

- `app/main.py`'s `lifespan` starts the watcher (`app/watcher.py`'s
  `start_watcher()`) as a background thread **inside the FastAPI process
  itself**. Only the Huey consumer that runs `start_job`/`process_job` is a
  separate OS process (`entrypoint.sh` launches both). This means the
  watcher's in-memory state is safely readable/writable by the web routes
  directly - no IPC needed for anything that stays within that boundary.
- `app/watcher.py`'s `_settle_checker_loop` maintains one dict, `activity:
  dict[Path, float]` (last-activity timestamp), protected by a
  `threading.Lock`. A `watchdog` `Observer` calls
  `_ActivityHandler.on_any_event` for *any* filesystem event under
  `INBOX_DIR` (create/modify/delete/move), which refreshes
  `activity[entry] = time.time()` for the affected top-level entry -
  including delete events, since `on_any_event` doesn't filter by event
  type. Every second, the checker loop snapshots `activity`, and for each
  entry: pops it if `not entry.exists()`; otherwise, once
  `now - last_seen >= SETTLE_WINDOW_SEC`, currently creates a `Job`. This
  existence check is what the redesign reuses for reconciliation - see
  below.
- `app/web/routes.py`'s `_board_context()` builds the `needs_input`,
  `converting`, `done` lists (from `Job.select()...`) shared by
  `index.html`, `_rail.html`, `_now_converting.html`, `_panel.html`.
  `_queue_item.html` (one rail row) and the `pending` branch of
  `_panel.html` were checked directly against this design: both derive
  their displayed title from `job.source_path.split('/')[-1]` when
  `job.title_guess` is falsy, and don't read anything else that isn't
  trivially available before detection has run. **No template changes are
  required** for the `pending` rendering path, confirmed by reading both
  files line-by-line.
- `POST /jobs/{job_id}/start` (`app/web/routes.py`) currently takes
  `job_id: int`, sets `status = QUEUED`, and calls `start_job(job_id)`
  (the Huey task).
- `queue.remove_job(job_id)` (real, persisted jobs) now runs
  `SOURCE_CLEANUP_MODE` cleanup via `archive.handle_source_cleanup` before
  setting `dismissed = True` (shipped in v0.2.5) - the pending-entry
  remove path added here should mirror that behavior, not reinvent it.
- `GET /api/status` returns `[{"id", "status", "progress_pct",
  "progress_stage"}, ...]` for every non-dismissed `Job`. `app.js`'s
  `pollStatus()` diffs the *set* of ids against `knownGroups` to decide
  when to call `refreshBoard()` - it already treats `"pending"` as a valid
  status (see `NEEDS_INPUT_STATUSES` in `app.js`, and
  `STATUS_RENDER_GROUP`/`renderGroup()` in the same file), so a
  newly-appeared pending entry is picked up by *existing* polling logic
  with no `app.js` changes, as long as it's included in this endpoint's
  response in the same shape.
- `app.js`'s action-button handler builds request URLs via plain string
  concatenation - `"/jobs/" + jobId + "/" + action"` - with **no
  URL-encoding**. Harmless today since job ids are always small integers.
  Not harmless once ids can be filesystem names (e.g. `The calamity Club`,
  which contains spaces). See "ID scheme" below for the fix - it does not
  require touching `app.js`.
- `app.js`'s `post()` throws when `res.ok` is false; the action-button
  click handler has no `.catch()`, so a failed request (e.g. a 404 because
  the entry vanished between render and click) silently no-ops from the
  user's perspective - the button re-enables, nothing else happens. The
  next natural `pollStatus()` tick (≤ ~1s later) will notice the
  structural change and refresh the rail on its own. This is acceptable
  for this design (see Non-Goals) but worth knowing going in.

## Design

### 1. Watcher: track unclaimed entries, don't auto-create Jobs

`_settle_checker_loop` keeps its existing settle-window logic completely
unchanged - the same `activity` dict, the same watchdog-driven
timestamp refresh, the same per-tick `not entry.exists()` pop. The only
behavioral change: once `now - last_seen >= SETTLE_WINDOW_SEC`, **do not**
call `Job.create()` and do **not** pop the entry from `activity`. Leave it
there. "Is it settled" becomes a computed property read by callers, not a
reason to stop tracking it.

Because the entry stays in `activity`, the *exact same* existence check
that already runs every tick keeps applying to it for as long as it sits
there unclaimed - so if the file is deleted or moved via the filesystem
after settling, the very next tick (≤ ~1s later) sees `not entry.exists()`
and pops it, with no new code required for that reconciliation. This was
verified against Brady's specific question about this scenario during
design and confirmed correct.

Add two small, lock-protected functions to `app/watcher.py`, callable from
`app/web/routes.py` (safe because both run in the same process - see
above):

- `list_pending() -> list[tuple[str, float]]` (or similar) - returns
  `(name, settled_since)` for every entry in `activity` where
  `now - last_seen >= SETTLE_WINDOW_SEC`, sorted by `settled_since`
  ascending (oldest first, matching current `Job.created_at` ordering for
  `needs_input`). Read-only; does not mutate `activity`.
- `claim_pending(name: str) -> Path | None` - acquires the lock, looks up
  the top-level entry `config.INBOX_DIR / name` in `activity`, and if
  present **and** still settled **and** `entry.exists()`, pops it and
  returns the `Path`. Returns `None` otherwise (already claimed by a
  concurrent request, vanished, or never was in `activity` to begin with -
  all three are the same "can't be claimed" outcome to the caller). This
  is the single atomic hand-off point between "watcher-owned, ephemeral"
  and "Job-owned, persisted" - both the start and remove actions for a
  pending entry go through it, and the lock is what makes it race-safe
  against the checker loop noticing a deletion at the same moment.

`_IGNORED_TOP_LEVEL_NAMES`/`_is_ignored_top_level_name` (the `.DS_Store` /
`@eaDir` filtering) are unaffected - they're applied before an entry is
ever added to `activity` at all, same as today.

### 2. ID scheme for unclaimed entries

An unclaimed entry's id is the string `"pending:" + name`, where `name` is
the entry's filename relative to `INBOX_DIR` (never contains `/`, since
these are always direct children of `INBOX_DIR`). This id is
**percent-encoded once, server-side**, wherever it's embedded into HTML
(`data-job-id`, `data-target`, the panel's `data-panel` attribute, the
`/api/status` JSON `id` field) - e.g. via Python's `urllib.parse.quote`.
FastAPI/Starlette path params are automatically URL-decoded on the way in,
so this round-trips correctly through `app.js`'s existing unencoded string
concatenation with **zero `app.js` changes required**. Use the encoded
form consistently everywhere an id is compared or embedded (e.g. the
`active_job_id` comparison in templates), not just in URLs.

Real (persisted) job ids remain plain integers, unchanged, rendered as
today (`str(job.id)`, no `pending:` prefix, nothing to encode since
integers never contain reserved characters).

### 3. Routes: widen `job_id` from `int` to `str`, branch on the prefix

`POST /jobs/{job_id}/start` and `POST /jobs/{job_id}/remove` (and any
other `/jobs/{job_id}/...` route reachable from a pending entry's rendered
buttons - check `_panel.html`'s `pending` branch for the full set; today
that's exactly `start` and `remove`) change their signature from
`job_id: int` to `job_id: str`, decode the leading `pending:` prefix if
present, and branch:

- **`pending:<name>` present**: url-decode `<name>` (FastAPI does this
  automatically for the path param, so this is just stripping the
  prefix), call `watcher.claim_pending(name)`. If `None`, return 404 (the
  entry vanished or was already claimed - this is a normal, expected
  outcome, not a server error). If a `Path` comes back:
  - **`start`**: `job = Job.create(source_path=str(entry),
    status=Job.STATUS_QUEUED)`, `job.touch_and_save()`, then
    `start_job(job.id)` exactly as the existing int-id path does after
    setting `status = QUEUED`. Return the same `{"ok": True}` shape.
  - **`remove`**: run the same cleanup `queue.remove_job` already does for
    persisted jobs - `archive.handle_source_cleanup(entry,
    log=<something reasonable, e.g. print or a no-op - there's no Job to
    append_log to>)`. No `Job` row is created. Return `{"ok": True}`.
- **No `pending:` prefix**: parse as `int` and fall through to the
  existing logic, unchanged. An unparseable id that's neither
  `pending:...` nor a valid int is a 404 (bad request), matching how
  `Job.get_or_none` already 404s today for an id that doesn't exist.

Consider factoring the shared "start a job" and "clean up per
SOURCE_CLEANUP_MODE" logic (used by both the pending-entry branch above
and the existing per-Job code in `app/queue.py`) into small shared
functions rather than duplicating the body inline in both branches -
implementer's judgment on the cleanest factoring, but avoid copy-pasting
the `Job.create`/`touch_and_save`/`start_job` sequence or the
`handle_source_cleanup` call twice.

### 4. `_board_context()`: merge pending entries into `needs_input`

Add a small read-only stand-in (a `dataclass` or similar; exact
implementation is the implementer's call) exposing the subset of `Job`'s
interface the `pending` branches of `_queue_item.html` and `_panel.html`
actually read - confirmed by direct inspection of both templates during
design, reproduced here so the implementer doesn't have to re-derive it:

```
id                 -> "pending:<url-quoted name>"
source_path        -> str(entry)  # templates do .split('/')[-1] on this
status             -> "pending"
created_at         -> the entry's settled_since timestamp (as a datetime;
                       _panel.html calls .strftime('%Y-%m-%d %H:%M') on it)
title_guess        -> None
author_guess       -> None
selected_metadata  -> None
candidates         -> []
dismissed          -> False   # if anything iterates all_jobs and checks this
progress_pct       -> 0       # only read for 'processing'/'ready' status,
                                 included for interface completeness
```

`_board_context()`'s `needs_input` becomes: real `Job` rows with
`dismissed == False and status in _NEEDS_INPUT_STATUSES` (unchanged query,
though note `Job.STATUS_PENDING` will now effectively never appear in this
query's results in practice, since real Jobs are never created with that
status anymore - harmless to leave the status constant and the query as
they are, no need to remove `STATUS_PENDING` from the enum or from
`_NEEDS_INPUT_STATUSES`), **plus** one stand-in per `watcher.list_pending()`
entry, merged and sorted by arrival time (`created_at` for real jobs,
`settled_since` for stand-ins) so pending items interleave naturally with
older failed/cancelled jobs rather than always sorting first or last.

`active_job` selection (`needs_input[0] if needs_input else ...`) and
`GET /fragments/panel/{job_id}` both need to accept the new id shape - the
latter's signature changes from `job_id: int` the same way the `/jobs/...`
routes do, and resolves either a real `Job.get_or_none` or a stand-in
built from `watcher.list_pending()` (read-only lookup by name, not a
claim - viewing the panel must not consume it).

### 5. `GET /api/status`: include pending entries

Extend the existing list comprehension to also emit one entry per
`watcher.list_pending()` result, in the same shape:
`{"id": "pending:<encoded name>", "status": "pending", "progress_pct": 0,
"progress_stage": None}`. This is what lets `app.js`'s existing
polling/structural-change detection notice a newly-settled file with zero
`app.js` changes (see "Current architecture" above for why this already
works without modification).

## Non-goals (explicitly out of scope for this change)

- Fixing `detect()`'s missing-path handling (raise a clear `DetectionError`
  instead of implicitly returning `None`) is a good, separate, small fix
  Brady also asked about - worth doing, but it's independent of this
  redesign and should be its own change (it still matters for a source
  that vanishes *after* becoming a real, persisted Job - e.g. between
  confirm and the dispatcher actually starting conversion - which this
  redesign doesn't address at all, by design, since that's squarely
  "already in the conversion queue" territory Brady wanted the DB to keep
  owning).
- Making `app.js`'s action-button handler show a visible error or force an
  immediate `refreshBoard()` on a failed (404) click is a nice-to-have,
  not required - the existing polling loop self-corrects within about a
  second regardless. Flag it to Brady as an easy follow-up if he wants it,
  but don't build it as part of this change.
- No database migration is needed for this feature - it removes a code
  path (watcher creating `Job` rows) rather than adding schema.

## Testing

- `app/watcher.py`: unit/integration coverage for `list_pending()` and
  `claim_pending()` - entries appear once settled and not before; a
  claimed entry is no longer returned by `list_pending()`; a claim on an
  already-claimed or nonexistent name returns `None`; a file
  deleted/moved after settling but before being claimed disappears from
  `list_pending()` within one checker tick (this is the core regression
  test for the original bug report - reproduce Brady's exact scenario:
  drop a file, let it settle, delete it via the filesystem, assert it's
  gone from the pending list without ever having created a `Job`).
- `app/web/routes.py`: `POST /jobs/pending:<name>/start` creates a `Job`
  with the right `source_path` and enqueues detection; `.../remove` cleans
  up per `SOURCE_CLEANUP_MODE` and creates no `Job`; both 404 on an
  already-claimed or vanished name; a name containing a space or other
  URL-reserved character round-trips correctly (this is the regression
  test for the encoding gotcha found during design - use a fixture name
  with a space, confirm the full click-through-encode-decode path works,
  not just the individual encode/decode functions in isolation).
- `_board_context()`/rendered fragments: a pending entry renders in
  `_rail.html` and as the default `_panel.html` view with the filename as
  its title, same as today's pre-redesign `pending`-status `Job` did
  (before/after comparison is a reasonable way to write this test -
  render both and diff, or just assert on the same key strings the
  existing pending-Job tests already check for, if any exist).
- `GET /api/status` includes a `pending:...` entry once a file settles,
  confirming the frontend polling path has what it needs without further
  `app.js` verification (that side is already covered by the "current
  architecture" analysis above, but end-to-end confirmation that the
  backend emits the right shape is still worth a test).

## Open items for the implementer to flag back if they hit them

- Confirm there is no other `/jobs/{job_id}/...` route besides `start` and
  `remove` reachable from a pending entry's rendered UI (re-check
  `_panel.html`'s full `pending` branch and `_queue_item.html` at
  implementation time, not just this doc, in case something changed).
- Confirm no other template beyond `_queue_item.html`/`_panel.html` reads
  a `Job`-specific attribute for a `pending`-status item that the
  stand-in doesn't provide (`index.html`/`_rail.html` were not directly
  read line-by-line during design - only `_queue_item.html` and
  `_panel.html` were).
