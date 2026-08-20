"""Unit tests for app/pipeline/chapters.py - the four-way chapter-source
priority order from the planning doc (Section 2 step 8):

  1. embedded M4B chapters (left untouched)
  2. audnexus chapter data
  3. source-file boundaries (multi-file MP3)
  4. silence detection (last resort)

Network/subprocess calls (metadata.get_chapters, ffutil.get_duration_sec,
ffutil.run_silencedetect) are monkeypatched at the module level so this
file needs neither ffmpeg nor network access.
"""
from pathlib import Path

import pytest

from app.pipeline import chapters as chapters_mod
from app.pipeline.chapters import (
    _chapters_from_source_boundaries,
    _classify_placements,
    _clamp_to_duration,
    _cluster_shift_check,
    _fold_short_chapters,
    _fold_unresolved_placements,
    _parse_silence_gaps,
    _verify_file_boundaries,
    resolve_chapters,
)


def test_priority_1_embedded_chapters_wins_even_with_asin(monkeypatch):
    """An M4B that already has its own chapters, AND already carries the
    QuickTime-style chapter track Apple's apps need for real titles, must
    be left alone - the caller signal for "don't touch chapters" is a None
    return, even when an audnexus match (asin) is available.
    """
    called = {"audnexus": False}

    def fake_get_chapters(asin):
        called["audnexus"] = True
        return [{"start_sec": 0, "end_sec": 10, "title": "Ch1"}]

    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", fake_get_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "has_quicktime_chapter_track", lambda p: True)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=True, asin="B123"
    )
    assert result is None
    assert called["audnexus"] is False


def test_priority_1_embedded_chapters_missing_quicktime_track_gets_repaired(monkeypatch):
    """An M4B with its own chapters but missing the QuickTime-style chapter
    track (e.g. written by an older/different tool that only wrote the
    legacy Nero 'chpl' atom - the real gap this repair exists for) must
    have its existing chapter data returned for re-injection, not silently
    passed through broken. audnexus must still not be consulted - the
    source's own chapter *data* still wins, only the atom *format* needs
    fixing.
    """
    called = {"audnexus": False}

    def fake_get_chapters(asin):
        called["audnexus"] = True
        return [{"start_sec": 0, "end_sec": 10, "title": "WRONG - should not appear"}]

    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", fake_get_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "has_quicktime_chapter_track", lambda p: False)
    monkeypatch.setattr(
        chapters_mod.ffutil, "get_embedded_chapters",
        lambda p: [{"start_sec": 0, "end_sec": 10, "title": "Real Chapter Title"}],
    )

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=True, asin="B123"
    )
    assert result == [{"start_sec": 0, "end_sec": 10, "title": "Real Chapter Title"}]
    assert called["audnexus"] is False


def test_priority_2_audnexus_used_when_no_embedded_chapters(monkeypatch):
    """A single-chapter audnexus result with no detected cues: the aligner
    has nothing to snap to, so chapter 0 (always forced to 0.0) is the only
    chapter and passes through unchanged. run_silencedetect is explicitly
    mocked (rather than left to hit a nonexistent /out.m4b through real
    ffmpeg) so this stays deterministic and independent of ffmpeg's error
    behavior on a missing file.
    """
    monkeypatch.setattr(
        chapters_mod.metadata, "get_chapters",
        lambda asin: [{"start_sec": 0, "end_sec": 100, "title": "Ch1"}],
    )
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 100.0)
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )
    assert result == [{"start_sec": 0.0, "end_sec": 100, "title": "Ch1"}]


def test_priority_2_audnexus_chapters_realigned_to_detected_silence(monkeypatch):
    """The actual bug fix: audnexus's chapter timestamps (0/100/200, evenly
    spaced) must not be written verbatim when the real converted audio's
    silences sit somewhere else (96/205) - each chapter is individually
    snapped to the nearest real cue instead of trusting audnexus's absolute
    offset, per app/pipeline/chapter_aligner.py (ported from achew).
    """
    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": 100.0, "title": "Ch1"},
        {"start_sec": 100.0, "end_sec": 200.0, "title": "Ch2"},
        {"start_sec": 200.0, "end_sec": 300.0, "title": "Ch3"},
    ]
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: audnexus_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 300.0)

    # Real silences at 96s and 205s (a strong 3s gap each) - not at audnexus's
    # 100/200. silencedetect's stderr format: silence_start comes before
    # silence_end for the same gap.
    stderr = (
        "silence_start: 93.333333\nsilence_end: 96.333333 | silence_duration: 3.0\n"
        "silence_start: 202.333333\nsilence_end: 205.333333 | silence_duration: 3.0\n"
    )
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: stderr)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )

    assert [c["title"] for c in result] == ["Ch1", "Ch2", "Ch3"]
    starts = [c["start_sec"] for c in result]
    assert starts[0] == 0.0
    assert starts[1] == pytest.approx(96.0, abs=0.01)
    assert starts[2] == pytest.approx(205.0, abs=0.01)
    # Contiguous: each chapter's end is the next one's (aligned) start.
    assert result[0]["end_sec"] == pytest.approx(96.0, abs=0.01)
    assert result[1]["end_sec"] == pytest.approx(205.0, abs=0.01)
    assert result[2]["end_sec"] == 300.0


def test_priority_2_alignment_outcome_is_logged(monkeypatch):
    """Task requirement: the realignment outcome (chapter prep/folding,
    confident vs. guessed with median/max shift, and the final per-path
    breakdown) must be logged via job.append_log so it's visible in job
    history without digging into code - no user-facing confirmation gate
    exists at this point in the pipeline (chapters resolve at ~92% of
    conversion progress, well past the metadata-confirm step). Both
    chapters here are >=5s (no short-chapter folding) and this is a
    non-mp3_multi source (no file-boundary line), so exactly three log
    lines are expected: prep, alignment, and final breakdown.
    """
    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": 100.0, "title": "Ch1"},
        {"start_sec": 100.0, "end_sec": 200.0, "title": "Ch2"},
    ]
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: audnexus_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 200.0)
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")

    logged = []
    resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"),
        has_embedded_chapters=False, asin="B123", log=logged.append,
    )

    assert len(logged) == 3
    assert "Chapter prep: 2 audnexus chapter(s)" in logged[0]
    assert "0 folded" in logged[0]

    assert "Aligned 2 chapter" in logged[1]
    assert "confidently matched" in logged[1]
    assert "guesses" in logged[1]
    assert "median shift" in logged[1] and "max shift" in logged[1]

    assert "Final chapters:" in logged[2]
    assert "placed by achew" in logged[2]
    assert "placed via verified file-boundary anchoring" in logged[2]
    assert "folded onto a neighbouring resolved chapter" in logged[2]


def test_priority_2_alignment_output_is_forced_monotonic(monkeypatch):
    """Safety net (app/pipeline/chapters.py's _align_audnexus_chapters): even
    if the aligner itself ever produced out-of-order timestamps for some
    pathological input, downstream ffmetadata writing can't represent a
    chapter starting before its predecessor - so this is clamped
    defensively rather than trusted blind. All three chapters here are
    achew-confident (is_guess False) so none get folded away - this
    isolates the monotonicity safety net itself from the new fold-when-
    unconfident behaviour (see test_priority_2_unconfident_chapter_is_folded_not_kept
    for that).
    """
    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": 50.0, "title": "Ch1"},
        {"start_sec": 50.0, "end_sec": 100.0, "title": "Ch2"},
        {"start_sec": 100.0, "end_sec": 150.0, "title": "Ch3"},
    ]
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: audnexus_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 150.0)
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")

    class _FakeAligner:
        def __init__(self, *a, **k):
            pass

        def align(self, ref_chapters, detected_cues, total_duration_ref, total_duration_actual, scanned_regions=None):
            # Deliberately non-monotonic: chapter 2 lands BEFORE chapter 1,
            # even though both are (implausibly, but the safety net must not
            # assume otherwise) reported as confident matches.
            return (
                [
                    {"title": "Ch1", "timestamp": 0.0, "confidence": 1.0, "is_guess": False, "matched_silence": 0.0},
                    {"title": "Ch2", "timestamp": 80.0, "confidence": 0.85, "is_guess": False, "matched_silence": 2.0},
                    {"title": "Ch3", "timestamp": 60.0, "confidence": 0.85, "is_guess": False, "matched_silence": 2.0},
                ],
                {"scale": 1.0, "offset": 0.0, "expansion_needed": False},
            )

    monkeypatch.setattr(chapters_mod, "ChapterAligner", _FakeAligner)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )

    assert [c["title"] for c in result] == ["Ch1", "Ch2", "Ch3"]  # all confident, none folded
    starts = [c["start_sec"] for c in result]
    assert starts == sorted(starts), f"chapter starts not monotonic: {starts}"
    assert starts[2] == 80.0  # clamped up to chapter 2's start, not left at 60.0


def test_priority_2_unconfident_chapter_is_folded_not_kept(monkeypatch):
    """The refined design's core behaviour change: a chapter achew flags as
    a guess (is_guess True) is no longer written at its guessed position -
    it's folded onto the PRECEDING confidently-placed chapter's title
    instead (here, with no mp3_multi file-boundary anchoring available to
    rescue it either), since that's the marker whose span actually grows to
    contain the unresolved chapter's audio - not the one that follows. No
    "Chapter 2" marker should exist in the output at all.
    """
    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": 50.0, "title": "Chapter 1"},
        {"start_sec": 50.0, "end_sec": 100.0, "title": "Chapter 2"},
        {"start_sec": 100.0, "end_sec": 150.0, "title": "Chapter 3"},
    ]
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: audnexus_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 150.0)
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")

    class _FakeAligner:
        def __init__(self, *a, **k):
            pass

        def align(self, ref_chapters, detected_cues, total_duration_ref, total_duration_actual, scanned_regions=None):
            return (
                [
                    {"title": "Chapter 1", "timestamp": 0.0, "confidence": 1.0, "is_guess": False, "matched_silence": 0.0},
                    {"title": "Chapter 2", "timestamp": 55.0, "confidence": 0.25, "is_guess": True, "matched_silence": 0.0},
                    {"title": "Chapter 3", "timestamp": 100.0, "confidence": 0.85, "is_guess": False, "matched_silence": 2.0},
                ],
                {"scale": 1.0, "offset": 0.0, "expansion_needed": False},
            )

    monkeypatch.setattr(chapters_mod, "ChapterAligner", _FakeAligner)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )

    assert [c["title"] for c in result] == ["Chapter 1 — Chapter 2", "Chapter 3"]
    assert [c["start_sec"] for c in result] == [0.0, 100.0]
    assert result[0]["end_sec"] == 100.0
    assert result[1]["end_sec"] == 150.0


def test_priority_2_trailing_unconfident_chapter_folds_onto_previous(monkeypatch):
    """The last chapter in the book is the one achew couldn't confidently
    place, and there's no verified file-boundary source to rescue it (not
    mp3_multi) - it folds onto the PRECEDING resolved chapter, since there
    is no later resolved chapter to fold onto."""
    audnexus_chapters = [
        {"start_sec": 0.0, "end_sec": 50.0, "title": "Chapter 1"},
        {"start_sec": 50.0, "end_sec": 100.0, "title": "Chapter 2"},
        {"start_sec": 100.0, "end_sec": 150.0, "title": "Epilogue"},
    ]
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: audnexus_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 150.0)
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")

    class _FakeAligner:
        def __init__(self, *a, **k):
            pass

        def align(self, ref_chapters, detected_cues, total_duration_ref, total_duration_actual, scanned_regions=None):
            return (
                [
                    {"title": "Chapter 1", "timestamp": 0.0, "confidence": 1.0, "is_guess": False, "matched_silence": 0.0},
                    {"title": "Chapter 2", "timestamp": 48.0, "confidence": 0.85, "is_guess": False, "matched_silence": 2.0},
                    {"title": "Epilogue", "timestamp": 140.0, "confidence": 0.25, "is_guess": True, "matched_silence": 0.0},
                ],
                {"scale": 1.0, "offset": 0.0, "expansion_needed": False},
            )

    monkeypatch.setattr(chapters_mod, "ChapterAligner", _FakeAligner)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )

    assert [c["title"] for c in result] == ["Chapter 1", "Chapter 2 — Epilogue"]
    assert [c["start_sec"] for c in result] == [0.0, 48.0]
    assert result[-1]["end_sec"] == 150.0


def test_priority_2_long_run_of_consecutive_unconfident_chapters_folds_onto_preceding(monkeypatch):
    """End-to-end reproduction (through resolve_chapters(), not just the
    _fold_unresolved_placements unit test) of the real-world failure that
    shipped in commit 84e52b3 and got fixed here: a long run of MANY
    consecutive unresolved chapters (15, mirroring the real book's report of
    ~9-hour unlabeled blocks - the severity scales with run length, so a
    1-2 chapter run wouldn't have caught this) between two achew-confident
    chapters. Confirms every written marker's title accurately describes
    the audio its span actually contains: no marker exists whose title
    describes content positioned after that marker's own span ends.
    """
    n_chapters = 17
    length = 500.0
    audnexus_chapters = [
        {"start_sec": i * length, "end_sec": (i + 1) * length, "title": f"Chapter {i + 1}"}
        for i in range(n_chapters)
    ]
    total_duration = n_chapters * length
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: audnexus_chapters)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: total_duration)
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")

    # Only the first and last chapters are achew-confident - a 15-chapter
    # unresolved run sits between them, exactly like the real book (no
    # reliable silence anywhere for achew to skeleton-match beyond the
    # forced chapter-0 anchor) and with no mp3_multi file-boundary source
    # available to rescue any of them either.
    class _FakeAligner:
        def __init__(self, *a, **k):
            pass

        def align(self, ref_chapters, detected_cues, total_duration_ref, total_duration_actual, scanned_regions=None):
            results = []
            for i, c in enumerate(audnexus_chapters):
                confident = i == 0 or i == n_chapters - 1
                results.append({
                    "title": c["title"],
                    "timestamp": c["start_sec"],
                    "confidence": 1.0 if i == 0 else (0.85 if confident else 0.25),
                    "is_guess": not confident,
                    "matched_silence": 0.0,
                })
            return results, {"scale": 1.0, "offset": 0.0, "expansion_needed": False}

    monkeypatch.setattr(chapters_mod, "ChapterAligner", _FakeAligner)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )

    # Exactly 2 markers written - not 17, not 3+.
    assert len(result) == 2

    expected_compound_title = " — ".join(f"Chapter {i + 1}" for i in range(n_chapters - 1))
    assert result[0]["title"] == expected_compound_title
    assert result[0]["start_sec"] == 0.0  # chapter 1's own position, unchanged

    assert result[1]["title"] == f"Chapter {n_chapters}"
    assert result[1]["start_sec"] == (n_chapters - 1) * length  # chapter 17's own position, unchanged
    assert result[1]["end_sec"] == total_duration

    # The critical property this bug violated: every marker's span must
    # actually contain everything its title claims to cover. The compound
    # marker's span [start, next_start) must extend at least up to the
    # position of the LAST chapter folded into its title (chapter 16's
    # audnexus reference start) - i.e. the marker's own position, not some
    # later chapter's position, anchors the start of everything it names.
    last_folded_chapter_ref_start = audnexus_chapters[n_chapters - 2]["start_sec"]  # "Chapter 16"
    assert result[0]["start_sec"] <= last_folded_chapter_ref_start < result[0]["end_sec"]


def test_priority_3_source_boundaries_when_audnexus_empty_and_multi_file(monkeypatch, tmp_path):
    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", lambda asin: [])

    files = []
    for i, dur in enumerate([30.0, 45.0]):
        p = tmp_path / f"track{i}.mp3"
        p.write_bytes(b"fake")
        files.append(p)
    monkeypatch.setattr(
        chapters_mod.ffutil, "get_duration_sec",
        lambda p: {files[0]: 30.0, files[1]: 45.0}[p],
    )

    result = resolve_chapters("mp3_multi", files, tmp_path / "out.m4b", has_embedded_chapters=False, asin="B123")

    assert result == [
        {"start_sec": 0.0, "end_sec": 30.0, "title": "track0"},
        {"start_sec": 30.0, "end_sec": 75.0, "title": "track1"},
    ]


def test_priority_3_source_boundaries_when_no_asin_and_multi_file(monkeypatch, tmp_path):
    files = [tmp_path / "a.mp3", tmp_path / "b.mp3"]
    for f in files:
        f.write_bytes(b"fake")
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 10.0)

    result = resolve_chapters("mp3_multi", files, tmp_path / "out.m4b", has_embedded_chapters=False, asin=None)

    assert len(result) == 2


def test_priority_4_silence_detection_last_resort_for_single_stream(monkeypatch, tmp_path):
    """No embedded chapters, no asin, and a single-file source (not
    mp3_multi) - must fall all the way through to silence detection.
    """
    from app.config import config

    monkeypatch.setattr(config, "SILENCE_MIN_CHAPTER_SEC", 40)
    stderr = "silence_start: 50.0\nsilence_end: 52.0\nsilence_start: 110.0\nsilence_end: 113.0\n"
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: stderr)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 200.0)

    result = resolve_chapters(
        "mp3_single", [tmp_path / "book.mp3"], tmp_path / "out.m4b", has_embedded_chapters=False, asin=None
    )

    assert result[0]["start_sec"] == 0.0
    assert result[-1]["end_sec"] == 200.0
    assert len(result) >= 2


def test_priority_4_silence_detection_when_m4b_has_no_embedded_and_no_asin_match(monkeypatch, tmp_path):
    """An M4B input with neither embedded chapters nor an audnexus match -
    the doc's other named last-resort scenario for silence detection.
    """
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: "")
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 60.0)

    result = resolve_chapters(
        "m4b_single", [tmp_path / "book.m4b"], tmp_path / "out.m4b", has_embedded_chapters=False, asin=None
    )
    assert result == [{"start_sec": 0.0, "end_sec": 60.0, "title": "Chapter 1"}]


def test_parse_silence_gaps_extracts_start_end_pairs():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 12.5\n"
        "[silencedetect @ 0x1] silence_end: 14.25 | silence_duration: 1.75\n"
        "[silencedetect @ 0x1] silence_start: 300.0\n"
        "[silencedetect @ 0x1] silence_end: 302.1 | silence_duration: 2.1\n"
    )
    gaps = _parse_silence_gaps(stderr)
    assert gaps == [(12.5, 14.25), (300.0, 302.1)]


def test_silence_breaks_merge_when_closer_than_min_chapter_sec(monkeypatch, tmp_path):
    """A candidate break only 30s after the previous *kept* break must merge
    away (stay unsplit) when SILENCE_MIN_CHAPTER_SEC=120, per the doc's
    "don't over-split on pauses mid-sentence" requirement - while a later
    candidate far enough away still survives.
    """
    from app.config import config

    monkeypatch.setattr(config, "SILENCE_MIN_CHAPTER_SEC", 120)
    monkeypatch.setattr(config, "SILENCE_THRESHOLD_DB", "-30dB")
    monkeypatch.setattr(config, "SILENCE_MIN_DURATION_SEC", 1.5)

    stderr = (
        "silence_start: 148.0\nsilence_end: 150.0\n"  # 150s after start -> kept
        "silence_start: 178.0\nsilence_end: 180.0\n"  # only 30s after the kept break -> merged away
        "silence_start: 318.0\nsilence_end: 320.0\n"  # 170s after the kept break -> kept
    )
    monkeypatch.setattr(chapters_mod.ffutil, "run_silencedetect", lambda *a, **k: stderr)
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 500.0)

    result = resolve_chapters(
        "mp3_single", [tmp_path / "book.mp3"], tmp_path / "out.m4b", has_embedded_chapters=False, asin=None
    )

    starts = [c["start_sec"] for c in result]
    assert starts == [0.0, 150.0, 320.0]
    assert result[-1]["end_sec"] == 500.0


def test_clamp_to_duration_drops_chapters_starting_past_end():
    chapters = [
        {"start_sec": 0, "end_sec": 100, "title": "A"},
        {"start_sec": 100, "end_sec": 200, "title": "B"},
        {"start_sec": 200, "end_sec": 300, "title": "C"},
    ]
    result = _clamp_to_duration(chapters, duration_sec=150.0)
    assert [c["title"] for c in result] == ["A", "B"]
    assert result[-1]["end_sec"] == 150.0


def test_clamp_to_duration_noop_when_chapters_fit(monkeypatch):
    chapters = [{"start_sec": 0, "end_sec": 100, "title": "A"}]
    result = _clamp_to_duration(chapters, duration_sec=200.0)
    assert result == chapters


def test_chapters_from_source_boundaries_uses_file_stem_as_title(monkeypatch, tmp_path):
    files = [tmp_path / "Chapter 01.mp3", tmp_path / "Chapter 02.mp3"]
    for f in files:
        f.write_bytes(b"fake")
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 60.0)

    result = _chapters_from_source_boundaries(files)
    assert [c["title"] for c in result] == ["Chapter 01", "Chapter 02"]


# ── Step 1: _fold_short_chapters ────────────────────────────────────────────


def test_fold_short_chapters_folds_name_marker_onto_next_chapter():
    """The real-world case this step exists for: a ~2s POV-name marker
    ("Meg") immediately before a real chapter gets dropped and its title
    folded onto that chapter, em-dash-joined."""
    chapters = [
        {"start_sec": 0.0, "end_sec": 2.0, "title": "Meg"},
        {"start_sec": 2.0, "end_sec": 602.0, "title": "Chapter 1"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert short_count == 1
    assert [c["title"] for c in cleaned] == ["Meg — Chapter 1"]
    assert cleaned[0]["start_sec"] == 2.0
    assert cleaned[0]["end_sec"] == 602.0


def test_fold_short_chapters_last_chapter_edge_case_folds_onto_previous():
    """A short chapter with no "next" chapter to fold onto (it's the very
    last one in the book) must fold onto the PRECEDING kept chapter
    instead, not crash and not silently drop the title."""
    chapters = [
        {"start_sec": 0.0, "end_sec": 500.0, "title": "Chapter 1"},
        {"start_sec": 500.0, "end_sec": 502.0, "title": "Birdie"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert short_count == 1
    assert [c["title"] for c in cleaned] == ["Chapter 1 — Birdie"]
    assert cleaned[0]["start_sec"] == 0.0
    assert cleaned[0]["end_sec"] == 500.0  # the kept chapter's OWN end, unchanged


def test_fold_short_chapters_multiple_consecutive_short_chapters_chain_in_order():
    chapters = [
        {"start_sec": 0.0, "end_sec": 2.0, "title": "Meg"},
        {"start_sec": 2.0, "end_sec": 4.0, "title": "Birdie"},
        {"start_sec": 4.0, "end_sec": 604.0, "title": "Chapter 1"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert short_count == 2
    assert [c["title"] for c in cleaned] == ["Meg — Birdie — Chapter 1"]


def test_fold_short_chapters_real_short_front_matter_not_caught():
    """A genuinely real, short front-matter chapter ("Dedication" at 8.4s,
    the exact figure observed in the investigation this design responds
    to) must NOT be folded away - only chapters under the 5s floor are."""
    chapters = [
        {"start_sec": 0.0, "end_sec": 8.4, "title": "Dedication"},
        {"start_sec": 8.4, "end_sec": 608.4, "title": "Chapter 1"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert short_count == 0
    assert [c["title"] for c in cleaned] == ["Dedication", "Chapter 1"]


def test_fold_short_chapters_length_is_own_not_neighbour_spacing():
    """A chapter's own reported length is the signal - not the gap to its
    neighbour. A short chapter followed by a huge gap before the next
    survives-by-neighbour-distance argument must still be folded, because
    ITS OWN length is what's under the floor."""
    chapters = [
        {"start_sec": 0.0, "end_sec": 3.0, "title": "Meg"},  # own length 3s: short
        # a huge (contrived) gap before the next chapter starts - irrelevant
        {"start_sec": 500.0, "end_sec": 1100.0, "title": "Chapter 1"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert short_count == 1
    assert [c["title"] for c in cleaned] == ["Meg — Chapter 1"]


def test_fold_short_chapters_degenerate_all_short_does_not_crash_or_drop_titles():
    chapters = [
        {"start_sec": 0.0, "end_sec": 1.0, "title": "A"},
        {"start_sec": 1.0, "end_sec": 2.0, "title": "B"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert len(cleaned) == 1
    assert cleaned[0]["title"] == "A — B"


def test_fold_short_chapters_no_short_chapters_is_a_noop():
    chapters = [
        {"start_sec": 0.0, "end_sec": 100.0, "title": "Chapter 1"},
        {"start_sec": 100.0, "end_sec": 200.0, "title": "Chapter 2"},
    ]
    cleaned, short_count = _fold_short_chapters(chapters, min_sec=5.0)

    assert short_count == 0
    assert cleaned == chapters


# ── Step 3: _classify_placements / _fold_unresolved_placements ─────────────


def test_classify_placements_achew_confident_wins_over_file_boundary():
    cleaned = [{"start_sec": 0.0, "end_sec": 100.0, "title": "Ch1"}]
    results = [{"timestamp": 5.0, "is_guess": False}]
    file_boundaries = {"positions": {0: 999.0}}

    placements = _classify_placements(cleaned, results, file_boundaries)
    assert placements == [{"title": "Ch1", "position": 5.0, "source": "achew"}]


def test_classify_placements_falls_back_to_file_boundary_when_unconfident():
    cleaned = [{"start_sec": 0.0, "end_sec": 100.0, "title": "Ch1"}]
    results = [{"timestamp": 5.0, "is_guess": True}]
    file_boundaries = {"positions": {0: 42.0}}

    placements = _classify_placements(cleaned, results, file_boundaries)
    assert placements == [{"title": "Ch1", "position": 42.0, "source": "file_boundary"}]


def test_classify_placements_unresolved_when_neither_available():
    cleaned = [{"start_sec": 0.0, "end_sec": 100.0, "title": "Ch1"}]
    results = [{"timestamp": 5.0, "is_guess": True}]

    placements = _classify_placements(cleaned, results, None)
    assert placements == [{"title": "Ch1", "position": None, "source": None}]


def test_fold_unresolved_placements_folds_middle_run_onto_preceding_resolved():
    """The bug fix's core case: title and span must agree. A chapter's span
    is always [its own position, the next resolved chapter's position), so
    a run of unresolved chapters between two resolved ones must fold onto
    the PRECEDING resolved chapter (whose span already extends forward to
    swallow them) - never the one that follows, which would attach the
    compound title to a marker positioned well past everything it claims to
    cover. This is the exact shape of the real-world failure (commit
    84e52b3): a long run of consecutive unresolved chapters (here 14, one
    short of the worked example in the task write-up) between two resolved
    ones must produce exactly 2 output markers, not 3+.
    """
    placements = [
        {"title": "Chapter 21", "position": 2100.0, "source": "achew"},
        *[{"title": f"Chapter {n}", "position": None, "source": None} for n in range(22, 36)],
        {"title": "Chapter 36", "position": 3600.0, "source": "achew"},
    ]
    resolved = _fold_unresolved_placements(placements)

    assert len(resolved) == 2

    expected_compound_title = "Chapter 21 — " + " — ".join(f"Chapter {n}" for n in range(22, 36))
    assert resolved[0]["title"] == expected_compound_title
    assert resolved[0]["position"] == 2100.0  # chapter 21's OWN original position, unchanged

    # Chapter 36 is completely untouched: its own title and its own position.
    assert resolved[1]["title"] == "Chapter 36"
    assert resolved[1]["position"] == 3600.0


def test_fold_unresolved_placements_trailing_run_folds_onto_previous():
    placements = [
        {"title": "A", "position": 0.0, "source": "achew"},
        {"title": "B", "position": None, "source": None},
    ]
    resolved = _fold_unresolved_placements(placements)
    assert [(r["title"], r["position"]) for r in resolved] == [("A — B", 0.0)]


def test_fold_unresolved_placements_leading_run_before_any_resolved_chapter():
    """Edge case: unresolved chapters appearing before the very first
    resolved chapter. Chapter 0 is documented as always achew-confident (so
    this shouldn't occur in practice), but the pending_leading_titles
    fallback must still work correctly rather than silently dropping
    titles or crashing if that assumption is ever violated.
    """
    placements = [
        {"title": "Intro A", "position": None, "source": None},
        {"title": "Intro B", "position": None, "source": None},
        {"title": "Chapter 1", "position": 10.0, "source": "achew"},
    ]
    resolved = _fold_unresolved_placements(placements)
    assert [(r["title"], r["position"]) for r in resolved] == [("Intro A — Intro B — Chapter 1", 10.0)]


def test_fold_unresolved_placements_degenerate_all_unresolved_does_not_crash_or_drop_titles():
    """Should not occur - chapter 0 is always achew-confident - but the
    defensive fallback must still produce a sane, non-crashing result if no
    chapter in the whole book ever resolves."""
    placements = [
        {"title": "A", "position": None, "source": None},
        {"title": "B", "position": None, "source": None},
    ]
    resolved = _fold_unresolved_placements(placements)
    assert len(resolved) == 1
    assert resolved[0]["title"] == "A — B"
    assert resolved[0]["position"] == 0.0


# ── Step 4: _cluster_shift_check / _verify_file_boundaries ─────────────────


def test_cluster_shift_check_strong_majority_verified():
    cleaned = [{"start_sec": t} for t in (0.0, 80.0, 175.0, 285.0, 355.0)]
    pairs = [(0, 0.0), (1, 80.0), (2, 175.0), (3, 285.0), (4, 355.0)]
    majority_fraction, shift = _cluster_shift_check(pairs, cleaned)
    assert majority_fraction == 1.0
    assert shift == pytest.approx(0.0)


def test_cluster_shift_check_no_consistent_shift_low_majority():
    cleaned = [{"start_sec": t} for t in (0.0, 80.0, 175.0, 285.0, 355.0)]
    pairs = [(0, 0.0), (1, 30.0), (2, 50.0), (3, 130.0), (4, 225.0)]  # all-different shifts
    majority_fraction, _shift = _cluster_shift_check(pairs, cleaned)
    assert majority_fraction < 0.8


def _mock_file_durations(monkeypatch, files, durations):
    lookup = dict(zip(files, durations))
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: lookup[p])


def test_verify_file_boundaries_skips_on_large_count_mismatch(tmp_path):
    """Cheap prefilter: a file count far from the (cleaned) chapter count
    must reject before ever touching ffutil.get_duration_sec - no
    monkeypatch is installed here, so a call to it would raise/hang."""
    cleaned = [{"start_sec": float(i * 100), "end_sec": float((i + 1) * 100), "title": f"Ch{i}"} for i in range(20)]
    files = [tmp_path / f"t{i}.mp3" for i in range(2)]
    assert _verify_file_boundaries(cleaned, files) is None


def test_verify_file_boundaries_front_anchored_when_excess_files_at_back(monkeypatch, tmp_path):
    """Bonus/back-matter tracks the audnexus reference doesn't know about
    sit AFTER the real chapter files - front-anchored (chapter 0 <-> file
    0, trim excess off the back) is the direction that produces a tight,
    consistent shift here, and must be the one selected."""
    lengths = [80.0, 95.0, 110.0, 70.0, 60.0]
    starts = [0.0]
    for length in lengths[:-1]:
        starts.append(starts[-1] + length)
    cleaned = [
        {"start_sec": s, "end_sec": s + l, "title": f"Chapter {i + 1}"}
        for i, (s, l) in enumerate(zip(starts, lengths))
    ]

    file_durations = lengths + [40.0, 45.0]  # 2 extra bonus tracks appended after
    files = [tmp_path / f"f{i}.mp3" for i in range(len(file_durations))]
    _mock_file_durations(monkeypatch, files, file_durations)

    result = _verify_file_boundaries(cleaned, files)

    assert result is not None
    assert result["direction"] == "front"
    assert result["majority_fraction"] == 1.0
    assert result["shift"] == pytest.approx(0.0)
    # Every cleaned chapter placed at its own real file boundary.
    assert result["positions"] == {0: 0.0, 1: 80.0, 2: 175.0, 3: 285.0, 4: 355.0}


def test_verify_file_boundaries_back_anchored_when_excess_files_at_front(monkeypatch, tmp_path):
    """An unripped intro (extra file(s) the reference doesn't cover) sits
    BEFORE the real chapter files - back-anchored (the last chapter <-> the
    last file, trim excess off the front) is the direction that produces a
    tight, consistent shift here, and must be the one selected."""
    lengths = [80.0, 95.0, 110.0, 70.0, 60.0]
    starts = [0.0]
    for length in lengths[:-1]:
        starts.append(starts[-1] + length)
    cleaned = [
        {"start_sec": s, "end_sec": s + l, "title": f"Chapter {i + 1}"}
        for i, (s, l) in enumerate(zip(starts, lengths))
    ]

    file_durations = [30.0, 20.0] + lengths  # 2 extra intro tracks prepended before
    files = [tmp_path / f"f{i}.mp3" for i in range(len(file_durations))]
    _mock_file_durations(monkeypatch, files, file_durations)

    result = _verify_file_boundaries(cleaned, files)

    assert result is not None
    assert result["direction"] == "back"
    assert result["majority_fraction"] == 1.0
    assert result["shift"] == pytest.approx(50.0)
    # Every cleaned chapter placed at its own real file boundary (file
    # index 2 onward - files 0/1 are the trimmed-away intro tracks).
    assert result["positions"] == {0: 50.0, 1: 130.0, 2: 225.0, 3: 335.0, 4: 405.0}


def test_verify_file_boundaries_rejects_when_neither_direction_clusters(monkeypatch, tmp_path):
    """Both directions produce inconsistent, scattered shifts (no real
    file<->chapter correspondence) - file-boundary anchoring must be
    rejected for the whole book rather than picking a weak winner."""
    cleaned = [{"start_sec": float(t), "end_sec": float(t + 10), "title": f"Ch{i}"}
               for i, t in enumerate([0, 37, 61, 122, 205])]
    file_durations = [13.0, 61.0, 8.0, 94.0, 3.0]  # unrelated to the reference spacing
    files = [tmp_path / f"f{i}.mp3" for i in range(len(file_durations))]
    _mock_file_durations(monkeypatch, files, file_durations)

    assert _verify_file_boundaries(cleaned, files) is None


def test_verify_file_boundaries_tie_prefers_back_anchored(monkeypatch, tmp_path):
    """Equal file and chapter counts, with both directions producing an
    identical, fully-consistent pairing (front == back when nc == nf) -
    the tie-break must default to back-anchored per the app owner's stated
    preference."""
    lengths = [80.0, 95.0, 110.0]
    starts = [0.0, 80.0, 175.0]
    cleaned = [
        {"start_sec": s, "end_sec": s + l, "title": f"Chapter {i + 1}"}
        for i, (s, l) in enumerate(zip(starts, lengths))
    ]
    files = [tmp_path / f"f{i}.mp3" for i in range(3)]
    _mock_file_durations(monkeypatch, files, lengths)

    result = _verify_file_boundaries(cleaned, files)
    assert result is not None
    assert result["direction"] == "back"
