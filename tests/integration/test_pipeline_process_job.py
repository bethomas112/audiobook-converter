"""Integration tests driving the real job pipeline end-to-end in-process:
detect -> metadata search -> confirm -> convert -> chapters -> tag ->
output -> archive (app/queue.py's start_job/confirm_metadata/dispatch_next/
process_job), against real ffmpeg on synthetic audio. Only the network-
touching metadata calls are mocked (audnexus/Audible) - everything else,
including real subprocess ffmpeg conversion, runs for real.

This is the single broadest check that the pipeline modules actually fit
together the way ARCHITECTURE.md's "Conversion pipeline" section describes.
"""
from pathlib import Path

from app.db import Job
from app.pipeline import metadata as metadata_mod
from app import queue as queue_mod
from mutagen.mp4 import MP4

from tests.helpers import (
    has_quicktime_chapter_text_track,
    make_m4b,
    make_m4b_with_silence_gap,
    make_tone_mp3,
    strip_quicktime_chapter_track,
)


def _run_job_to_completion(monkeypatch, source_path, selected_metadata, candidates=None, chapters_from_audnexus=None):
    monkeypatch.setattr(metadata_mod, "search", lambda *a, **k: candidates or [selected_metadata])
    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin: chapters_from_audnexus or [])
    monkeypatch.setattr(metadata_mod, "fetch_cover_bytes", lambda url: b"\xff\xd8fakejpeg" if url else None)

    job = Job.create(source_path=str(source_path))
    queue_mod.start_job(job.id)
    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_AWAITING_METADATA_CONFIRM, job.log

    queue_mod.confirm_metadata(job.id, selected_metadata)
    return Job.get_by_id(job.id)


def test_mp3_single_pipeline_standalone_output_with_silence_chapters(isolated_dirs, monkeypatch, huey_immediate):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{author} - {title}")
    monkeypatch.setattr(config, "SILENCE_MIN_CHAPTER_SEC", 1)
    monkeypatch.setattr(config, "SILENCE_MIN_DURATION_SEC", 0.3)

    source = isolated_dirs["inbox"] / "Some Book.mp3"
    make_tone_mp3(source, duration_sec=2.0)

    meta = {
        "asin": "", "title": "Some Book", "author": "Some Author", "narrator": "",
        "series": "", "series_index": "", "year": "2020", "genre": "", "description": "", "cover_url": "",
    }
    job = _run_job_to_completion(monkeypatch, source, meta)

    assert job.status == Job.STATUS_DONE, job.log
    dest = Path(job.destination_path)
    assert dest.exists()
    assert dest.parent == isolated_dirs["output"]

    audio = MP4(dest)
    assert audio["\xa9nam"] == ["Some Book"]
    assert audio["\xa9ART"] == ["Some Author"]

    # Source archived (default SOURCE_CLEANUP_MODE), not left in the inbox.
    assert not source.exists()
    archived = isolated_dirs["archive"] / "Some Book.mp3"
    assert archived.exists()

    # source_path must follow the source to its new location - otherwise
    # the watcher's dedup (app/watcher.py's _known_source_paths()) would
    # permanently blacklist this book's original inbox path even though
    # nothing is there anymore, blocking any future unrelated drop-off
    # that happens to reuse the same filename.
    assert job.source_path == str(archived)


def test_mp3_multi_pipeline_library_output_with_source_boundary_chapters(isolated_dirs, monkeypatch, huey_immediate):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "library")
    monkeypatch.setattr(config, "LIBRARY_FOLDER_TEMPLATE", "{author}/[{series}/]{year} - {title}")
    monkeypatch.setattr(config, "LIBRARY_FILENAME_TEMPLATE", "{title}")
    monkeypatch.setattr(config, "WRITE_SIDECAR_FILES", True)

    source = isolated_dirs["inbox"] / "Multi Book"
    source.mkdir()
    make_tone_mp3(source / "track1.mp3", duration_sec=1.0)
    make_tone_mp3(source / "track2.mp3", duration_sec=1.5)

    meta = {
        "asin": "", "title": "Multi Book", "author": "Multi Author", "narrator": "Reader Name",
        "series": "", "series_index": "", "year": "2021", "genre": "", "description": "A tale.",
        "cover_url": "https://example.com/cover.jpg",
    }
    job = _run_job_to_completion(monkeypatch, source, meta)

    assert job.status == Job.STATUS_DONE, job.log
    dest = Path(job.destination_path)
    assert dest.name == "Multi Book.m4b"
    assert dest.parent == isolated_dirs["output"] / "Multi Author" / "2021 - Multi Book"

    # Source-file-boundary chapters: no audnexus data, multi-file source ->
    # exactly one chapter per original file (planning doc, chapter priority 3).
    from app.pipeline import ffutil
    embedded = ffutil.get_embedded_chapters(dest)
    assert len(embedded) == 2

    # Sidecar files written alongside the m4b (library mode + WRITE_SIDECAR_FILES).
    assert (dest.parent / "desc.txt").read_text() == "A tale."
    assert (dest.parent / "reader.txt").read_text() == "Reader Name"
    assert (dest.parent / "cover.jpg").exists()

    assert not source.exists()
    assert (isolated_dirs["archive"] / "Multi Book").exists()


def test_m4b_passthrough_pipeline_preserves_embedded_chapters_untouched(isolated_dirs, monkeypatch, huey_immediate):
    """Planning doc, Section 2 step 7: an M4B source must pass through with
    zero audio re-encoding - only a metadata-only tag patch. Chapter
    priority 1: embedded chapters already present are left exactly as-is.
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")

    source = isolated_dirs["inbox"] / "Chaptered Book.m4b"
    original_chapters = [
        {"start_sec": 0.0, "end_sec": 1.0, "title": "Prologue"},
        {"start_sec": 1.0, "end_sec": 2.0, "title": "Chapter One"},
    ]
    make_m4b(source, duration_sec=2.0, chapters=original_chapters)

    from app.pipeline import ffutil
    source_bitrate = ffutil.get_audio_bitrate_kbps(source)

    meta = {
        "asin": "B999", "title": "Chaptered Book", "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    # Even though an asin is present, audnexus chapters must NOT override
    # embedded chapters - priority 1 beats priority 2.
    job = _run_job_to_completion(
        monkeypatch, source, meta,
        chapters_from_audnexus=[{"start_sec": 0, "end_sec": 2, "title": "WRONG - should not appear"}],
    )

    assert job.status == Job.STATUS_DONE, job.log
    dest = Path(job.destination_path)

    embedded = ffutil.get_embedded_chapters(dest)
    assert [c["title"] for c in embedded] == ["Prologue", "Chapter One"]

    # No re-encode: bitrate/duration should match the original almost exactly
    # (a real transcode of a 96kbps tone would change these).
    assert ffutil.get_audio_bitrate_kbps(dest) == source_bitrate
    assert ffutil.get_duration_sec(dest) == 2.0 or abs(ffutil.get_duration_sec(dest) - 2.0) < 0.05


def test_m4b_passthrough_repairs_chapters_missing_quicktime_track(isolated_dirs, monkeypatch, huey_immediate):
    """Regression test for the real-world bug this fix addresses: a
    converted .m4b whose chapters read back correctly via ffprobe (Nero
    'chpl' atom) but show only generic "1", "2", "3" numbering - not the
    real titles - in the macOS Books app, because the source file was
    missing the QuickTime-style chapter track Apple's own apps need for
    titles.

    Simulates a source .m4b produced by some other/older ffmpeg-based tool
    that only ever wrote chpl (tests.helpers.strip_quicktime_chapter_track).
    The pipeline must detect the missing track (chapters.resolve_chapters +
    ffutil.has_quicktime_chapter_track) and repair it via
    ffutil.inject_chapters_ffmetadata - which, unlike a plain ffprobe
    chapter-list check, is verified here at the raw MP4 box level, since
    ffprobe's -show_chapters cannot tell the two formats apart (that's
    exactly why the original bug shipped unnoticed through this suite's
    prior ffprobe-only chapter assertions).
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")

    source = isolated_dirs["inbox"] / "Legacy Chaptered Book.m4b"
    original_chapters = [
        {"start_sec": 0.0, "end_sec": 1.0, "title": "Prologue: Birdie"},
        {"start_sec": 1.0, "end_sec": 2.0, "title": "Author's Note"},
    ]
    make_m4b(source, duration_sec=2.0, chapters=original_chapters)
    strip_quicktime_chapter_track(source)
    assert has_quicktime_chapter_text_track(source) is False  # confirm the fixture is chpl-only

    meta = {
        "asin": "", "title": "Legacy Chaptered Book", "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    job = _run_job_to_completion(monkeypatch, source, meta)

    assert job.status == Job.STATUS_DONE, job.log
    dest = Path(job.destination_path)

    from app.pipeline import ffutil
    embedded = ffutil.get_embedded_chapters(dest)
    assert [c["title"] for c in embedded] == ["Prologue: Birdie", "Author's Note"]

    # The actual fix: the output now has the QuickTime-style chapter track,
    # not just the chpl atom the source started with.
    assert has_quicktime_chapter_text_track(dest) is True
    assert ffutil.has_quicktime_chapter_track(dest) is True


def test_m4b_without_embedded_chapters_uses_audnexus_chapters(isolated_dirs, monkeypatch, huey_immediate):
    """Chapter priority 2: an M4B with NO embedded chapters falls through to
    audnexus's official chapter data for the matched title.

    The source has a real silence gap at ~8s so achew's aligner (see
    app/pipeline/chapters.py's _align_audnexus_chapters) has something to
    confidently anchor chapter 2 onto - the refined pipeline only ever
    writes a marker for a chapter with a *verified* placement (achew-
    confident or file-boundary-anchored), folding anything else onto a
    neighbour rather than trusting an unverified guess (see
    tests/unit/test_chapters.py for that folding behaviour in isolation).
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")

    source = isolated_dirs["inbox"] / "No Chapters Book.m4b"
    make_m4b_with_silence_gap(source, segments=[("tone", 8.0), ("silence", 2.0), ("tone", 10.0)])

    meta = {
        "asin": "B123", "title": "No Chapters Book", "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    job = _run_job_to_completion(
        monkeypatch, source, meta,
        # Both well over the refined chapter-alignment pipeline's 5s
        # short-chapter-folding floor (app/pipeline/chapters.py's
        # _MIN_CHAPTER_SEC) - this test is about priority-2 wiring, not
        # that folding behaviour (see tests/unit/test_chapters.py for that).
        chapters_from_audnexus=[
            {"start_sec": 0.0, "end_sec": 8.0, "title": "Official Ch1"},
            {"start_sec": 8.0, "end_sec": 20.0, "title": "Official Ch2"},
        ],
    )

    assert job.status == Job.STATUS_DONE, job.log
    from app.pipeline import ffutil
    embedded = ffutil.get_embedded_chapters(Path(job.destination_path))
    assert [c["title"] for c in embedded] == ["Official Ch1", "Official Ch2"]


def test_source_cleanup_mode_delete_removes_source_after_success(isolated_dirs, monkeypatch, huey_immediate):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")
    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "delete")

    source = isolated_dirs["inbox"] / "Delete Me.mp3"
    make_tone_mp3(source, duration_sec=1.0)
    meta = {"asin": "", "title": "Delete Me", "author": "", "narrator": "", "series": "",
            "series_index": "", "year": "", "genre": "", "description": "", "cover_url": ""}

    job = _run_job_to_completion(monkeypatch, source, meta)

    assert job.status == Job.STATUS_DONE, job.log
    assert not source.exists()
    assert list(isolated_dirs["archive"].iterdir()) == []


def test_source_cleanup_mode_keep_leaves_source_after_success(isolated_dirs, monkeypatch, huey_immediate):
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")
    monkeypatch.setattr(config, "SOURCE_CLEANUP_MODE", "keep")

    source = isolated_dirs["inbox"] / "Keep Me.mp3"
    make_tone_mp3(source, duration_sec=1.0)
    meta = {"asin": "", "title": "Keep Me", "author": "", "narrator": "", "series": "",
            "series_index": "", "year": "", "genre": "", "description": "", "cover_url": ""}

    job = _run_job_to_completion(monkeypatch, source, meta)

    assert job.status == Job.STATUS_DONE, job.log
    assert source.exists()


def test_cancellation_mid_conversion_removes_partial_output_and_marks_cancelled(
    isolated_dirs, monkeypatch, huey_immediate
):
    """ARCHITECTURE.md's "Live progress and cancellation" section:
    process_job's should_cancel() re-reads cancel_requested from the DB on
    every ffmpeg progress line, exactly as it would if a concurrent web
    request (cancel_job()) flipped that flag mid-run. To make that
    deterministic without racing a real wall clock, the DB flag is flipped
    from inside a wrapper around the real convert call - i.e. "cancel
    lands right as conversion begins" - then the *real* transcode function
    runs and its *real* should_cancel callback (wired straight to the DB)
    picks it up, so this still exercises genuine subprocess termination.
    """
    from app.config import config
    from app.pipeline import convert as convert_mod
    from app.pipeline import metadata as metadata_mod

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")
    monkeypatch.setattr(metadata_mod, "search", lambda *a, **k: [])
    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin: [])

    source = isolated_dirs["inbox"] / "Cancel Me.mp3"
    make_tone_mp3(source, duration_sec=5.0)

    job = Job.create(source_path=str(source))
    queue_mod.start_job(job.id)
    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_AWAITING_METADATA_CONFIRM, job.log

    real_convert = convert_mod.convert_mp3_to_m4b

    def _convert_then_cancel(*args, **kwargs):
        Job.update(cancel_requested=True).where(Job.id == job.id).execute()
        return real_convert(*args, **kwargs)

    monkeypatch.setattr(queue_mod.convert, "convert_mp3_to_m4b", _convert_then_cancel)

    meta = {"asin": "", "title": "Cancel Me", "author": "", "narrator": "", "series": "",
            "series_index": "", "year": "", "genre": "", "description": "", "cover_url": ""}
    queue_mod.confirm_metadata(job.id, meta)

    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_CANCELLED, job.log
    assert job.queue_order is None
    assert job.cancel_requested is False
    assert not list(isolated_dirs["work"].glob("*.m4b"))


def test_detection_computes_total_source_duration(isolated_dirs, monkeypatch, huey_immediate):
    """Enhancement: start_job() must probe and sum the real duration of
    every audio file right after detection succeeds (not only later,
    during conversion), so the metadata review step can show it next to a
    candidate's official runtime and let a mismatched edition (e.g. an
    abridged match against an unabridged source) be spotted before
    confirming.
    """
    from app.pipeline import metadata as metadata_mod

    monkeypatch.setattr(metadata_mod, "search", lambda *a, **k: [])
    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin: [])

    source = isolated_dirs["inbox"] / "Multi Duration Book"
    source.mkdir()
    make_tone_mp3(source / "track1.mp3", duration_sec=1.0)
    make_tone_mp3(source / "track2.mp3", duration_sec=1.5)

    job = Job.create(source_path=str(source))
    queue_mod.start_job(job.id)
    job = Job.get_by_id(job.id)

    assert job.status == Job.STATUS_AWAITING_METADATA_CONFIRM, job.log
    assert job.source_duration_sec is not None
    assert abs(job.source_duration_sec - 2.5) < 0.2


def test_failed_detection_marks_job_failed_not_crash(isolated_dirs, monkeypatch, huey_immediate):
    """A source that fails detection (e.g. an unsupported extension slipped
    past the watcher somehow) must land in 'failed' with a log/error
    message, not raise out of the worker.
    """
    source = isolated_dirs["inbox"] / "not_audio.txt"
    source.write_text("oops")

    job = Job.create(source_path=str(source))
    queue_mod.start_job(job.id)
    job = Job.get_by_id(job.id)

    assert job.status == Job.STATUS_FAILED
    assert job.error_message
