"""End-to-end check that the ported chapter aligner (app/pipeline/chapter_aligner.py,
see its attribution header for provenance) is correctly wired into the real pipeline -
not just correct in isolation (see tests/unit/test_chapter_aligner.py for that).

Builds a real, synthetic multi-chapter MP3 with deliberate silences at the true
chapter boundaries (tests/helpers.make_tone_silence_pattern_mp3, real ffmpeg, no
mocked audio), runs it through the actual job pipeline (app/queue.py's
start_job/confirm_metadata/process_job) with audnexus chapter data mocked to a
known, deliberately wrong offset from those true boundaries, and asserts the
chapters actually written into the output M4B land on the real silences - not on
audnexus's offset timestamps. This is the exact bug the aligner exists to fix (see
app/pipeline/chapters.py's module docstring and ARCHITECTURE.md's "Chapters are
resolved in priority order" section).
"""
from pathlib import Path

import pytest

from app.db import Job
from app.pipeline import metadata as metadata_mod
from app.pipeline import ffutil
from app import queue as queue_mod

from tests.helpers import make_tone_silence_pattern_mp3


def _run_job_to_completion(monkeypatch, source_path, selected_metadata, chapters_from_audnexus):
    monkeypatch.setattr(metadata_mod, "search", lambda *a, **k: [selected_metadata])
    monkeypatch.setattr(metadata_mod, "get_chapters", lambda asin: chapters_from_audnexus)
    monkeypatch.setattr(metadata_mod, "fetch_cover_bytes", lambda url: None)

    job = Job.create(source_path=str(source_path))
    queue_mod.start_job(job.id)
    job = Job.get_by_id(job.id)
    assert job.status == Job.STATUS_AWAITING_METADATA_CONFIRM, job.log

    queue_mod.confirm_metadata(job.id, selected_metadata)
    return Job.get_by_id(job.id)


def test_audnexus_chapters_are_corrected_to_real_silence_boundaries(isolated_dirs, monkeypatch, huey_immediate):
    """The real-world bug (three rounds of investigation, see the task's findings):
    audnexus's chapter timestamps are anchored to Audible's own official release,
    which typically has different front/back matter than a local rip, so writing
    them verbatim lands chapter navigation after a chapter has actually begun. This
    builds a source where the true boundaries (real 3s silences) are deliberately
    ~7-9s away from what "audnexus" reports for this book, and confirms the
    finished M4B's chapters land on the real silences, not audnexus's offsets.
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")

    source = isolated_dirs["inbox"] / "Drifted Book.mp3"
    # tone(20s) silence(3s) tone(20s) silence(3s) tone(20s) = 66s total, two real
    # boundaries at silence_end 23.0 and 46.0 - chapter cues land ~1/3s before that
    # (achew's own convention - see DetectedCue.from_silences), i.e. ~22.67 / ~45.67.
    _, silence_windows = make_tone_silence_pattern_mp3(
        source,
        segments=[("tone", 20.0), ("silence", 3.0), ("tone", 20.0), ("silence", 3.0), ("tone", 20.0)],
    )
    true_ch2_start = silence_windows[0][1] - (1 / 3)
    true_ch3_start = silence_windows[1][1] - (1 / 3)

    # audnexus's reported chapters, deliberately offset from the true boundaries -
    # simulating different front/back matter between Audible's release and this rip.
    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": 30.0, "title": "Chapter One"},
        {"start_sec": 30.0, "end_sec": 55.0, "title": "Chapter Two"},
        {"start_sec": 55.0, "end_sec": 66.0, "title": "Chapter Three"},
    ]
    assert abs(30.0 - true_ch2_start) > 5.0  # sanity: the offset really is deliberate/substantial
    assert abs(55.0 - true_ch3_start) > 5.0

    meta = {
        "asin": "B0DRIFTED", "title": "Drifted Book", "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    job = _run_job_to_completion(monkeypatch, source, meta, audnexus_chapters)

    assert job.status == Job.STATUS_DONE, job.log
    dest = Path(job.destination_path)
    written = ffutil.get_embedded_chapters(dest)

    assert [c["title"] for c in written] == ["Chapter One", "Chapter Two", "Chapter Three"]
    assert written[0]["start_sec"] == 0.0

    # The actual fix: chapter 2/3 land near the REAL silence boundaries, clearly
    # closer to the true position than to audnexus's own offset timestamp.
    assert written[1]["start_sec"] == pytest.approx(true_ch2_start, abs=1.0), (
        f"expected ~{true_ch2_start:.2f} (real silence), got {written[1]['start_sec']:.2f} "
        f"(audnexus said 30.0 - was it left uncorrected?)"
    )
    assert written[2]["start_sec"] == pytest.approx(true_ch3_start, abs=1.0), (
        f"expected ~{true_ch3_start:.2f} (real silence), got {written[2]['start_sec']:.2f} "
        f"(audnexus said 55.0 - was it left uncorrected?)"
    )
    assert abs(written[1]["start_sec"] - 30.0) > 3.0, "chapter 2 looks like it kept audnexus's raw offset"
    assert abs(written[2]["start_sec"] - 55.0) > 3.0, "chapter 3 looks like it kept audnexus's raw offset"

    # The outcome must be logged (no user-facing confirmation gate at this point
    # in the pipeline - see app/pipeline/chapters.py's _align_audnexus_chapters).
    # job.log is one big timestamped string (see Job.append_log), not a list.
    assert "Aligned 3 audnexus chapter" in job.log


def test_audnexus_alignment_leaves_book_matching_reference_unchanged(isolated_dirs, monkeypatch, huey_immediate):
    """Sanity/no-regression companion case: when the real audio's silences DO sit
    right where audnexus says they should, alignment must land the chapters there
    too (not introduce drift where none existed).
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")

    source = isolated_dirs["inbox"] / "Matched Book.mp3"
    _, silence_windows = make_tone_silence_pattern_mp3(
        source,
        segments=[("tone", 10.0), ("silence", 2.0), ("tone", 10.0)],
    )
    true_ch2_start = silence_windows[0][1] - (1 / 3)

    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": true_ch2_start, "title": "Chapter One"},
        {"start_sec": true_ch2_start, "end_sec": 22.0, "title": "Chapter Two"},
    ]

    meta = {
        "asin": "B0MATCHED", "title": "Matched Book", "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    job = _run_job_to_completion(monkeypatch, source, meta, audnexus_chapters)

    assert job.status == Job.STATUS_DONE, job.log
    written = ffutil.get_embedded_chapters(Path(job.destination_path))
    assert written[1]["start_sec"] == pytest.approx(true_ch2_start, abs=0.5)
