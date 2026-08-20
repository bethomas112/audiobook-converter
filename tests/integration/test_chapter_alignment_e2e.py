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

from tests.helpers import make_tone_mp3, make_tone_silence_pattern_mp3


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
    assert "Chapter prep: 3 audnexus chapter(s)" in job.log
    assert "Aligned 3 chapter" in job.log


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


def test_no_silence_book_with_name_marker_chapters_folds_and_anchors_on_files(
    isolated_dirs, monkeypatch, huey_immediate
):
    """Synthetic reproduction of the real 28.6-hour audiobook that motivated the
    refined design (app/pipeline/chapters.py's _align_audnexus_chapters):

    - A narration with NO reliably strong silence anywhere - modelled here even
      more strictly than the real book (every real gap capped at 4.55s, stable
      across three silencedetect thresholds) as literally zero silence at all,
      guaranteeing achew's skeleton tier has nothing whatsoever to anchor on
      beyond chapter 0 (which the aligner always forces to position 0.0
      regardless of detected cues).
    - 69 raw audnexus chapters, 16 of them (~23%, matching the real book's
      proportion) ~2s POV-character-name markers ("Meg"/"Birdie" alternating,
      plus one "Part 2") with no acoustic boundary of their own - modelled as
      spoken over the opening of the real chapter that follows, sharing that
      chapter's own source file (so the *file* boundary for a real chapter
      isn't pushed later by a preceding marker - matching how these markers
      actually appear in a real rip: the file that contains the real chapter
      also opens with the marker).
    - 52 real source files for 53 real (post-fold) chapters - one real chapter
      short of a dedicated file, mirroring the real book's own file/chapter
      mismatch - close enough for the file-boundary prefilter (tolerance 2).

    Confirms the refined pipeline produces a SENSIBLE result on this
    reproduction: every marker folded away (never its own chapter), achew
    alone confidently placing only chapter 0 (there are no cues for it to
    skeleton-match beyond that), the rest rescued by verified file-boundary
    anchoring rather than being written as a fabricated, smoothly-drifting
    scale-interpolated guess (the old behaviour this design replaces).
    """
    from app.config import config

    monkeypatch.setattr(config, "OUTPUT_MODE", "standalone")
    monkeypatch.setattr(config, "STANDALONE_FILENAME_TEMPLATE", "{title}")

    # 52 source files, pure continuous tone each (no internal silence at all,
    # and each individually well over the 5s short-chapter floor).
    durations_cycle = [6.0, 7.0, 5.5, 8.0, 6.5]
    file_durations = [durations_cycle[i % len(durations_cycle)] for i in range(52)]
    source_dir = isolated_dirs["inbox"] / "No Silence Book"
    source_dir.mkdir()
    for i, dur in enumerate(file_durations):
        make_tone_mp3(source_dir / f"part{i:03d}.mp3", duration_sec=dur)

    # 53 "real" chapters - the first 52 lengths exactly match the 52 files
    # (so a chapter's own file-boundary and its own audnexus reference start
    # line up exactly), plus one trailing real chapter with no matching file.
    real_lengths = file_durations + [6.0]

    # 16 short (~2s) POV-name markers, spread through the 69-entry raw list -
    # each one immediately BEFORE the real chapter it gets folded onto.
    names = ["Meg", "Birdie"]
    marker_titles = [names[k % 2] for k in range(16)]
    marker_titles[8] = "Part 2"
    marker_before_real_index = sorted({round(k * 53 / 16) for k in range(16)})
    assert len(marker_before_real_index) == 16  # 53/16's spacing (~3.3) never collides when rounded
    marker_set = set(marker_before_real_index)

    raw_chapters = []
    t = 0.0
    marker_iter = iter(marker_titles)
    for real_idx in range(53):
        if real_idx in marker_set:
            raw_chapters.append({"start_sec": t, "end_sec": t + 2.0, "title": next(marker_iter)})
        length = real_lengths[real_idx]
        raw_chapters.append({"start_sec": t, "end_sec": t + length, "title": f"Chapter {real_idx + 1}"})
        t += length

    assert len(raw_chapters) == 69
    assert sum(1 for c in raw_chapters if (c["end_sec"] - c["start_sec"]) < 5.0) == 16

    meta = {
        "asin": "B0NOSILENCE", "title": "No Silence Book", "author": "", "narrator": "",
        "series": "", "series_index": "", "year": "", "genre": "", "description": "", "cover_url": "",
    }
    job = _run_job_to_completion(monkeypatch, source_dir, meta, raw_chapters)

    assert job.status == Job.STATUS_DONE, job.log
    written = ffutil.get_embedded_chapters(Path(job.destination_path))

    # No bare marker ever survives as its own chapter - every one got folded
    # onto whichever real chapter followed it.
    written_titles = [c["title"] for c in written]
    assert "Meg" not in written_titles
    assert "Birdie" not in written_titles
    assert "Part 2" not in written_titles
    assert any("Meg —" in t or "Birdie —" in t or "Part 2 —" in t for t in written_titles)

    # Folding actually happened - fewer chapters written than the raw 69.
    assert len(written) < 69

    # No fabricated smooth drift: with zero detected cues, achew can
    # confidently place only chapter 0 - everything else placed must come
    # from verified file-boundary anchoring, per the job log (not a guess).
    assert "Chapter prep: 69 audnexus chapter(s), 16 folded" in job.log
    assert "1 confidently matched" in job.log
    assert "File-boundary anchoring verified" in job.log

    # Chapter start times land on real, irregular file-boundary durations -
    # not a smooth linear interpolation across the book (a fabricated drift
    # curve would instead look uniform/near-constant spacing).
    starts = [c["start_sec"] for c in written]
    assert starts == sorted(starts)
    gaps = [round(b - a, 2) for a, b in zip(starts, starts[1:])]
    assert len(set(gaps)) > 1, f"chapter spacing looks suspiciously uniform: {gaps}"
