"""Integration tests for app/pipeline/tag.py against a real M4B file,
verifying the tag mapping table documented in the README:

  Title -> nam, Author -> ART/aART, Narrator -> wrt, Series -> alb
  (falls back to title), Series index -> trkn, Year -> day, Genre -> gen
  (defaults "Audiobook"), Description -> desc/cmt, Cover -> covr
"""
import respx
import httpx
from mutagen.mp4 import MP4

from app.pipeline.tag import apply_tags
from tests.helpers import make_m4b


def _read_tags(path):
    return MP4(path)


def test_apply_tags_writes_full_metadata(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    meta = {
        "title": "The Final Empire",
        "author": "Brandon Sanderson",
        "narrator": "Michael Kramer",
        "series": "Mistborn",
        "series_index": "1",
        "year": "2006",
        "genre": "Fantasy",
        "description": "A boy and a plan.",
        "cover_url": "",
    }
    apply_tags(f, meta)

    audio = _read_tags(f)
    assert audio["\xa9nam"] == ["The Final Empire"]
    assert audio["\xa9ART"] == ["Brandon Sanderson"]
    assert audio["aART"] == ["Brandon Sanderson"]
    assert audio["\xa9wrt"] == ["Michael Kramer"]
    assert audio["\xa9alb"] == ["Mistborn"]
    assert audio["trkn"] == [(1, 0)]
    assert audio["\xa9day"] == ["2006"]
    assert audio["\xa9gen"] == ["Fantasy"]
    assert audio["desc"] == ["A boy and a plan."]
    assert audio["\xa9cmt"] == ["A boy and a plan."]


def test_apply_tags_series_falls_back_to_title_when_no_series(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Standalone Book", "series": "", "series_index": ""})
    audio = _read_tags(f)
    assert audio["\xa9alb"] == ["Standalone Book"]


def test_apply_tags_genre_defaults_to_audiobook_when_unset(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Book"})
    audio = _read_tags(f)
    assert audio["\xa9gen"] == ["Audiobook"]


def test_apply_tags_omits_series_index_when_not_numeric(tmp_path):
    """A malformed series_index shouldn't crash tagging - just skip trkn."""
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Book", "series_index": "not-a-number"})
    audio = _read_tags(f)
    assert "trkn" not in audio


def test_apply_tags_description_over_255_chars_truncates_desc_atom_only(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    long_desc = "x" * 500
    apply_tags(f, {"title": "Book", "description": long_desc})
    audio = _read_tags(f)
    assert len(audio["desc"][0]) == 255
    assert audio["\xa9cmt"][0] == long_desc


@respx.mock
def test_apply_tags_embeds_cover_art_when_url_present(tmp_path):
    respx.get("https://example.com/cover.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff\xe0fakejpegbytes")
    )
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Book", "cover_url": "https://example.com/cover.jpg"})
    audio = _read_tags(f)
    assert bytes(audio["covr"][0]) == b"\xff\xd8\xff\xe0fakejpegbytes"


def test_apply_tags_no_cover_atom_when_url_empty(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Book", "cover_url": ""})
    audio = _read_tags(f)
    assert "covr" not in audio


def test_apply_tags_is_idempotent_and_overwrites_previous_values(tmp_path):
    """Re-running tag application (e.g. after a metadata edit) must replace
    old values, not append to them.
    """
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "First Title", "author": "Author A"})
    apply_tags(f, {"title": "Second Title", "author": "Author B"})
    audio = _read_tags(f)
    assert audio["\xa9nam"] == ["Second Title"]
    assert audio["\xa9ART"] == ["Author B"]
