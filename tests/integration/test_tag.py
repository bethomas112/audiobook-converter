"""Integration tests for app/pipeline/tag.py against a real M4B file,
verifying the tag mapping table documented in the README:

  Title -> nam and alb, Author -> ART/aART, Narrator -> wrt,
  Series index -> trkn, Year -> day, Genre -> gen (defaults "Audiobook"),
  Description -> desc/cmt, Cover -> covr

Album (alb) is deliberately always the book's own title, never the series
name - confirmed by reading the tags on Brady's existing, working library
(e.g. Robin Hobb's Farseer Trilogy books each carry their own title as
Album). Media servers that group by embedded Album tag (Plex's music-style
scanner included) collapse every book sharing one Album value into a
single entry, which is exactly what happened when this used to be
`series or title`.
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
    assert audio["\xa9alb"] == ["The Final Empire"]
    assert audio["trkn"] == [(1, 0)]
    assert audio["\xa9day"] == ["2006"]
    assert audio["\xa9gen"] == ["Fantasy"]
    assert audio["desc"] == ["A boy and a plan."]
    assert audio["\xa9cmt"] == ["A boy and a plan."]


def test_apply_tags_album_is_title_when_no_series(tmp_path):
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Standalone Book", "series": "", "series_index": ""})
    audio = _read_tags(f)
    assert audio["\xa9alb"] == ["Standalone Book"]


def test_apply_tags_album_is_title_not_series_when_series_present(tmp_path):
    """Regression guard: Album must stay the book's own title even for a
    book in a series, so each book in a series is a distinct entry in a
    media server that groups by the embedded Album tag - see the module
    docstring above for why.
    """
    f = make_m4b(tmp_path / "book.m4b", duration_sec=1.0)
    apply_tags(f, {"title": "Dungeon Crawler Carl", "series": "Dungeon Crawler Carl", "series_index": "1"})
    audio = _read_tags(f)
    assert audio["\xa9alb"] == ["Dungeon Crawler Carl"]

    f2 = make_m4b(tmp_path / "book2.m4b", duration_sec=1.0)
    apply_tags(f2, {"title": "Carl's Doomsday Scenario", "series": "Dungeon Crawler Carl", "series_index": "2"})
    audio2 = _read_tags(f2)
    assert audio2["\xa9alb"] == ["Carl's Doomsday Scenario"]
    assert audio2["\xa9alb"] != audio["\xa9alb"]


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
