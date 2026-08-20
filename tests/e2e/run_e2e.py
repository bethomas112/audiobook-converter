#!/usr/bin/env python3
"""Drives a real Docker Compose deployment of this tool through the full
manual review workflow via real HTTP calls - the same actions a user
would take in the browser - against a real audiobook you supply, then
verifies the result. See README.md in this directory for setup and for
the SQLite WAL finding this script's compose file already works around.

Usage:
    docker compose -f docker-compose.e2e.yml up -d --build
    python3 run_e2e.py "/path/to/a/real/audiobook/folder-or-file"

Job state is read via `docker exec ... python3 -c ...` against the
container's own sqlite3 module (the image doesn't ship the sqlite3 CLI,
and CONFIG_DIR is a named volume, not host-path-addressable) rather than
scraping rendered HTML - the actions themselves go through the real HTTP
API; this is just the verification mechanism.
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "http://localhost:8001"
CONTAINER = "audiobook-converter-e2e"
E2E_DIR = Path(__file__).resolve().parent
DATA_DIR = E2E_DIR / "data"

_PY_QUERY = """
import sqlite3, json, sys
conn = sqlite3.connect('/data/config/app.db')
conn.row_factory = sqlite3.Row
cur = conn.execute(sys.argv[1])
print(json.dumps([dict(r) for r in cur.fetchall()]))
"""


def db_query(sql):
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "python3", "-c", _PY_QUERY, sql],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"db_query failed: {result.stderr}")
    return json.loads(result.stdout)


def http(method, path, data=None):
    url = BASE_URL + path
    body, headers = None, {}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode()


def get_job_row(job_id):
    rows = db_query(f"SELECT * FROM job WHERE id = {job_id}")
    return rows[0] if rows else None


def wait_for(predicate, timeout, interval=3, desc=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"Timed out waiting for: {desc} (last check: {last})")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} /path/to/a/real/audiobook/folder-or-file", file=sys.stderr)
        sys.exit(2)
    source_book = Path(sys.argv[1]).expanduser().resolve()
    if not source_book.exists():
        print(f"No such file/folder: {source_book}", file=sys.stderr)
        sys.exit(2)

    log(f"=== Copying {source_book.name} into the inbox ===")
    dest = DATA_DIR / "inbox" / source_book.name
    if dest.exists():
        shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
    if source_book.is_dir():
        shutil.copytree(source_book, dest)
    else:
        shutil.copy2(source_book, dest)
    log(f"Copied to {dest}")

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

    def detection_done():
        j = get_job_row(job_id)
        return j if j["status"] in ("awaiting_metadata_confirm", "failed") else None

    job = wait_for(detection_done, timeout=120, interval=2, desc="detection + metadata search")
    log(f"Post-detection status: {job['status']}")
    log("--- job log so far ---\n" + job["log"])
    if job["status"] == "failed":
        log(f"DETECTION/METADATA SEARCH FAILED: {job['error_message']}")
        sys.exit(1)

    candidates = json.loads(job["candidates_json"] or "[]")
    log(f"Found {len(candidates)} metadata candidate(s):")
    for c in candidates[:8]:
        log(f"  - [{c.get('asin')}] {c.get('title')!r} by {c.get('author')!r} ({c.get('year')})")

    chosen = candidates[0] if candidates else {
        "asin": "", "title": source_book.stem, "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    log(f"Confirming candidate: {chosen.get('title')!r} by {chosen.get('author')!r}")

    log("=== POST /jobs/{id}/confirm ===")
    status, body = http("POST", f"/jobs/{job_id}/confirm", data={k: v for k, v in chosen.items()})
    log(f"POST confirm -> {status} {body}")
    assert status == 200

    log("=== Waiting for conversion to complete ===")
    start_time = time.time()

    def conversion_done():
        j = get_job_row(job_id)
        elapsed = time.time() - start_time
        log(f"  [{elapsed:6.0f}s] status={j['status']:10s} progress={j['progress_pct']:3d}% stage={j['progress_stage']}")
        return j if j["status"] in ("done", "failed", "cancelled") else None

    job = wait_for(conversion_done, timeout=3600, interval=20, desc="conversion completion")
    log(f"\n=== FINAL STATUS: {job['status']} (took {time.time() - start_time:.0f}s) ===")
    log(f"Destination: {job['destination_path']}")
    log("--- full job log ---\n" + job["log"])

    if job["status"] != "done":
        log("E2E RUN FAILED")
        sys.exit(1)
    log("E2E run completed successfully.")


if __name__ == "__main__":
    main()
