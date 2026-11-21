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
        "runtime_length_min": 1479,
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
        "runtime_minutes": 1479,
        "region": "us",
    }


def test_product_to_candidate_handles_missing_optional_fields():
    candidate = _product_to_candidate({"asin": "B002", "title": "Bare Book"})
    assert candidate["author"] == ""
    assert candidate["series"] == ""
    assert candidate["series_index"] == ""
    assert candidate["runtime_minutes"] is None


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
    candidates = search("Book", "Author", regions=["us"])
    assert [c["asin"] for c in candidates] == ["B001", "B002"]
    assert all(c["region"] == "us" for c in candidates)


@respx.mock
def test_search_raises_metadata_error_on_http_failure():
    respx.get(AUDIBLE_URL).mock(return_value=httpx.Response(500))
    with pytest.raises(MetadataError):
        search("Book", "Author", regions=["us"])


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


# ---- multi-region search ----

AUDIBLE_UK_URL = "https://api.audible.co.uk/1.0/catalog/products"
AUDIBLE_AU_URL = "https://api.audible.com.au/1.0/catalog/products"


def _products(*items):
    return httpx.Response(
        200,
        json={
            "products": [
                {
                    "asin": asin,
                    "title": title,
                    "narrators": [{"name": narrator}],
                    "runtime_length_min": runtime,
                }
                for asin, title, narrator, runtime in items
            ]
        },
    )


@respx.mock
def test_search_interleaves_results_from_each_region_by_rank():
    """A region-exclusive edition (e.g. a UK-only narrator) must land near
    the top of the merged list, not after every US result.
    """
    respx.get(AUDIBLE_URL).mock(
        return_value=_products(("US1", "Book", "Elliot Hill", 1638), ("US2", "Other", "X", 10))
    )
    respx.get(AUDIBLE_UK_URL).mock(
        return_value=_products(("UK1", "Book", "Lee Maxwell-Simpson", 1720), ("UK2", "Else", "Y", 20))
    )
    candidates = search("Book", "Author", regions=["us", "uk"])
    assert [(c["asin"], c["region"]) for c in candidates] == [
        ("US1", "us"), ("UK1", "uk"), ("US2", "us"), ("UK2", "uk"),
    ]


@respx.mock
def test_search_dedupes_same_edition_across_regions_keeping_first_region():
    """The same recording listed in several stores (different ASINs, same
    title/narrator/runtime - title punctuation may differ, e.g. curly vs
    straight apostrophe) shows up once, from the first configured region.
    """
    respx.get(AUDIBLE_UK_URL).mock(
        return_value=_products(("UK1", "Fool\u2019s Assassin", "Lee Maxwell-Simpson", 1720))
    )
    respx.get(AUDIBLE_AU_URL).mock(
        return_value=_products(("AU1", "Fool's Assassin", "Lee Maxwell-Simpson", 1720))
    )
    candidates = search("Fool's Assassin", "", regions=["uk", "au"])
    assert [(c["asin"], c["region"]) for c in candidates] == [("UK1", "uk")]


@respx.mock
def test_search_keeps_working_when_one_region_fails():
    respx.get(AUDIBLE_URL).mock(return_value=httpx.Response(503))
    respx.get(AUDIBLE_UK_URL).mock(return_value=_products(("UK1", "Book", "N", 100)))
    candidates = search("Book", "", regions=["us", "uk"])
    assert [c["asin"] for c in candidates] == ["UK1"]


@respx.mock
def test_search_raises_when_every_region_fails():
    respx.get(AUDIBLE_URL).mock(return_value=httpx.Response(503))
    respx.get(AUDIBLE_UK_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(MetadataError):
        search("Book", "", regions=["us", "uk"])


def test_search_rejects_unknown_region():
    with pytest.raises(MetadataError):
        search("Book", "", regions=["zz"])


@respx.mock
def test_search_defaults_to_configured_regions(monkeypatch):
    from app.config import config

    monkeypatch.setattr(config, "METADATA_REGIONS", ["uk"])
    respx.get(AUDIBLE_UK_URL).mock(return_value=_products(("UK1", "Book", "N", 100)))
    assert [c["region"] for c in search("Book")] == ["uk"]


@respx.mock
def test_get_chapters_passes_region_to_audnexus():
    """audnexus only returns a non-US edition's chapters when asked for that
    region (verified live: UK Fool's Assassin B016P8YESO is a 404 for
    region=us, 36 chapters for region=uk).
    """
    route = respx.get(AUDNEXUS_URL_TMPL.format(asin="UK1"), params={"region": "uk"}).mock(
        return_value=httpx.Response(
            200, json={"chapters": [{"title": "One", "startOffsetMs": 0, "lengthMs": 1000}]}
        )
    )
    assert get_chapters("UK1", region="uk") == [{"start_sec": 0.0, "end_sec": 1.0, "title": "One"}]
    assert route.called


@respx.mock
def test_get_chapters_defaults_to_us_region():
    route = respx.get(AUDNEXUS_URL_TMPL.format(asin="B001"), params={"region": "us"}).mock(
        return_value=httpx.Response(200, json={"chapters": []})
    )
    get_chapters("B001")
    assert route.called


def test_parse_regions_from_config_string():
    from app.config import _regions

    assert _regions(" US, uk ,,au") == ["us", "uk", "au"]
    assert _regions("") == ["us"]


# ---- ranking by closeness to the source file's length ----

from app.pipeline.metadata import rank_by_runtime  # noqa: E402


def _c(asin, runtime):
    return {"asin": asin, "runtime_minutes": runtime}


def test_rank_by_runtime_moves_length_matches_to_top_closest_first():
    cands = [_c("US", 1638), _c("OTHER", 900), _c("UK_FAR", 1735), _c("UK", 1720)]
    ranked = rank_by_runtime(cands, 1720.9)
    assert [c["asin"] for c in ranked] == ["UK", "UK_FAR", "US", "OTHER"]
    assert [c["runtime_match"] for c in ranked] == [True, True, False, False]


def test_rank_by_runtime_tolerance_is_larger_of_3_min_and_1_percent():
    # Short book: 3 min floor wins over 1% (0.6 min).
    short = rank_by_runtime([_c("A", 63), _c("B", 64)], 60)
    assert {c["asin"]: c["runtime_match"] for c in short} == {"A": True, "B": False}
    # Long book: 1% (17.2 min) wins over 3 min.
    long = rank_by_runtime([_c("A", 1737), _c("B", 1739)], 1720)
    assert {c["asin"]: c["runtime_match"] for c in long} == {"A": True, "B": False}


def test_rank_by_runtime_keeps_relevance_order_among_non_matches():
    ranked = rank_by_runtime([_c("A", 10), _c("B", None), _c("C", 20), _c("M", 500)], 500)
    assert [c["asin"] for c in ranked] == ["M", "A", "B", "C"]
    assert ranked[2]["runtime_match"] is False


def test_rank_by_runtime_without_source_length_changes_nothing():
    cands = [_c("A", 10), _c("B", 500)]
    ranked = rank_by_runtime(cands, None)
    assert [c["asin"] for c in ranked] == ["A", "B"]
    assert all(c["runtime_match"] is False for c in ranked)


def test_rank_by_runtime_does_not_mutate_input():
    cands = [_c("A", 10)]
    rank_by_runtime(cands, 10)
    assert "runtime_match" not in cands[0]
