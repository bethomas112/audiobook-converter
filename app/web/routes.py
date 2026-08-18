import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from app.config import config
from app.db import Job
from app.queue import process_job, start_job

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")
_basic_auth = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)):
    if config.WEB_UI_AUTH != "basic":
        return
    valid = bool(credentials) and secrets.compare_digest(
        credentials.username, config.WEB_UI_USERNAME
    ) and secrets.compare_digest(credentials.password, config.WEB_UI_PASSWORD)
    if not valid:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


@router.get("/")
def root():
    return RedirectResponse(url="/pending")


@router.get("/pending")
def pending(request: Request, _=Depends(require_auth)):
    jobs = list(Job.select().where(Job.status == Job.STATUS_PENDING).order_by(Job.created_at))
    return templates.TemplateResponse("pending.html", {"request": request, "jobs": jobs, "active": "pending"})


@router.post("/jobs/{job_id}/start")
def start(job_id: int, _=Depends(require_auth)):
    job = Job.get_or_none(Job.id == job_id)
    if job is None:
        raise HTTPException(status_code=404)
    job.status = Job.STATUS_QUEUED
    job.touch_and_save()
    start_job(job_id)
    return RedirectResponse(url="/pending", status_code=303)


@router.get("/review")
def review(request: Request, _=Depends(require_auth)):
    jobs = list(
        Job.select()
        .where(Job.status == Job.STATUS_AWAITING_METADATA_CONFIRM)
        .order_by(Job.created_at)
    )
    return templates.TemplateResponse("review.html", {"request": request, "jobs": jobs, "active": "review"})


@router.get("/review/{job_id}")
def review_detail(request: Request, job_id: int, _=Depends(require_auth)):
    job = Job.get_or_none(Job.id == job_id)
    if job is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "review_detail.html",
        {"request": request, "job": job, "candidates": job.candidates, "active": "review"},
    )


@router.post("/review/{job_id}/confirm")
def review_confirm(
    job_id: int,
    candidate_index: int | None = Form(None),
    mode: str | None = Form(None),
    title: str | None = Form(None),
    author: str | None = Form(None),
    narrator: str | None = Form(None),
    series: str | None = Form(None),
    series_index: str | None = Form(None),
    year: str | None = Form(None),
    genre: str | None = Form(None),
    cover_url: str | None = Form(None),
    description: str | None = Form(None),
    _=Depends(require_auth),
):
    job = Job.get_or_none(Job.id == job_id)
    if job is None:
        raise HTTPException(status_code=404)

    if mode == "manual":
        job.selected_metadata = {
            "asin": "",
            "title": title or "",
            "author": author or "",
            "narrator": narrator or "",
            "series": series or "",
            "series_index": series_index or "",
            "year": year or "",
            "genre": genre or "",
            "cover_url": cover_url or "",
            "description": description or "",
        }
    else:
        candidates = job.candidates
        if candidate_index is None or not (0 <= candidate_index < len(candidates)):
            raise HTTPException(status_code=400, detail="Invalid candidate selection")
        job.selected_metadata = candidates[candidate_index]

    job.status = Job.STATUS_PROCESSING
    job.touch_and_save()
    process_job(job_id)
    return RedirectResponse(url="/history", status_code=303)


@router.get("/history")
def history(request: Request, _=Depends(require_auth)):
    jobs = list(
        Job.select()
        .where(
            Job.status.in_(
                [
                    Job.STATUS_QUEUED,
                    Job.STATUS_DETECTING,
                    Job.STATUS_PROCESSING,
                    Job.STATUS_DONE,
                    Job.STATUS_FAILED,
                ]
            )
        )
        .order_by(Job.updated_at.desc())
    )
    return templates.TemplateResponse("history.html", {"request": request, "jobs": jobs, "active": "history"})


@router.get("/history/{job_id}")
def history_detail(request: Request, job_id: int, _=Depends(require_auth)):
    job = Job.get_or_none(Job.id == job_id)
    if job is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("history_detail.html", {"request": request, "job": job, "active": "history"})
