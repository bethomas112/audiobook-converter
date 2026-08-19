"""Unit tests for app/pipeline/detect.py - the three input-shape detector.

Requirement (planning doc, Section 2 step 4): identify which of single M4B
/ multi-file MP3 / monolithic MP3 a drop-off is, and its ordered audio
files. Also covers the "should still ask about" edge cases from Section 7:
a folder mixing MP3 and a stray non-audio file, and mixed extensions.
"""
import pytest

from app.pipeline.detect import DetectionError, detect


def test_single_m4b_file(tmp_path):
    f = tmp_path / "My Book.m4b"
    f.write_bytes(b"fake")
    result = detect(f)
    assert result.source_type == "m4b_single"
    assert result.audio_files == [f]


def test_single_monolithic_mp3_file(tmp_path):
    f = tmp_path / "My Book.mp3"
    f.write_bytes(b"fake")
    result = detect(f)
    assert result.source_type == "mp3_single"
    assert result.audio_files == [f]


def test_unsupported_file_extension_raises(tmp_path):
    f = tmp_path / "My Book.flac"
    f.write_bytes(b"fake")
    with pytest.raises(DetectionError):
        detect(f)


def test_folder_of_mp3s_is_multi(tmp_path):
    folder = tmp_path / "My Book"
    folder.mkdir()
    for i in range(1, 4):
        (folder / f"track{i}.mp3").write_bytes(b"fake")
    result = detect(folder)
    assert result.source_type == "mp3_multi"
    assert [p.name for p in result.audio_files] == ["track1.mp3", "track2.mp3", "track3.mp3"]


def test_folder_with_single_mp3_is_mp3_single_not_multi(tmp_path):
    folder = tmp_path / "My Book"
    folder.mkdir()
    (folder / "track1.mp3").write_bytes(b"fake")
    result = detect(folder)
    assert result.source_type == "mp3_single"


def test_natural_sort_orders_double_digit_tracks_correctly(tmp_path):
    """A plain lexicographic sort would put track10 before track2 - the
    detector must sort numerically within the filename instead.
    """
    folder = tmp_path / "My Book"
    folder.mkdir()
    for name in ["track2.mp3", "track10.mp3", "track1.mp3"]:
        (folder / name).write_bytes(b"fake")
    result = detect(folder)
    assert [p.name for p in result.audio_files] == ["track1.mp3", "track2.mp3", "track10.mp3"]


def test_folder_with_stray_non_audio_file_is_ignored(tmp_path):
    """Section 7 open question: a folder mixing MP3s with a stray
    non-audio file (e.g. a .nfo or .jpg) should still detect successfully,
    reporting the stray file as ignored rather than erroring out.
    """
    folder = tmp_path / "My Book"
    folder.mkdir()
    (folder / "track1.mp3").write_bytes(b"fake")
    (folder / "track2.mp3").write_bytes(b"fake")
    (folder / "cover.jpg").write_bytes(b"fake")
    (folder / "info.nfo").write_text("metadata")
    result = detect(folder)
    assert result.source_type == "mp3_multi"
    assert len(result.audio_files) == 2
    assert {p.name for p in result.ignored_files} == {"cover.jpg", "info.nfo"}


def test_folder_mixing_mp3_and_m4b_raises(tmp_path):
    """Section 7 open question: mixed MP3/M4B in one folder is ambiguous -
    the detector should refuse rather than silently guess.
    """
    folder = tmp_path / "My Book"
    folder.mkdir()
    (folder / "track1.mp3").write_bytes(b"fake")
    (folder / "whole.m4b").write_bytes(b"fake")
    with pytest.raises(DetectionError):
        detect(folder)


def test_folder_with_multiple_m4b_files_raises(tmp_path):
    folder = tmp_path / "My Book"
    folder.mkdir()
    (folder / "part1.m4b").write_bytes(b"fake")
    (folder / "part2.m4b").write_bytes(b"fake")
    with pytest.raises(DetectionError):
        detect(folder)


def test_empty_folder_raises(tmp_path):
    folder = tmp_path / "Empty"
    folder.mkdir()
    with pytest.raises(DetectionError):
        detect(folder)


def test_folder_with_only_non_audio_files_raises(tmp_path):
    folder = tmp_path / "Junk"
    folder.mkdir()
    (folder / "readme.txt").write_text("hi")
    with pytest.raises(DetectionError):
        detect(folder)


@pytest.mark.parametrize(
    "name,expected_title,expected_author",
    [
        ("Brandon Sanderson - The Final Empire", "The Final Empire", "Brandon Sanderson"),
        ("The.Final.Empire", "The Final Empire", ""),
        ("some_book_title", "some book title", ""),
    ],
)
def test_title_author_guess_from_name(tmp_path, name, expected_title, expected_author):
    f = tmp_path / f"{name}.m4b"
    f.write_bytes(b"fake")
    result = detect(f)
    assert result.title_guess == expected_title
    assert result.author_guess == expected_author


def test_title_author_guess_from_folder_name(tmp_path):
    folder = tmp_path / "Andy Weir - Project Hail Mary"
    folder.mkdir()
    (folder / "track1.mp3").write_bytes(b"fake")
    result = detect(folder)
    assert result.title_guess == "Project Hail Mary"
    assert result.author_guess == "Andy Weir"


def test_neither_file_nor_dir_raises(tmp_path):
    missing = tmp_path / "does_not_exist"
    with pytest.raises(DetectionError):
        detect(missing)
