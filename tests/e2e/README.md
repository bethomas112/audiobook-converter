# End-to-end test

Drives a real Docker Compose deployment of this tool through the full
manual review workflow, against a real audiobook you supply, entirely
over HTTP - the same actions a user takes in the browser. This is
separate from `pytest` (marked `e2e`, excluded by default in
`pytest.ini`) because it needs Docker, real network access to
Audible/audnexus, and a real audiobook file - none of which belong in
the regular fast test run.

## Running it

You need a real audiobook (an M4B, an MP3, or a folder of MP3s) to test
with - none is bundled here.

```bash
cd tests/e2e
docker compose -f docker-compose.e2e.yml up -d --build
python3 run_e2e.py "/path/to/your/audiobook"
```

The script copies your book into a scratch inbox, waits for the watcher
to pick it up, calls `/start`, picks the top metadata candidate (or
falls back to a bare-minimum manual entry if the search finds nothing),
calls `/confirm`, and polls until the job finishes - printing the full
job log either way. Everything lands under `tests/e2e/data/` (gitignored)
and the source book's folder name, so re-running with the same book
resumes cleanly rather than re-copying.

Tear down when done:

```bash
docker compose -f docker-compose.e2e.yml down -v
```

## Known issue: SQLite WAL + bind-mounted CONFIG_DIR

While building this test, a bind-mounted `CONFIG_DIR` (what the shipped
top-level `docker-compose.yml` uses) reliably produced
`sqlite3.OperationalError: disk I/O error` right after container start,
on Docker Desktop for Mac - both the web process and the Huey consumer
open a WAL-mode connection to `app.db` at startup, and the host↔VM
filesystem-sharing layer (virtiofs) didn't handle that cleanly. Once hit,
every request 500'd and it did **not** self-heal - the process stays
alive so `restart: unless-stopped` never kicks in, and only a manual
`docker restart` recovered it.

Switching `CONFIG_DIR` to a named volume made it disappear completely,
which is why `docker-compose.e2e.yml` here uses one for `config_data`
instead of a bind mount. The top-level `docker-compose.yml` intentionally
keeps every `*_DIR` as a plain bind mount for simplicity (no Docker
volume management for the deployer to think about), so this fix wasn't
carried over there - it's noted here as a real risk to watch for in
production. If `disk I/O error` ever shows up in real container logs,
switching `CONFIG_DIR` to a named volume is a one-line fix.
