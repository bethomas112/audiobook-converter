from pathlib import Path

from huey import SqliteHuey, crontab

from app.config import config
from app.db import Job, init_db
from app.pipeline import archive, chapters, convert, detect, ffutil, metadata, tag, output

config.ensure_dirs()
init_db()

huey = SqliteHuey("audiobook-pipeline", filename=str(config.CONFIG_DIR / "huey.db"))


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

        candidates = metadata.search(result.title_guess, result.author_guess)
        job.candidates = candidates
        job.append_log(f"Found {len(candidates)} metadata candidate(s).")

        if config.AUTO_CONFIRM_METADATA and candidates:
            job.selected_metadata = candidates[0]
            job.status = Job.STATUS_PROCESSING
            job.touch_and_save()
            process_job(job_id)
        else:
            job.status = Job.STATUS_AWAITING_METADATA_CONFIRM
            job.touch_and_save()
    except Exception as e:  # noqa: BLE001 - job failures must not crash the worker
        job.status = Job.STATUS_FAILED
        job.error_message = str(e)
        job.append_log(f"Failed during detection/metadata search: {e}")


@huey.task()
def process_job(job_id: int):
    """Runs conversion, chapters, tagging, output placement, and archival."""
    job = Job.get_by_id(job_id)
    job.status = Job.STATUS_PROCESSING
    job.touch_and_save()

    try:
        meta = job.selected_metadata
        if not meta:
            raise ValueError("No confirmed metadata on this job.")

        audio_files = job.audio_files
        work_path = config.WORK_DIR / f"job_{job.id}.m4b"

        if job.source_type == "m4b_single":
            convert.passthrough_m4b(audio_files[0], work_path)
            has_embedded = bool(ffutil.get_embedded_chapters(work_path))
            job.append_log("M4B source passed through untouched (no re-encode).")
        else:
            convert.convert_mp3_to_m4b(audio_files, work_path, log=job.append_log)
            has_embedded = False

        resolved_chapters = chapters.resolve_chapters(
            job.source_type, audio_files, work_path, has_embedded, meta.get("asin")
        )
        if resolved_chapters is not None:
            ffutil.inject_chapters_ffmetadata(work_path, resolved_chapters)
            job.append_log(f"Wrote {len(resolved_chapters)} chapter(s).")
        else:
            job.append_log("Left source's embedded chapters untouched.")

        tag.apply_tags(work_path, meta)
        job.append_log("Applied metadata tags and cover art.")

        dest = output.place_output(work_path, meta)
        job.destination_path = str(dest)
        job.append_log(f"Placed finished audiobook at {dest}.")

        archive.handle_source_cleanup(Path(job.source_path), log=job.append_log)

        job.status = Job.STATUS_DONE
        job.touch_and_save()
    except Exception as e:  # noqa: BLE001 - job failures must not crash the worker
        job.status = Job.STATUS_FAILED
        job.error_message = str(e)
        job.append_log(f"Failed during processing: {e}")


@huey.periodic_task(crontab(hour="3", minute="0"))
def purge_expired_archives_task():
    archive.purge_expired_archives(log=print)
