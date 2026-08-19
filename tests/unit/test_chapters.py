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
    _clamp_to_duration,
    _parse_silence_gaps,
    resolve_chapters,
)


def test_priority_1_embedded_chapters_wins_even_with_asin(monkeypatch):
    """An M4B that already has its own chapters must be left alone - the
    caller signal for "don't touch chapters" is a None return, even when
    an audnexus match (asin) is available.
    """
    called = {"audnexus": False}

    def fake_get_chapters(asin):
        called["audnexus"] = True
        return [{"start_sec": 0, "end_sec": 10, "title": "Ch1"}]

    monkeypatch.setattr(chapters_mod.metadata, "get_chapters", fake_get_chapters)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=True, asin="B123"
    )
    assert result is None
    assert called["audnexus"] is False


def test_priority_2_audnexus_used_when_no_embedded_chapters(monkeypatch):
    monkeypatch.setattr(
        chapters_mod.metadata, "get_chapters",
        lambda asin: [{"start_sec": 0, "end_sec": 100, "title": "Ch1"}],
    )
    monkeypatch.setattr(chapters_mod.ffutil, "get_duration_sec", lambda p: 100.0)

    result = resolve_chapters(
        "m4b_single", [Path("/x.m4b")], Path("/out.m4b"), has_embedded_chapters=False, asin="B123"
    )
    assert result == [{"start_sec": 0, "end_sec": 100, "title": "Ch1"}]


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
