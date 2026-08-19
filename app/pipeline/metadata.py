"""Metadata lookup.

audnexus (https://api.audnex.us) is an ASIN -> harmonized-metadata lookup;
it has no free-text search of its own. Candidate search is therefore done
against Audible's own unauthenticated catalog API (the same approach used
by audnexus's own downstream integrations, e.g. beets-audible and
audiobookshelf's Audible provider) to get a list of ASIN candidates with
enough detail to populate the review UI, then audnexus is used as the
canonical harmonized source (and for chapter data) once a candidate is
confirmed.
"""
import html
import re

import httpx

AUDIBLE_SEARCH_URL = "https://api.audible.com/1.0/catalog/products"
AUDNEXUS_BASE_URL = "https://api.audnex.us"

RESPONSE_GROUPS = "contributors,product_desc,media,series"

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


class MetadataError(RuntimeError):
    pass


def _extract_year(release_date: str | None) -> str:
    if not release_date:
        return ""
    return release_date[:4]


def _extract_cover_url(product: dict) -> str:
    images = product.get("product_images") or {}
    for size in ("500", "1024", "300"):
        if images.get(size):
            return images[size]
    return ""


def _pick_series(series_list: list[dict]) -> dict:
    """Prefer the most specific series entry (the one with a sequence
    number) over a broader umbrella series (e.g. "The Cosmere") that lists
    the book without a position in it.
    """
    if not series_list:
        return {}
    for entry in series_list:
        if entry.get("sequence"):
            return entry
    return series_list[0]


def _product_to_candidate(product: dict) -> dict:
    series = _pick_series(product.get("series") or [])
    description = product.get("merchandising_summary") or product.get("publisher_summary") or ""
    return {
        "asin": product.get("asin", ""),
        "title": product.get("title", ""),
        "author": ", ".join(a.get("name", "") for a in product.get("authors", []) or []),
        "narrator": ", ".join(n.get("name", "") for n in product.get("narrators", []) or []),
        "series": series.get("title", ""),
        "series_index": str(series.get("sequence", "") or ""),
        "year": _extract_year(product.get("release_date")),
        "description": _strip_html(description),
        "cover_url": _extract_cover_url(product),
        "genre": "",
        # Official runtime in minutes, straight from Audible's catalog
        # (confirmed via a live query against AUDIBLE_SEARCH_URL: the field
        # is `runtime_length_min`, an int). Surfaced in the review UI next
        # to the source's actual probed duration so a mismatched edition
        # (e.g. an abridged audiobook matched against an unabridged source)
        # is visible before confirming, not just discoverable after
        # conversion. None (not "") when Audible didn't report one, since
        # this is numeric rather than display text.
        "runtime_minutes": product.get("runtime_length_min"),
    }


def search(title_guess: str, author_guess: str = "", limit: int = 8) -> list[dict]:
    keywords = " ".join(part for part in (title_guess, author_guess) if part).strip()
    if not keywords:
        raise MetadataError("Cannot search metadata with an empty title guess.")

    params = {
        "keywords": keywords,
        "num_results": str(limit),
        "products_sort_by": "Relevance",
        "response_groups": RESPONSE_GROUPS,
    }
    try:
        resp = httpx.get(AUDIBLE_SEARCH_URL, params=params, timeout=15)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise MetadataError(f"Audible search request failed: {e}") from e

    products = resp.json().get("products", [])
    return [_product_to_candidate(p) for p in products]


def fetch_cover_bytes(cover_url: str) -> bytes | None:
    if not cover_url:
        return None
    try:
        resp = httpx.get(cover_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError:
        return None


def get_chapters(asin: str) -> list[dict]:
    """Return [{start_sec, end_sec, title}, ...] from audnexus, or [] if unavailable."""
    try:
        resp = httpx.get(f"{AUDNEXUS_BASE_URL}/books/{asin}/chapters", timeout=15)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
    except httpx.HTTPError:
        return []

    data = resp.json()
    chapters = []
    offset_ms = 0
    for ch in data.get("chapters", []):
        start_ms = ch.get("startOffsetMs", offset_ms)
        length_ms = ch.get("lengthMs", 0)
        chapters.append(
            {
                "start_sec": start_ms / 1000,
                "end_sec": (start_ms + length_ms) / 1000,
                "title": ch.get("title", ""),
            }
        )
        offset_ms = start_ms + length_ms
    return chapters
