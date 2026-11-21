"""Metadata lookup.

audnexus (https://api.audnex.us) is an ASIN -> harmonized-metadata lookup;
it has no free-text search of its own. Candidate search is therefore done
against Audible's own unauthenticated catalog API (the same approach used
by audnexus's own downstream integrations, e.g. beets-audible and
audiobookshelf's Audible provider) to get a list of ASIN candidates with
enough detail to populate the review UI, then audnexus is used as the
canonical harmonized source (and for chapter data) once a candidate is
confirmed.

Audible's catalog is split by marketplace: the same book can have a
completely different recording (different narrator, runtime and ASIN) in
the UK store than in the US one, and a region's edition is only listed in
that region's catalog. search() therefore queries every configured region
(config.METADATA_REGIONS) in parallel and merges the results, tagging each
candidate with the region it came from. audnexus likewise only returns a
non-US edition's chapters when asked for that region, so get_chapters()
takes the candidate's region too.
"""
import html
import re
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.config import config

# Region code -> Audible catalog host suffix. The codes are the same ones
# audnexus's `region` query parameter accepts.
AUDIBLE_REGION_HOSTS = {
    "us": "com",
    "uk": "co.uk",
    "ca": "ca",
    "au": "com.au",
    "de": "de",
    "fr": "fr",
    "it": "it",
    "es": "es",
    "in": "in",
    "jp": "co.jp",
}
REGION_LABELS = {
    "us": "US", "uk": "UK", "ca": "Canada", "au": "Australia", "de": "Germany",
    "fr": "France", "it": "Italy", "es": "Spain", "in": "India", "jp": "Japan",
}
DEFAULT_REGION = "us"

AUDNEXUS_BASE_URL = "https://api.audnex.us"


def audible_search_url(region: str) -> str:
    return f"https://api.audible.{AUDIBLE_REGION_HOSTS[region]}/1.0/catalog/products"

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


def _product_to_candidate(product: dict, region: str = DEFAULT_REGION) -> dict:
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
        # (confirmed via a live query against the Audible catalog: the field
        # is `runtime_length_min`, an int). Surfaced in the review UI next
        # to the source's actual probed duration so a mismatched edition
        # (e.g. an abridged audiobook matched against an unabridged source)
        # is visible before confirming, not just discoverable after
        # conversion. None (not "") when Audible didn't report one, since
        # this is numeric rather than display text.
        "runtime_minutes": product.get("runtime_length_min"),
        # Which Audible marketplace this edition came from. Carried through
        # to the confirmed metadata so chapter lookup asks audnexus for the
        # same region's data (a UK-only ASIN is a 404 for region=us).
        "region": region,
    }


_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def _edition_key(candidate: dict) -> tuple:
    """Same recording listed in several stores (different ASINs per store,
    and titles that can differ only in curly vs straight quotes)."""
    title = candidate.get("title", "").translate(_QUOTES).casefold().strip()
    narrator = candidate.get("narrator", "").casefold().strip()
    return (title, narrator, candidate.get("runtime_minutes"))


def _search_region(region: str, params: dict) -> list[dict]:
    resp = httpx.get(audible_search_url(region), params=params, timeout=15)
    resp.raise_for_status()
    return [_product_to_candidate(p, region) for p in resp.json().get("products", [])]


def search(
    title_guess: str,
    author_guess: str = "",
    limit: int = 8,
    regions: list[str] | None = None,
) -> list[dict]:
    """Search every region in `regions` (default config.METADATA_REGIONS) and
    merge: results are interleaved by rank in region order, so each region's
    best match sits near the top, then exact duplicate editions are dropped
    (keeping the earliest region). A region that fails is skipped; only all
    of them failing is an error.
    """
    keywords = " ".join(part for part in (title_guess, author_guess) if part).strip()
    if not keywords:
        raise MetadataError("Cannot search metadata with an empty title guess.")

    regions = list(regions or config.METADATA_REGIONS)
    unknown = [r for r in regions if r not in AUDIBLE_REGION_HOSTS]
    if unknown:
        raise MetadataError(f"Unknown metadata region(s): {', '.join(unknown)}")

    params = {
        "keywords": keywords,
        "num_results": str(limit),
        "products_sort_by": "Relevance",
        "response_groups": RESPONSE_GROUPS,
    }

    per_region: list[list[dict]] = []
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(regions)) as pool:
        futures = [(r, pool.submit(_search_region, r, params)) for r in regions]
        for region, future in futures:
            try:
                per_region.append(future.result())
            except httpx.HTTPError as e:
                errors.append(f"{region}: {e}")
    if errors and not per_region:
        raise MetadataError(f"Audible search request failed: {'; '.join(errors)}")

    merged: list[dict] = []
    seen: set[tuple] = set()
    for rank in range(max((len(r) for r in per_region), default=0)):
        for results in per_region:
            if rank < len(results):
                key = _edition_key(results[rank])
                if key not in seen:
                    seen.add(key)
                    merged.append(results[rank])
    return merged


# A candidate "matches" the source when its official runtime is within the
# larger of these of the source's measured length: 3 min covers Audible's
# whole-minute rounding plus small credit/intro differences on short books,
# 1% scales for long ones (17 min on a 28h book). Different editions of the
# same title (abridged, another narrator) are usually off by far more.
RUNTIME_MATCH_MIN_TOLERANCE = 3.0
RUNTIME_MATCH_FRACTION = 0.01


def rank_by_runtime(candidates: list[dict], source_minutes: float | None) -> list[dict]:
    """Lift candidates whose runtime matches the source file's length to the
    top (closest first), leaving the rest in their search-relevance order,
    and tag each with `runtime_match`. Only ever reorders search results -
    a wrong title with a coincidentally similar length still has to have
    been returned by the keyword search to appear at all. Returns new
    dicts; the input is left untouched.
    """
    tolerance = (
        max(RUNTIME_MATCH_MIN_TOLERANCE, source_minutes * RUNTIME_MATCH_FRACTION) if source_minutes else None
    )
    tagged = []
    for c in candidates:
        runtime = c.get("runtime_minutes")
        diff = abs(runtime - source_minutes) if (tolerance is not None and runtime is not None) else None
        tagged.append(({**c, "runtime_match": diff is not None and diff <= tolerance}, diff))
    matches = sorted((t for t in tagged if t[0]["runtime_match"]), key=lambda t: t[1])
    others = [t for t in tagged if not t[0]["runtime_match"]]
    return [c for c, _ in matches + others]


def fetch_cover_bytes(cover_url: str) -> bytes | None:
    if not cover_url:
        return None
    try:
        resp = httpx.get(cover_url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        return resp.content
    except httpx.HTTPError:
        return None


def get_chapters(asin: str, region: str | None = DEFAULT_REGION) -> list[dict]:
    """Return [{start_sec, end_sec, title}, ...] from audnexus, or [] if unavailable.

    `region` must be the region the ASIN came from; jobs confirmed before
    region support existed carry none and are treated as US.
    """
    try:
        resp = httpx.get(
            f"{AUDNEXUS_BASE_URL}/books/{asin}/chapters",
            params={"region": region or DEFAULT_REGION},
            timeout=15,
        )
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
