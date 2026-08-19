"""Integration tests for app/web/routes.py's HTTP layer, via FastAPI's
TestClient against a minimal app built from just the router (skipping
app.main's lifespan/watcher-thread startup, which isn't needed here and
would add an unrelated real Observer thread to every test).
"""
import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import Job
from app.web.routes import router


@pytest.fixture
def client(isolated_dirs):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_index_renders_ok(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_index_shows_needs_input_job(client):
    Job.create(source_path="/x", title_guess="My Book", status=Job.STATUS_PENDING)
    resp = client.get("/")
    assert "My Book" in resp.text or "/x" in resp.text


def test_fragment_rail_renders_ok(client):
    resp = client.get("/fragments/rail")
    assert resp.status_code == 200


def test_fragment_now_converting_renders_ok(client):
    resp = client.get("/fragments/now-converting")
    assert resp.status_code == 200


def test_fragment_panel_for_existing_job(client):
    job = Job.create(source_path="/x", status=Job.STATUS_PENDING)
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200


def test_fragment_panel_for_missing_job_is_404(client):
    resp = client.get("/fragments/panel/99999")
    assert resp.status_code == 404


def test_api_status_shape(client):
    job = Job.create(source_path="/x", status=Job.STATUS_PROCESSING, progress_pct=42, progress_stage="Encoding")
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body == [{"id": job.id, "status": "processing", "progress_pct": 42, "progress_stage": "Encoding"}]


def test_api_status_excludes_dismissed_jobs(client):
    Job.create(source_path="/x", status=Job.STATUS_DONE, dismissed=True)
    resp = client.get("/api/status")
    assert resp.json() == []


def test_start_endpoint_transitions_to_queued_and_runs_detection(client, monkeypatch, huey_immediate):
    from app.pipeline import metadata as metadata_mod
    from tests.helpers import make_tone_mp3

    monkeypatch.setattr(metadata_mod, "search", lambda *a, **k: [])
    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin: [])

    from app.config import config
    source = config.INBOX_DIR / "book.mp3"
    make_tone_mp3(source, duration_sec=0.3)
    job = Job.create(source_path=str(source), status=Job.STATUS_PENDING)

    resp = client.post(f"/jobs/{job.id}/start")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_AWAITING_METADATA_CONFIRM


def test_start_endpoint_404_for_missing_job(client):
    resp = client.post("/jobs/99999/start")
    assert resp.status_code == 404


def test_confirm_endpoint_stages_metadata_and_queues_job(client):
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM)

    resp = client.post(
        f"/jobs/{job.id}/confirm",
        data={"title": "Confirmed Title", "author": "Confirmed Author"},
    )
    assert resp.status_code == 200

    job = Job.get_by_id(job.id)
    assert job.selected_metadata["title"] == "Confirmed Title"
    assert job.selected_metadata["author"] == "Confirmed Author"
    assert job.status in (Job.STATUS_READY, Job.STATUS_PROCESSING)


def test_confirm_endpoint_manual_entry_with_no_candidates_works(client):
    """Planning doc Section 2 step 6: manual entry must work even when no
    metadata candidates matched - the form fields are the only source of
    truth for what gets confirmed either way.
    """
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM, candidates_json="[]")
    resp = client.post(f"/jobs/{job.id}/confirm", data={"title": "Hand Typed Title"})
    assert resp.status_code == 200
    assert Job.get_by_id(job.id).selected_metadata["title"] == "Hand Typed Title"


def test_cancel_endpoint(client):
    job = Job.create(source_path="/x", status=Job.STATUS_READY, queue_order=1)
    resp = client.post(f"/jobs/{job.id}/cancel")
    assert resp.status_code == 200
    assert Job.get_by_id(job.id).status == Job.STATUS_CANCELLED


def test_requeue_endpoint(client):
    """Since nothing else is processing, dispatch_next() picks this job up
    immediately once it's requeued - "ready" is a transient state here, not
    the final one (see tests/integration/test_queue_dispatcher.py for
    dispatcher behavior in isolation).
    """
    job = Job.create(source_path="/x", status=Job.STATUS_CANCELLED, selected_metadata_json='{"title": "T"}')
    resp = client.post(f"/jobs/{job.id}/requeue")
    assert resp.status_code == 200
    assert Job.get_by_id(job.id).status == Job.STATUS_PROCESSING


def test_remove_endpoint(client):
    job = Job.create(source_path="/x", status=Job.STATUS_DONE)
    resp = client.post(f"/jobs/{job.id}/remove")
    assert resp.status_code == 200
    assert Job.get_by_id(job.id).dismissed is True


def test_reorder_endpoint_invalid_direction_is_400(client):
    job = Job.create(source_path="/x", status=Job.STATUS_READY, queue_order=1)
    resp = client.post(f"/jobs/{job.id}/reorder", data={"direction": "sideways"})
    assert resp.status_code == 400


def test_reorder_endpoint_valid(client):
    job1 = Job.create(source_path="/a", status=Job.STATUS_READY, queue_order=1)
    job2 = Job.create(source_path="/b", status=Job.STATUS_READY, queue_order=2)
    resp = client.post(f"/jobs/{job2.id}/reorder", data={"direction": "up"})
    assert resp.status_code == 200
    assert Job.get_by_id(job2.id).queue_order == 1
    assert Job.get_by_id(job1.id).queue_order == 2


class TestBasicAuth:
    @pytest.fixture(autouse=True)
    def _basic_auth_enabled(self, monkeypatch):
        from app.config import config

        monkeypatch.setattr(config, "WEB_UI_AUTH", "basic")
        monkeypatch.setattr(config, "WEB_UI_USERNAME", "brady")
        monkeypatch.setattr(config, "WEB_UI_PASSWORD", "s3cret")

    def test_no_credentials_is_401(self, client):
        resp = client.get("/")
        assert resp.status_code == 401

    def test_wrong_credentials_is_401(self, client):
        resp = client.get("/", auth=("brady", "wrong"))
        assert resp.status_code == 401

    def test_correct_credentials_is_200(self, client):
        resp = client.get("/", auth=("brady", "s3cret"))
        assert resp.status_code == 200

    def test_api_status_also_requires_auth(self, client):
        resp = client.get("/api/status")
        assert resp.status_code == 401
