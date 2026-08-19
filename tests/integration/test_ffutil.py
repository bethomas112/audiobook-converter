"""Integration tests for app/pipeline/ffutil.py against real ffmpeg/ffprobe
on synthetic audio (tests/helpers.py). Requires ffmpeg on PATH - see
README's "Development" section, which already assumes the same.
"""
import re

import pytest

from app.pipeline import ffutil
from tests.helpers import (
    has_quicktime_chapter_text_track,
    make_m4b,
    make_tone_mp3,
    make_tone_silence_pattern_mp3,
    strip_quicktime_chapter_track,
)


def test_get_duration_sec_matches_generated_length(tmp_path):
    f = make_tone_mp3(tmp_path / "tone.mp3", duration_sec=3.0)
    duration = ffutil.get_duration_sec(f)
    assert duration == pytest.approx(3.0, abs=0.2)


def test_get_audio_bitrate_kbps_matches_encoded_bitrate(tmp_path):
    f = make_tone_mp3(tmp_path / "tone.mp3", duration_sec=2.0, bitrate_kbps=64)
    bitrate = ffutil.get_audio_bitrate_kbps(f)
    assert bitrate == pytest.approx(64, abs=8)


def test_get_embedded_chapters_empty_for_plain_file(tmp_path):
    f = make_m4b(tmp_path / "plain.m4b", duration_sec=2.0)
    assert ffutil.get_embedded_chapters(f) == []


def test_get_embedded_chapters_reads_back_written_chapters(tmp_path):
    chapters = [
        {"start_sec": 0.0, "end_sec": 1.0, "title": "Intro"},
        {"start_sec": 1.0, "end_sec": 2.0, "title": "Outro"},
    ]
    f = make_m4b(tmp_path / "chaptered.m4b", duration_sec=2.0, chapters=chapters)
    result = ffutil.get_embedded_chapters(f)
    assert [c["title"] for c in result] == ["Intro", "Outro"]
    assert result[0]["start_sec"] == pytest.approx(0.0, abs=0.05)
    assert result[1]["start_sec"] == pytest.approx(1.0, abs=0.05)


def test_run_silencedetect_finds_known_gap(tmp_path):
    f, ground_truth = make_tone_silence_pattern_mp3(
        tmp_path / "pattern.mp3",
        segments=[("tone", 2.0), ("silence", 2.0), ("tone", 2.0)],
    )
    stderr = ffutil.run_silencedetect(f, threshold_db="-30dB", min_duration_sec=1.0)
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", stderr)]
    assert len(starts) == 1 and len(ends) == 1
    expected_start, expected_end = ground_truth[0]
    assert starts[0] == pytest.approx(expected_start, abs=0.3)
    assert ends[0] == pytest.approx(expected_end, abs=0.3)


def test_probe_raises_ffe_error_on_nonexistent_file(tmp_path):
    with pytest.raises(ffutil.FFError):
        ffutil.probe(tmp_path / "does_not_exist.mp3")


def test_inject_chapters_ffmetadata_writes_readable_chapters(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=3.0)
    chapters = [
        {"start_sec": 0.0, "end_sec": 1.5, "title": "Part One"},
        {"start_sec": 1.5, "end_sec": 3.0, "title": "Part Two"},
    ]
    ffutil.inject_chapters_ffmetadata(f, chapters)
    result = ffutil.get_embedded_chapters(f)
    assert [c["title"] for c in result] == ["Part One", "Part Two"]


def test_inject_chapters_ffmetadata_writes_quicktime_chapter_track(tmp_path):
    """ffprobe's -show_chapters (get_embedded_chapters) can't tell a
    QuickTime-style chapter track apart from the legacy Nero 'chpl' atom -
    both read back the same chapter list. This is the atom-level check:
    inject_chapters_ffmetadata must write the actual QuickTime track (the
    structure Apple's Books/Music/Podcasts/QuickTime need for real chapter
    titles instead of generic "1", "2", "3" numbering), not just chpl.
    """
    f = make_m4b(tmp_path / "book.m4b", duration_sec=3.0)
    chapters = [
        {"start_sec": 0.0, "end_sec": 1.5, "title": "Part One"},
        {"start_sec": 1.5, "end_sec": 3.0, "title": "Part Two"},
    ]
    ffutil.inject_chapters_ffmetadata(f, chapters)
    assert ffutil.has_quicktime_chapter_track(f) is True
    assert has_quicktime_chapter_text_track(f) is True  # independent raw-box confirmation


def test_has_quicktime_chapter_track_false_for_plain_file(tmp_path):
    f = make_m4b(tmp_path / "plain.m4b", duration_sec=2.0)
    assert ffutil.has_quicktime_chapter_track(f) is False


def test_has_quicktime_chapter_track_false_when_only_chpl_atom_present(tmp_path):
    """Simulates a source .m4b from some other/older ffmpeg-based tool that
    only ever wrote the legacy Nero chpl atom (a real, historically common
    gap) - the exact case this pipeline's chapter-track repair exists for.
    """
    chapters = [{"start_sec": 0.0, "end_sec": 2.0, "title": "Ch1"}]
    f = make_m4b(tmp_path / "legacy.m4b", duration_sec=2.0, chapters=chapters)
    assert ffutil.has_quicktime_chapter_track(f) is True  # make_m4b writes both by default

    strip_quicktime_chapter_track(f)
    assert has_quicktime_chapter_text_track(f) is False  # confirm the fixture is now chpl-only
    assert ffutil.get_embedded_chapters(f) == [{"start_sec": 0.0, "end_sec": 2.0, "title": "Ch1"}]  # data intact
    assert ffutil.has_quicktime_chapter_track(f) is False


def test_transcode_to_aac_m4b_single_file_produces_correct_duration(tmp_path):
    src = make_tone_mp3(tmp_path / "src.mp3", duration_sec=2.5, bitrate_kbps=96)
    out = tmp_path / "out.m4b"
    ffutil.transcode_to_aac_m4b([src], out, bitrate_kbps=96)
    assert out.exists()
    assert ffutil.get_duration_sec(out) == pytest.approx(2.5, abs=0.3)
    probed = ffutil.probe(out)
    assert probed["streams"][0]["codec_name"] == "aac"


def test_transcode_to_aac_m4b_concatenates_multiple_files(tmp_path):
    parts = [
        make_tone_mp3(tmp_path / "a.mp3", duration_sec=2.0),
        make_tone_mp3(tmp_path / "b.mp3", duration_sec=3.0),
    ]
    out = tmp_path / "out.m4b"
    ffutil.transcode_to_aac_m4b(parts, out, bitrate_kbps=96)
    assert ffutil.get_duration_sec(out) == pytest.approx(5.0, abs=0.4)


def test_transcode_reports_progress_and_supports_cancellation(tmp_path):
    src = make_tone_mp3(tmp_path / "src.mp3", duration_sec=3.0)
    out = tmp_path / "out.m4b"
    progress_calls = []

    ffutil.transcode_to_aac_m4b(
        [src], out, bitrate_kbps=96,
        total_duration_sec=3.0,
        on_progress=progress_calls.append,
        should_cancel=lambda: False,
    )
    assert out.exists()
    assert progress_calls, "on_progress should have been called at least once"
    assert all(0 <= p <= 99 for p in progress_calls)


def test_transcode_cancellation_stops_ffmpeg_and_removes_no_output(tmp_path):
    src = make_tone_mp3(tmp_path / "src.mp3", duration_sec=8.0)
    out = tmp_path / "out.m4b"

    with pytest.raises(ffutil.CancelledError):
        ffutil.transcode_to_aac_m4b(
            [src], out, bitrate_kbps=96,
            total_duration_sec=8.0,
            on_progress=lambda pct: None,
            should_cancel=lambda: True,
        )
