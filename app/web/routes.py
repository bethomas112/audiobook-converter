"""HTTP layer. Four kinds of routes, matching how app/web/static/app.js
uses them:

  - GET /               the full page (topbar + rail + one detail panel)
  - GET /fragments/*     the same rail/panel/now-converting HTML rendered
                          standalone, for the frontend to fetch and swap
                          in without a full page reload
  - GET /api/status      a small JSON poll target - just id/status/progress
                          per job, cheap enough to hit every couple seconds
  - POST /jobs/{id}/...  actions (start, confirm, cancel, requeue, remove,
                          reorder, search); most just return {"ok": true}
                          and let the frontend re-fetch whatever fragments
                          changed - the exception is search, which returns
                          the re-rendered _candidates.html fragment
                          directly, since app.js swaps that one in place
                          rather than re-fetching it separately

_board_context() is the one place that queries and groups jobs by status;
every route that renders a rail or panel builds its response from it, so
the "Needs Input / Converting / Done" grouping only has to be defined once.
"""
import secrets
import urllib.parse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.config import config
from app.db import Job
from app.pipeline import metadata
from app.pipeline import archive
from app.queue import (
    cancel_job,
    confirm_metadata,
    remove_job,
    reorder_queue,
    requeue_job,
    start_job,
    start_new_job,
)
from app.watcher import claim_pending, list_pending

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")
_basic_auth = HTTPBasic(auto_error=False)

_NEEDS_INPUT_STATUSES = [
    Job.STATUS_PENDING,
    Job.STATUS_QUEUED,
    Job.STATUS_DETECTING,
    Job.STATUS_AWAITING_METADATA_CONFIRM,
    Job.STATUS_FAILED,
    Job.STATUS_CANCELLED,
]
_CONVERTING_STATUSES = [Job.STATUS_READY, Job.STATUS_PROCESSING]


def _ordinal(n: int) -> str:
    suffixes = ["th", "st", "nd", "rd"]
    v = n % 100
    suffix = suffixes[(v - 20) % 10] if (v - 20) % 10 in (1, 2, 3) else suffixes[0]
    if 11 <= v <= 13:
        suffix = "th"
    return f"{n}{suffix}"


templates.env.filters["ordinal"] = _ordinal

# A book with no confirmed cover yet (still waiting, or lookup failed) gets a
# monogram instead - deterministic per book so it stays consistent across
# refreshes rather than reshuffling colors on every page load.
_COVER_GRADIENTS = [
    "linear-gradient(155deg,#e0a86a,#8a5a2c)",
    "linear-gradient(155deg,#c98f8f,#6e3b3b)",
    "linear-gradient(155deg,#8fa9c9,#3c5474)",
    "linear-gradient(155deg,#e0c96a,#8a742c)",
    "linear-gradient(155deg,#9fb8c9,#3c5b74)",
    "linear-gradient(155deg,#9dbb7d,#4f6b3a)",
]


def _cover_gradient(seed: str) -> str:
    idx = sum(ord(c) for c in (seed or "?")) % len(_COVER_GRADIENTS)
    return _COVER_GRADIENTS[idx]


def _monogram(text: str) -> str:
    return (text or "?").strip()[:1].upper() or "?"


templates.env.filters["cover_gradient"] = _cover_gradient
templates.env.filters["monogram"] = _monogram


# Duration display for the metadata review step: a candidate's official
# runtime (app/pipeline/metadata.py's runtime_minutes) and a source's actual
# probed duration (app/db.py's Job.source_duration_sec) are shown side by
# side there so a mismatched edition is visible before confirming - see
# _candidates.html and _panel.html's awaiting_metadata_confirm branch.
def _format_duration_minutes(total_minutes) -> str:
    if not total_minutes:
        return ""
    total_minutes = round(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _format_duration_seconds(total_seconds) -> str:
    if not total_seconds:
        return ""
    return _format_duration_minutes(total_seconds / 60)


templates.env.filters["duration_minutes"] = _format_duration_minutes
templates.env.filters["duration_seconds"] = _format_duration_seconds


# Chapter timestamps in the review UI (_chapters.html) - a "clock" style
# readout, not the rounded "Xh Ym" summary above: m:ss under an hour so
# short-book chapter lists stay compact, h:mm:ss (zero-padded) past the
# 1-hour mark so a 14h-in chapter reads as "14:02:00" rather than an
# unbounded minute count like "842:00".
def _chapter_timestamp(start_sec) -> str:
    start_sec = int(start_sec or 0)
    hours, remainder = divmod(start_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


templates.env.filters["chapter_timestamp"] = _chapter_timestamp


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    if config.WEB_UI_AUTH != "basic":
        return
    valid = bool(credentials) and secrets.compare_digest(
        credentials.username, config.WEB_UI_USERNAME
    ) and secrets.compare_digest(credentials.password, config.WEB_UI_PASSWORD)
    if not valid:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


def _resolve_board_item(job_id: str):
    """Looks up either a real Job (plain integer id) or a not-yet-claimed
    PendingEntry ("pending:<url-quoted name>" id) for read-only display -
    does not claim/consume a pending entry. Returns None if neither
    resolves.

    FastAPI/Starlette auto-decodes path params, so the incoming job_id
    here is already unquoted (e.g. a literal space, not "%20") while
    PendingEntry.id is always stored pre-quoted (see app/watcher.py's
    _pending_id() - the design doc requires using the encoded form
    consistently everywhere an id is compared or embedded). Re-quoting
    job_id's name back to the canonical encoded form before comparing is
    what makes this match rather than silently 404 for any name
    containing a space or other reserved character.
    """
    if job_id.startswith("pending:"):
        name = urllib.parse.unquote(job_id[len("pending:"):])
        canonical_id = "pending:" + urllib.parse.quote(name, safe="")
        return next((entry for entry in list_pending() if entry.id == canonical_id), None)
    try:
        numeric_id = int(job_id)
    except ValueError:
        return None
    return Job.get_or_none(Job.id == numeric_id)


def _board_context(request: Request) -> dict:
    needs_input_jobs = list(
        Job.select()
        .where(Job.dismissed == False, Job.status.in_(_NEEDS_INPUT_STATUSES))  # noqa: E712
        .order_by(Job.created_at)
    )
    needs_input = sorted(needs_input_jobs + list_pending(), key=lambda j: j.created_at)
    converting = list(
        Job.select()
        .where(Job.dismissed == False, Job.status.in_(_CONVERTING_STATUSES))  # noqa: E712
        .order_by(Job.queue_order.asc())
    )
    done = list(
        Job.select()
        .where(Job.dismissed == False, Job.status == Job.STATUS_DONE)
        .order_by(Job.updated_at.desc())
    )

    processing_job = next((j for j in converting if j.status == Job.STATUS_PROCESSING), None)
    ready_jobs = [j for j in converting if j.status == Job.STATUS_READY]
    queue_positions = {j.id: i + 1 for i, j in enumerate(ready_jobs)}

    active_job = processing_job or (needs_input[0] if needs_input else None) or (done[0] if done else None)

    return {
        "request": request,
        "needs_input": needs_input,
        "converting": converting,
        "done": done,
        "processing_job": processing_job,
        "queue_positions": queue_positions,
        "all_jobs": needs_input + converting + done,
        "active_job": active_job,
        "active_job_id": active_job.id if active_job else None,
    }


@router.get("/")
def index(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("index.html", _board_context(request))


@router.get("/fragments/rail")
def fragment_rail(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("_rail.html", _board_context(request))


@router.get("/fragments/now-converting")
def fragment_now_converting(request: Request, _=Depends(require_auth)):
    ctx = _board_context(request)
    return templates.TemplateResponse("_now_converting.html", ctx)


@router.get("/fragments/panel/{job_id}")
def fragment_panel(request: Request, job_id: str, _=Depends(require_auth)):
    job = _resolve_board_item(job_id)
    if job is None:
        raise HTTPException(status_code=404)
    ctx = _board_context(request)
    ctx["job"] = job
    return templates.TemplateResponse("_panel.html", ctx)


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


@router.post("/jobs/{job_id}/confirm")
def confirm(
    job_id: int,
    title: str = Form(""),
    author: str = Form(""),
    narrator: str = Form(""),
    series: str = Form(""),
    series_index: str = Form(""),
    year: str = Form(""),
    genre: str = Form(""),
    cover_url: str = Form(""),
    description: str = Form(""),
    asin: str = Form(""),
    _=Depends(require_auth),
):
    """Whatever's currently in the staged-metadata fields is what gets
    written - the candidate cards are just a quick way to fill them in, not
    a separate submission mode. This covers manual entry for free: an empty
    candidates list just means the fields start blank.
    """
    if Job.get_or_none(Job.id == job_id) is None:
        raise HTTPException(status_code=404)

    selected = {
        "asin": asin,
        "title": title,
        "author": author,
        "narrator": narrator,
        "series": series,
        "series_index": series_index,
        "year": year,
        "genre": genre,
        "cover_url": cover_url,
        "description": description,
    }
    confirm_metadata(job_id, selected)
    return JSONResponse({"ok": True})


@router.post("/jobs/{job_id}/search")
def search_metadata(
    job_id: int,
    request: Request,
    title: str = Form(""),
    author: str = Form(""),
    _=Depends(require_auth),
):
    """Manual retry of the metadata search for the review step, for when
    detect._guess_title_author()'s filename-derived guess didn't produce a
    good automatic match. Reuses the exact same metadata.search() call
    start_job() makes - it's not coupled to the filename in any way, that's
    purely what start_job() happens to pass it - so this is purely an
    additive way to re-run it with user-supplied terms. Returns just the
    re-rendered candidates fragment (not the whole panel) so the frontend
    can swap it in without disturbing the rest of the review form.
    """
    job = Job.get_or_none(Job.id == job_id)
    if job is None:
        raise HTTPException(status_code=404)
    if not title.strip() and not author.strip():
        raise HTTPException(status_code=400, detail="Enter a title or author to search.")

    job.candidates = metadata.search(title, author)
    job.touch_and_save()
    return templates.TemplateResponse("_candidates.html", {"request": request, "job": job})


@router.post("/jobs/{job_id}/cancel")
def cancel(job_id: int, _=Depends(require_auth)):
    if Job.get_or_none(Job.id == job_id) is None:
        raise HTTPException(status_code=404)
    cancel_job(job_id)
    return JSONResponse({"ok": True})


@router.post("/jobs/{job_id}/requeue")
def requeue(job_id: int, _=Depends(require_auth)):
    if Job.get_or_none(Job.id == job_id) is None:
        raise HTTPException(status_code=404)
    requeue_job(job_id)
    return JSONResponse({"ok": True})


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


@router.post("/jobs/{job_id}/reorder")
def reorder(job_id: int, direction: str = Form(...), _=Depends(require_auth)):
    if direction not in ("up", "down"):
        raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")
    if Job.get_or_none(Job.id == job_id) is None:
        raise HTTPException(status_code=404)
    reorder_queue(job_id, direction)
    return JSONResponse({"ok": True})
