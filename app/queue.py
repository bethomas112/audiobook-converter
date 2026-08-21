"""Job orchestration: the Huey task definitions, and the queue dispatcher
that decides which job Huey actually runs and when.

Two Huey tasks do the real work - start_job (detection + metadata search)
and process_job (conversion through to archiving) - but nothing is ever
handed to Huey the instant it becomes eligible. Confirming a job's
metadata just marks it "ready" with a queue_order; dispatch_next() is the
only thing that ever promotes a ready job to Huey, and only when nothing
else is currently processing. That indirection is what makes the rest of
this module simple:

  - Reordering the queue (reorder_queue) is a plain update to queue_order
    on rows Huey has never seen, not a queue-system operation.
  - Cancelling a not-yet-started job (cancel_job) is a plain status flip,
    since Huey was never told about it either.
  - Cancelling the one job that IS running has to work differently, since
    it's mid-execution in the worker process: cancel_job sets a
    cancel_requested flag, and process_job's own progress-reporting loop
    (see should_cancel() inside it) is what notices and stops the ffmpeg
    subprocess.

dispatch_next() is called after every state change that could free up or
fill the "one job converting" slot - a confirm, a cancel, a requeue, or a
job finishing - so the next ready job (if any) starts on its own without
anything else having to poll for it.
"""
from pathlib import Path

from huey import SqliteHuey, crontab

from app.config import config
from app.db import Job, init_db
from app.pipeline import archive, chapters, convert, detect, ffutil, metadata, tag, output

config.ensure_dirs()
init_db()

huey = SqliteHuey("audiobook-converter", filename=str(config.CONFIG_DIR / "huey.db"))


@huey.task()
def start_job(job_id: int):
    """Runs format detection and a metadata search for a newly-confirmed job."""
    job = Job.get_by_id(job_id)
    job.status = Job.STATUS_DETECTING
    job.touch_and_save()

    try:
        result = detect.detect(Path(job.source_path))
        job.source_type = result.source_type
        job.audio_files = result.audio_files
        job.title_guess = result.title_guess
        job.author_guess = result.author_guess
        job.append_log(
            f"Detected as {result.source_type} ({len(result.audio_files)} audio file(s))."
        )
        if result.ignored_files:
            job.append_log(f"Ignored non-audio files: {[f.name for f in result.ignored_files]}")

        # Total actual source duration, probed directly rather than trusted
        # from any catalog. Computed here (once, up front) rather than only
        # at conversion time so the review step can show it next to a
        # candidate's official runtime_minutes - letting a mismatched
        # edition (e.g. an abridged match against an unabridged source) be
        # spotted before confirming instead of only after conversion.
        try:
            job.source_duration_sec = sum(ffutil.get_duration_sec(f) for f in result.audio_files)
        except ffutil.FFError as e:
            raise ffutil.FFError(
                f"A source file appears to be corrupt or unreadable and can't be processed ({e})."
            ) from e
        job.append_log(f"Total source duration: {job.source_duration_sec / 60:.1f} min.")

        candidates = metadata.search(result.title_guess, result.author_guess)
        job.candidates = candidates
        job.append_log(f"Found {len(candidates)} metadata candidate(s).")

        top_asin = candidates[0].get("asin") if candidates else None
        if top_asin:
            # A preview only - shown during review so the user can see roughly
            # what will be written before confirming. Whichever candidate they
            # actually confirm is what process_job resolves chapters against.
            job.chapters_preview = metadata.get_chapters(top_asin)

        if config.AUTO_CONFIRM_METADATA and candidates:
            job.touch_and_save()
            confirm_metadata(job.id, candidates[0])
        else:
            job.status = Job.STATUS_AWAITING_METADATA_CONFIRM
            job.touch_and_save()
    except Exception as e:  # noqa: BLE001 - job failures must not crash the worker
        job.status = Job.STATUS_FAILED
        job.error_message = str(e)
        job.append_log(f"Failed during detection/metadata search: {e}")
        job.touch_and_save()


def _next_queue_order() -> int:
    highest = (
        Job.select(Job.queue_order)
        .where(Job.queue_order.is_null(False))
        .order_by(Job.queue_order.desc())
        .first()
    )
    return (highest.queue_order + 1) if highest else 1


def confirm_metadata(job_id: int, selected_metadata: dict):
    """Saves the confirmed match and puts the job at the back of the
    conversion queue. Doesn't start converting by itself - dispatch_next()
    decides that, so this is safe to call for any number of jobs in a row
    without them all starting at once.
    """
    job = Job.get_by_id(job_id)
    job.selected_metadata = selected_metadata
    job.status = Job.STATUS_READY
    job.queue_order = _next_queue_order()
    job.touch_and_save()
    dispatch_next()


def requeue_job(job_id: int):
    """Puts a cancelled job back at the end of the conversion queue, reusing
    its already-confirmed metadata rather than searching again.
    """
    job = Job.get_by_id(job_id)
    job.status = Job.STATUS_READY
    job.queue_order = _next_queue_order()
    job.cancel_requested = False
    job.touch_and_save()
    dispatch_next()


def dispatch_next():
    """If nothing is currently converting, start the next ready job (lowest
    queue_order first). Safe to call any time from either process (after a
    confirm, a cancel, or a job finishing) - a no-op if something's already
    running or nothing is waiting. Nothing is ever handed to Huey before
    this actually picks it, which is what makes reordering a plain data
    update instead of a queue-system operation.
    """
    if Job.select().where(Job.status == Job.STATUS_PROCESSING).exists():
        return
    next_job = (
        Job.select()
        .where(Job.status == Job.STATUS_READY)
        .order_by(Job.queue_order.asc())
        .first()
    )
    if next_job is None:
        return
    next_job.status = Job.STATUS_PROCESSING
    next_job.cancel_requested = False
    next_job.progress_pct = 0
    next_job.progress_stage = None
    next_job.touch_and_save()
    process_job(next_job.id)


def cancel_job(job_id: int):
    """Cancels a job that's ready (not yet started) or actively converting.

    A ready job is simply marked cancelled - it was never hand off to Huey,
    since dispatch_next() only does that the moment a job actually starts.
    An actively-converting job can't be stopped from here directly (it's
    running in the worker process); instead this sets cancel_requested,
    which the running conversion's own progress loop polls and acts on.
    """
    job = Job.get_by_id(job_id)
    if job.status == Job.STATUS_READY:
        job.status = Job.STATUS_CANCELLED
        job.queue_order = None
        job.append_log("Cancelled before conversion started.")
        job.touch_and_save()
    elif job.status == Job.STATUS_PROCESSING:
        job.cancel_requested = True
        job.touch_and_save()
    else:
        return


def reorder_queue(job_id: int, direction: str):
    """Moves a not-yet-started job up or down one place among the other
    ready jobs. The currently-processing job (if any) isn't part of this -
    it's already running and can't be reordered without cancelling it.
    """
    ready_jobs = list(
        Job.select().where(Job.status == Job.STATUS_READY).order_by(Job.queue_order.asc())
    )
    ids = [j.id for j in ready_jobs]
    if job_id not in ids:
        return
    idx = ids.index(job_id)
    swap_with = idx - 1 if direction == "up" else idx + 1
    if swap_with < 0 or swap_with >= len(ids):
        return
    ready_jobs[idx].queue_order, ready_jobs[swap_with].queue_order = (
        ready_jobs[swap_with].queue_order,
        ready_jobs[idx].queue_order,
    )
    ready_jobs[idx].save()
    ready_jobs[swap_with].save()


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


def remove_job(job_id: int):
    """Hides a job from the active queue views without deleting its row -
    see Job.dismissed for why deleting the row outright would be a real
    bug (the watcher would re-detect a still-present source as new).

    Runs the same SOURCE_CLEANUP_MODE cleanup a completed job's source
    gets (see archive.handle_source_cleanup), so a removed job's source
    stops sitting in the inbox exactly the way a converted one does,
    rather than being left there to permanently occupy its path.
    """
    job = Job.get_by_id(job_id)
    source_path = Path(job.source_path)
    if source_path.exists():
        new_path = archive.handle_source_cleanup(source_path, log=job.append_log)
        if new_path is not None:
            job.source_path = str(new_path)
    job.dismissed = True
    job.touch_and_save()


@huey.task()
def process_job(job_id: int):
    """Runs conversion, chapters, tagging, output placement, and archival
    for the job the dispatcher just started. dispatch_next() enforces that
    only one of these ever runs at a time.
    """
    job = Job.get_by_id(job_id)
    last_pct = -1

    def on_progress(pct: int):
        nonlocal last_pct
        if pct == last_pct:
            return
        last_pct = pct
        job.save_progress(pct)

    def should_cancel() -> bool:
        # Re-read from the DB every time: cancellation is requested from the
        # web process, which this worker process can only see via SQLite.
        return bool(Job.select(Job.cancel_requested).where(Job.id == job_id).scalar())

    try:
        meta = job.selected_metadata
        if not meta:
            raise ValueError("No confirmed metadata on this job.")

        audio_files = job.audio_files
        work_path = config.WORK_DIR / f"job_{job.id}.m4b"

        if job.source_type == "m4b_single":
            job.save_progress(10, stage="Copying M4B (no re-encode)")
            convert.passthrough_m4b(audio_files[0], work_path)
            has_embedded = bool(ffutil.get_embedded_chapters(work_path))
            job.append_log("M4B source passed through untouched (no re-encode).")
            job.save_progress(90)
        else:
            job.save_progress(0, stage="Transcoding audio")
            convert.convert_mp3_to_m4b(
                audio_files,
                work_path,
                log=job.append_log,
                on_progress=on_progress,
                should_cancel=should_cancel,
            )
            has_embedded = False

        job.save_progress(92, stage="Resolving chapters")
        resolved_chapters = chapters.resolve_chapters(
            job.source_type, audio_files, work_path, has_embedded, meta.get("asin"), log=job.append_log
        )
        if resolved_chapters is not None:
            ffutil.inject_chapters_ffmetadata(work_path, resolved_chapters)
            job.append_log(f"Wrote {len(resolved_chapters)} chapter(s).")
        else:
            job.append_log("Left source's embedded chapters untouched.")

        job.save_progress(96, stage="Applying tags")
        tag.apply_tags(work_path, meta)
        job.append_log("Applied metadata tags and cover art.")

        job.save_progress(98, stage="Moving to output")
        dest = output.place_output(work_path, meta)
        job.destination_path = str(dest)
        job.append_log(f"Placed finished audiobook at {dest}.")

        new_source_path = archive.handle_source_cleanup(Path(job.source_path), log=job.append_log)
        if new_source_path is not None:
            job.source_path = str(new_source_path)

        job.status = Job.STATUS_DONE
        job.queue_order = None
        job.progress_pct = 100
        job.progress_stage = None
        job.touch_and_save()
    except ffutil.CancelledError:
        work_path.unlink(missing_ok=True)
        job.status = Job.STATUS_CANCELLED
        job.queue_order = None
        job.progress_stage = None
        job.cancel_requested = False
        job.append_log("Conversion cancelled before it finished. Nothing was written.")
        job.touch_and_save()
    except Exception as e:  # noqa: BLE001 - job failures must not crash the worker
        job.status = Job.STATUS_FAILED
        job.error_message = str(e)
        job.progress_stage = None
        job.append_log(f"Failed during processing: {e}")
        job.touch_and_save()
    finally:
        dispatch_next()


@huey.periodic_task(crontab(hour="3", minute="0"))
def purge_expired_archives_task():
    archive.purge_expired_archives(log=print)
