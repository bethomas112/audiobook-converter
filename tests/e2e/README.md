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

## Why CONFIG_DIR is a named volume here too

While building this test, a *bind-mounted* `CONFIG_DIR` reliably produced
`sqlite3.OperationalError: disk I/O error` right after container start on
Docker Desktop for Mac, which doesn't self-heal on its own. Switching
`CONFIG_DIR` to a named volume made it disappear completely - this
directory's `docker-compose.e2e.yml` uses one for `config_data`, matching
the same fix the shipped top-level `docker-compose.yml` now uses too.

See the main [README.md](../../README.md#why-config_dir-is-a-named-docker-volume-sqlite-disk-io-error)
for the full writeup (symptom, cause, and the backup command a named
volume needs instead of a plain `cp`) - not duplicated here to avoid the
two drifting out of sync.
