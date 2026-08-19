"""Unit tests for app/pipeline/metadata.py - candidate search (Audible
catalog) and chapter lookup (audnexus). All HTTP is mocked via respx; no
real network access happens in this file (that's reserved for the e2e
test against the real book).
"""
import httpx
import pytest
import respx

from app.pipeline.metadata import (
    MetadataError,
    _extract_cover_url,
    _extract_year,
    _pick_series,
    _product_to_candidate,
    _strip_html,
    fetch_cover_bytes,
    get_chapters,
    search,
)

AUDIBLE_URL = "https://api.audible.com/1.0/catalog/products"
AUDNEXUS_URL_TMPL = "https://api.audnex.us/books/{asin}/chapters"


def test_strip_html_removes_tags_and_unescapes_entities():
    assert _strip_html("<p>Hello &amp; welcome</p>") == "Hello & welcome"


def test_extract_year_from_release_date():
    assert _extract_year("2006-05-30") == "2006"


def test_extract_year_handles_missing_release_date():
    assert _extract_year(None) == ""


def test_extract_cover_url_prefers_500_over_others():
    images = {"product_images": {"300": "small.jpg", "500": "medium.jpg", "1024": "large.jpg"}}
    assert _extract_cover_url(images) == "medium.jpg"


def test_extract_cover_url_falls_back_when_500_missing():
    images = {"product_images": {"1024": "large.jpg"}}
    assert _extract_cover_url(images) == "large.jpg"


def test_extract_cover_url_empty_when_no_images():
    assert _extract_cover_url({}) == ""


def test_pick_series_prefers_entry_with_sequence():
    series = [{"title": "The Cosmere", "sequence": None}, {"title": "Mistborn", "sequence": "1"}]
    assert _pick_series(series) == {"title": "Mistborn", "sequence": "1"}


def test_pick_series_falls_back_to_first_when_none_have_sequence():
    series = [{"title": "The Cosmere", "sequence": None}]
    assert _pick_series(series) == {"title": "The Cosmere", "sequence": None}


def test_pick_series_empty_list():
    assert _pick_series([]) == {}


def test_product_to_candidate_maps_all_review_fields():
    """The web UI's review step (planning doc Section 2 step 6) needs cover
    art, title, author, narrator, series, series index, year, description -
    all of it must survive the Audible product -> candidate mapping.
    """
    product = {
        "asin": "B001",
        "title": "The Final Empire",
        "authors": [{"name": "Brandon Sanderson"}],
        "narrators": [{"name": "Michael Kramer"}, {"name": "Kate Reading"}],
        "series": [{"title": "Mistborn", "sequence": "1"}],
        "release_date": "2006-07-17",
        "merchandising_summary": "<b>A boy</b> and a plan.",
        "product_images": {"500": "cover.jpg"},
    }
    candidate = _product_to_candidate(product)
    assert candidate == {
        "asin": "B001",
        "title": "The Final Empire",
        "author": "Brandon Sanderson",
        "narrator": "Michael Kramer, Kate Reading",
        "series": "Mistborn",
        "series_index": "1",
        "year": "2006",
        "description": "A boy and a plan.",
        "cover_url": "cover.jpg",
        "genre": "",
    }


def test_product_to_candidate_handles_missing_optional_fields():
    candidate = _product_to_candidate({"asin": "B002", "title": "Bare Book"})
    assert candidate["author"] == ""
    assert candidate["series"] == ""
    assert candidate["series_index"] == ""


@respx.mock
def test_search_returns_candidates_from_audible():
    respx.get(AUDIBLE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "products": [
                    {"asin": "B001", "title": "Book One", "authors": [{"name": "Author A"}]},
                    {"asin": "B002", "title": "Book Two", "authors": [{"name": "Author B"}]},
                ]
            },
        )
    )
    candidates = search("Book", "Author")
    assert [c["asin"] for c in candidates] == ["B001", "B002"]


@respx.mock
def test_search_raises_metadata_error_on_http_failure():
    respx.get(AUDIBLE_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(MetadataError):
        search("Book", "Author")


def test_search_raises_on_empty_keywords():
    with pytest.raises(MetadataError):
        search("", "")


@respx.mock
def test_get_chapters_converts_ms_to_sec():
    respx.get(AUDNEXUS_URL_TMPL.format(asin="B001")).mock(
        return_value=httpx.Response(
            200,
            json={
                "chapters": [
                    {"title": "Chapter 1", "startOffsetMs": 0, "lengthMs": 60000},
                    {"title": "Chapter 2", "startOffsetMs": 60000, "lengthMs": 90000},
                ]
            },
        )
    )
    result = get_chapters("B001")
    assert result == [
        {"start_sec": 0.0, "end_sec": 60.0, "title": "Chapter 1"},
        {"start_sec": 60.0, "end_sec": 150.0, "title": "Chapter 2"},
    ]


@respx.mock
def test_get_chapters_returns_empty_list_on_404():
    """A book with no audnexus chapter data - must fall through gracefully
    (empty list, not an exception) so chapters.py's priority-3/4 fallback
    can take over.
    """
    respx.get(AUDNEXUS_URL_TMPL.format(asin="B999")).mock(return_value=httpx.Response(404))
    assert get_chapters("B999") == []


@respx.mock
def test_get_chapters_returns_empty_list_on_network_error():
    respx.get(AUDNEXUS_URL_TMPL.format(asin="B001")).mock(side_effect=httpx.ConnectError("down"))
    assert get_chapters("B001") == []


@respx.mock
def test_fetch_cover_bytes_returns_content():
    respx.get("https://example.com/cover.jpg").mock(return_value=httpx.Response(200, content=b"jpegbytes"))
    assert fetch_cover_bytes("https://example.com/cover.jpg") == b"jpegbytes"


def test_fetch_cover_bytes_returns_none_for_empty_url():
    assert fetch_cover_bytes("") is None


@respx.mock
def test_fetch_cover_bytes_returns_none_on_http_error():
    respx.get("https://example.com/missing.jpg").mock(return_value=httpx.Response(404))
    assert fetch_cover_bytes("https://example.com/missing.jpg") is None
