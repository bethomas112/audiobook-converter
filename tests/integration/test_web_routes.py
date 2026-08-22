"""Integration tests for app/web/routes.py's HTTP layer, via FastAPI's
TestClient against a minimal app built from just the router (skipping
app.main's lifespan/watcher-thread startup, which isn't needed here and
would add an unrelated real Observer thread to every test).
"""
import base64

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.db import Job
from app.web.routes import router

# The `client` fixture lives in tests/integration/conftest.py (shared with
# test_pending_routes.py).


def test_static_favicon_is_served():
    # Mirrors app.main's `app.mount("/static", ...)` directly - that mount
    # doesn't depend on the lifespan (init_db + watcher thread), so it's
    # easy to exercise standalone without pulling in the rest of main.py.
    app = FastAPI()
    app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
    resp = TestClient(app).get("/static/favicon.svg")
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]


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


def test_fragment_panel_awaiting_confirm_shows_select_the_book_label(client):
    """Regression guard for the candidate-picker section label copy: it
    should read "Select the Book", not the older "Which one is this?"
    wording.
    """
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM)
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert "Select the Book" in resp.text
    assert "Which one is this?" not in resp.text


def test_fragment_panel_confirm_form_carries_candidate_asin(client):
    """Regression test for the confirm form silently dropping asin: the
    server-rendered confirm form (app/web/templates/_panel.html) must embed
    the top candidate's asin in a submittable field, since app.js's
    candidate-click handler only ever overwrites fields that already exist
    in the form. A form with a title/author input but no asin field would
    make the bug in test_confirm_endpoint_persists_asin unreachable from the
    real UI even if the route itself handles asin correctly.
    """
    job = Job.create(
        source_path="/x",
        status=Job.STATUS_AWAITING_METADATA_CONFIRM,
        candidates_json='[{"title": "The Calamity Club", "author": "A. Author", "asin": "B0123456789"}]',
    )
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert 'name="asin" value="B0123456789"' in resp.text


def test_candidates_list_offers_none_of_these_option_when_candidates_exist(client):
    """Enhancement: when real search results exist but none of them are
    right, the reviewer needs a way to say so explicitly rather than either
    picking a wrong match or hand-editing over whatever the first result
    left in the form. The option should only appear alongside real
    candidates - the empty-candidates case already gets a "no candidate
    chosen" confirm form for free (see _panel.html's c0 = {} fallback), so
    there's nothing for it to do there.
    """
    job = Job.create(
        source_path="/x",
        status=Job.STATUS_AWAITING_METADATA_CONFIRM,
        title_guess="My Book",
        author_guess="Some Author",
        candidates_json='[{"title": "The Calamity Club", "author": "A. Author", "asin": "B0123456789"}]',
    )
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert 'class="candidate candidate-none"' in resp.text
    assert "None of these" in resp.text
    # app.js needs the job's title/author guesses to reset the confirm form
    # to when this option is clicked, since it can't read Jinja context.
    assert 'data-title-guess="My Book"' in resp.text
    assert 'data-author-guess="Some Author"' in resp.text


def test_candidates_list_omits_none_of_these_option_when_no_candidates(client):
    """The empty-candidates-list branch already renders a plain "no matches"
    note with no cards at all, and its confirm form already defaults to the
    no-candidate-chosen state via _panel.html's c0 = {} fallback - so the
    "None of these" row (which exists to opt out of a real, wrong match)
    would be a redundant, confusing extra click here.
    """
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM, candidates_json="[]")
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert "candidate-none" not in resp.text
    assert "No candidate matches found" in resp.text


@pytest.mark.parametrize("status", [Job.STATUS_AWAITING_METADATA_CONFIRM, Job.STATUS_PROCESSING])
def test_fragment_panel_chapter_preview_includes_all_chapters(client, status):
    """Regression test for the "N more chapters" line in _chapters.html
    (included by both the awaiting_metadata_confirm and processing branches
    of _panel.html) being a dead end: the full chapter list must be present
    in the rendered HTML - just past the first 6, inside the nested
    <details class="chapters-more"> - so the "more chapters" control has
    something to actually reveal instead of the count being pure decoration.
    """
    chapters = [{"start_sec": i * 60, "title": f"Chapter {i}"} for i in range(1, 10)]
    job = Job.create(source_path="/x", status=status, chapters_preview=chapters)
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    for chapter in chapters:
        assert chapter["title"] in resp.text
    assert 'class="chapters-more"' in resp.text


@pytest.mark.parametrize("status", [Job.STATUS_AWAITING_METADATA_CONFIRM, Job.STATUS_PROCESSING])
def test_fragment_panel_chapter_more_control_has_collapse_markup(client, status):
    """Regression test for the "N more chapters" summary text staying
    visible (and misleading) after it's been expanded, with no way to
    collapse back to the 6-chapter view except that same stale line.

    The fix is CSS-only (style.css flexes <details class="chapters-more">
    and reorders its <summary> to the bottom when [open], swapping the
    "more chapters" text for a lone caret) - a TestClient render can't
    execute CSS, so this test only checks that the markup both states
    depend on is actually present in the rendered HTML:
    - .chapter-more-text: the "N more chapters" copy, hidden via CSS once
      <details class="chapters-more"> is [open].
    - .chapter-more-collapse: the caret, hidden by default and shown via
      CSS only when [open], which doubles as the collapse control since
      it's part of the same <summary> that toggles the <details>.
    It does NOT verify the actual expand/collapse visual behavior, the
    flex reordering, or click behavior - that requires a real browser.
    """
    chapters = [{"start_sec": i * 60, "title": f"Chapter {i}"} for i in range(1, 10)]
    job = Job.create(source_path="/x", status=status, chapters_preview=chapters)
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert 'class="chapter-more-text"' in resp.text
    assert 'class="chapter-more-collapse"' in resp.text


@pytest.mark.parametrize("status", [Job.STATUS_AWAITING_METADATA_CONFIRM, Job.STATUS_PROCESSING])
def test_fragment_panel_chapter_timestamps_use_hms_past_one_hour(client, status):
    """Regression test for chapter timestamps in _chapters.html always
    rendering as an unbounded m:ss (e.g. a chapter starting 14h in showed
    "842:00" - 842 total minutes - rather than anything resembling a clock),
    which is hard to read for long audiobooks. Chapters starting at or past
    the 1-hour mark must render as h:mm:ss (zero-padded minutes/seconds),
    while chapters under an hour keep the original m:ss rendering.
    """
    chapters = [
        {"start_sec": 125, "title": "Early Chapter"},  # 2:05
        {"start_sec": 5025, "title": "Late Chapter"},  # 1:23:45
    ]
    job = Job.create(source_path="/x", status=status, chapters_preview=chapters)
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert "2:05" in resp.text
    assert "1:23:45" in resp.text


def test_fragment_panel_shows_candidate_and_source_durations_side_by_side(client):
    """Enhancement: runtime/duration must be visible during metadata review,
    not just discoverable after conversion - so a mismatched edition (e.g.
    an abridged audiobook candidate matched against an unabridged source)
    can be spotted at a glance. Both numbers must appear in the same
    rendered panel: the candidate's official runtime (_candidates.html) and
    the source's actual probed duration (_panel.html).
    """
    job = Job.create(
        source_path="/x",
        status=Job.STATUS_AWAITING_METADATA_CONFIRM,
        source_duration_sec=28 * 3600,  # a 28-hour unabridged source...
        candidates_json='[{"title": "The Calamity Club", "author": "A. Author", '
        '"asin": "B0123456789", "runtime_minutes": 480}]',  # ...matched against an 8-hour candidate
    )
    resp = client.get(f"/fragments/panel/{job.id}")
    assert resp.status_code == 200
    assert "source runs 28h" in resp.text
    assert "8h" in resp.text


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


def test_api_summary_counts_match_board_groups(client):
    Job.create(source_path="/a", status=Job.STATUS_PENDING)
    Job.create(source_path="/b", status=Job.STATUS_AWAITING_METADATA_CONFIRM)
    Job.create(source_path="/c", status=Job.STATUS_READY, queue_order=1)
    Job.create(source_path="/d", status=Job.STATUS_DONE)
    Job.create(source_path="/e", status=Job.STATUS_DONE)

    resp = client.get("/api/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_input"] == 2
    assert body["converting"] == 1
    assert body["done"] == 2


def test_api_summary_excludes_dismissed_jobs(client):
    Job.create(source_path="/a", status=Job.STATUS_DONE, dismissed=True)
    resp = client.get("/api/summary")
    body = resp.json()
    assert body["needs_input"] == 0
    assert body["converting"] == 0
    assert body["done"] == 0


def test_api_summary_counts_pending_watcher_entries_as_needs_input(client, isolated_dirs, monkeypatch):
    from app.config import config
    from app.watcher import claim_pending, start_watcher

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.1)
    observer, stop_event = start_watcher()
    try:
        (isolated_dirs["inbox"] / "Some Book.mp3").write_bytes(b"x" * 100)
        import time

        deadline = time.time() + 5.0
        needs_input = 0
        while time.time() < deadline:
            needs_input = client.get("/api/summary").json()["needs_input"]
            if needs_input == 1:
                break
            time.sleep(0.1)
        assert needs_input == 1
    finally:
        claim_pending("Some Book.mp3")
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)


def test_api_summary_current_is_null_when_nothing_processing(client):
    Job.create(source_path="/a", status=Job.STATUS_READY, queue_order=1)
    resp = client.get("/api/summary")
    assert resp.json()["current"] is None


def test_api_summary_current_reflects_processing_job(client):
    Job.create(
        source_path="/a",
        status=Job.STATUS_PROCESSING,
        progress_pct=42,
        progress_stage="Transcoding audio",
        selected_metadata_json='{"title": "Project Hail Mary"}',
    )
    resp = client.get("/api/summary")
    assert resp.json()["current"] == {
        "title": "Project Hail Mary",
        "progress_pct": 42,
        "stage": "Transcoding audio",
    }


def test_api_summary_current_falls_back_to_title_guess(client):
    Job.create(
        source_path="/a",
        status=Job.STATUS_PROCESSING,
        title_guess="Untitled Guess",
        progress_pct=10,
    )
    resp = client.get("/api/summary")
    current = resp.json()["current"]
    assert current["title"] == "Untitled Guess"
    assert current["stage"] is None


def test_api_summary_also_requires_auth(client, monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "WEB_UI_AUTH", "basic")
    monkeypatch.setattr(config, "WEB_UI_USERNAME", "brady")
    monkeypatch.setattr(config, "WEB_UI_PASSWORD", "s3cret")
    resp = client.get("/api/summary")
    assert resp.status_code == 401


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
    # job_id is additive (see the pending: branch of this route, which
    # needs it to report the newly-created Job's real id) - app.js only
    # checks res.ok, so this stays backward compatible.
    assert resp.json() == {"ok": True, "job_id": job.id}

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


def test_confirm_endpoint_persists_asin(client):
    """Regression test: the confirm form must actually carry the selected
    candidate's asin through to job.selected_metadata. Audnexus chapter
    lookup in resolve_chapters() only fires `if asin:`, so a lost asin here
    silently degrades every MP3 job to filename-based chapter titles instead
    of Audible's real chapter list - see app/pipeline/chapters.py.
    """
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM)

    resp = client.post(
        f"/jobs/{job.id}/confirm",
        data={"title": "Confirmed Title", "author": "Confirmed Author", "asin": "B0123456789"},
    )
    assert resp.status_code == 200

    job = Job.get_by_id(job.id)
    assert job.selected_metadata["asin"] == "B0123456789"


def test_confirm_endpoint_manual_entry_with_no_candidates_works(client):
    """Planning doc Section 2 step 6: manual entry must work even when no
    metadata candidates matched - the form fields are the only source of
    truth for what gets confirmed either way.
    """
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM, candidates_json="[]")
    resp = client.post(f"/jobs/{job.id}/confirm", data={"title": "Hand Typed Title"})
    assert resp.status_code == 200
    assert Job.get_by_id(job.id).selected_metadata["title"] == "Hand Typed Title"


def test_search_endpoint_updates_candidates_and_renders_them(client, monkeypatch):
    """A manual search from the review step should overwrite job.candidates
    with fresh results and hand back HTML containing those results, so
    app.js can swap the candidates portion of the panel in place.
    """
    from app.pipeline import metadata as metadata_mod

    job = Job.create(
        source_path="/x",
        status=Job.STATUS_AWAITING_METADATA_CONFIRM,
        title_guess="bad guess",
        author_guess="",
    )

    def fake_search(title, author, **kwargs):
        assert title == "The Real Title"
        assert author == "The Real Author"
        return [
            {
                "asin": "B0999999999",
                "title": "The Real Title",
                "author": "The Real Author",
                "narrator": "",
                "series": "",
                "series_index": "",
                "year": "2020",
                "description": "",
                "cover_url": "",
                "genre": "",
            }
        ]

    monkeypatch.setattr(metadata_mod, "search", fake_search)

    resp = client.post(
        f"/jobs/{job.id}/search",
        data={"title": "The Real Title", "author": "The Real Author"},
    )
    assert resp.status_code == 200
    assert "The Real Title" in resp.text

    job = Job.get_by_id(job.id)
    assert job.candidates[0]["title"] == "The Real Title"
    assert job.candidates[0]["asin"] == "B0999999999"


def test_search_endpoint_response_carries_candidate_asin(client, monkeypatch):
    """Regression guard for the asin-dropping bug class: the fragment a
    manual search returns must embed asin in its data-candidates-json,
    exactly like the initial panel render does, since app.js's
    candidate-click handler only ever reads asin out of that JSON blob.
    """
    from app.pipeline import metadata as metadata_mod

    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM)

    monkeypatch.setattr(
        metadata_mod,
        "search",
        lambda title, author, **kwargs: [
            {"title": "The Calamity Club", "author": "A. Author", "asin": "B0123456789"}
        ],
    )

    resp = client.post(f"/jobs/{job.id}/search", data={"title": "calamity club"})
    assert resp.status_code == 200
    assert "data-candidates-json" in resp.text
    assert "B0123456789" in resp.text


def test_search_endpoint_response_includes_none_of_these_option(client, monkeypatch):
    """The "None of these" opt-out row must survive a manual re-search too,
    not just the initial automatic-search render, since app.js swaps this
    same fragment back into the panel and re-wires it after every search.
    """
    from app.pipeline import metadata as metadata_mod

    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM)

    monkeypatch.setattr(
        metadata_mod,
        "search",
        lambda title, author, **kwargs: [
            {"title": "The Calamity Club", "author": "A. Author", "asin": "B0123456789"}
        ],
    )

    resp = client.post(f"/jobs/{job.id}/search", data={"title": "calamity club"})
    assert resp.status_code == 200
    assert 'class="candidate candidate-none"' in resp.text


def test_search_endpoint_404_for_missing_job(client, monkeypatch):
    from app.pipeline import metadata as metadata_mod

    monkeypatch.setattr(metadata_mod, "search", lambda title, author, **kwargs: [])
    resp = client.post("/jobs/99999/search", data={"title": "x"})
    assert resp.status_code == 404


def test_search_endpoint_requires_title_or_author(client):
    job = Job.create(source_path="/x", status=Job.STATUS_AWAITING_METADATA_CONFIRM)
    resp = client.post(f"/jobs/{job.id}/search", data={"title": "", "author": ""})
    assert resp.status_code == 400


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
    from app.watcher import claim_pending, list_pending, start_watcher

    monkeypatch.setattr(config, "SETTLE_WINDOW_SEC", 0.1)
    observer, stop_event = start_watcher()
    try:
        (isolated_dirs["inbox"] / "Some Book.mp3").write_bytes(b"x" * 100)
        import time

        deadline = time.time() + 5.0
        pending = []
        while time.time() < deadline:
            pending = list_pending()
            if len(pending) == 1:
                break
            time.sleep(0.1)
        assert len(pending) == 1
        resp = client.get(f"/fragments/panel/{pending[0].id}")
        assert resp.status_code == 200
        assert "Some Book.mp3" in resp.text
        # Viewing the panel must not consume the entry.
        assert claim_pending("Some Book.mp3") is not None
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5)
