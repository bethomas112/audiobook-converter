"""Test bootstrap.

Everything in app/config.py, app/db.py, and app/queue.py does real work at
*import* time: config.py reads os.environ, db.py opens a SQLite file at
CONFIG_DIR/app.db, and queue.py calls config.ensure_dirs()/init_db() and
constructs a SqliteHuey bound to CONFIG_DIR/huey.db. So the *_DIR env vars
must be pointed at a scratch directory before any test module does
`import app...` for the first time - hence this all happens at module level
here, in conftest.py, which pytest always imports before collecting tests.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_SESSION_BASE = Path(tempfile.mkdtemp(prefix="abp-test-"))
for _sub in ("inbox", "work", "archive", "output", "config"):
    (_SESSION_BASE / _sub).mkdir(parents=True, exist_ok=True)

os.environ["INBOX_DIR"] = str(_SESSION_BASE / "inbox")
os.environ["WORK_DIR"] = str(_SESSION_BASE / "work")
os.environ["ARCHIVE_DIR"] = str(_SESSION_BASE / "archive")
os.environ["OUTPUT_DIR"] = str(_SESSION_BASE / "output")
os.environ["CONFIG_DIR"] = str(_SESSION_BASE / "config")
# Deliberately not set: tests that care about a specific value monkeypatch
# `config` directly rather than relying on env/defaults.

import pytest  # noqa: E402

from app.config import config  # noqa: E402
from app.db import Job, db, init_db  # noqa: E402

init_db()


def pytest_sessionfinish(session, exitstatus):
    db.close()
    shutil.rmtree(_SESSION_BASE, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_job_table():
    """Every test starts with an empty Job table, so job-related tests never
    see leftover rows from a previous test.
    """
    Job.delete().execute()
    yield
    Job.delete().execute()


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Point INBOX/WORK/ARCHIVE/OUTPUT at a fresh directory tree for this
    test only (CONFIG_DIR/the Job DB stays on the shared session DB - see
    _clean_job_table for how that's kept test-isolated instead).
    """
    dirs = {}
    for attr, name in (
        ("INBOX_DIR", "inbox"),
        ("WORK_DIR", "work"),
        ("ARCHIVE_DIR", "archive"),
        ("OUTPUT_DIR", "output"),
    ):
        d = tmp_path / name
        d.mkdir()
        monkeypatch.setattr(config, attr, d)
        dirs[name] = d
    return dirs


@pytest.fixture
def huey_immediate():
    """Runs @huey.task()-decorated calls synchronously in-process instead of
    enqueuing them for a separate consumer - lets integration tests exercise
    the real start_job/process_job task bodies without a live Huey consumer
    process. See app/queue.py for the tasks this affects.
    """
    from app.queue import huey

    original = huey.immediate
    huey.immediate = True
    yield huey
    huey.immediate = original
