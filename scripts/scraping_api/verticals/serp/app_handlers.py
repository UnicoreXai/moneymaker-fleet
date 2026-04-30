"""
SERP / Google Search Results vertical wrapper.

GET /v1/scrape/serp/google?q=...&num=...
GET /v1/scrape/serp/google-images?q=...
GET /v1/scrape/serp/bing?q=...
GET /v1/scrape/serp/duckduckgo?q=...

DEPRECATED 2026-04-29: Google + DDG paths return degraded results
(Google → JS-gated interstitial; DDG → anti-bot anomaly page). The
Bing path here returns raw HTML; prefer the new purpose-built JSON
endpoint at /v1/scrape/bing-search instead. This entire /serp/*
family will be removed 2026-06-01.

Returns raw HTML for the search engine results page. Customer parses
the organic results, ads, related searches, and "people also ask"
blocks client-side — these change frequently and we don't want to be
on the hook for ongoing parser maintenance.

Mount on main app:
    from scripts.scraping_api.verticals.serp import router
    app.include_router(router, prefix="/v1/scrape", tags=["serp"])

Not mounted by default — opt in via app.include_router(router).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Header, Query, Response

from scripts.scraping_api.app import ScrapeOptions, ScrapeRequest, scrape

router = APIRouter()

_DEPRECATION_HEADERS = {
    "X-Deprecated": "true",
    "X-Replacement": "/v1/scrape/bing-search",
}


def _mark_deprecated(response: Response) -> None:
    """Attach deprecation headers to the outgoing response."""
    for k, v in _DEPRECATION_HEADERS.items():
        response.headers[k] = v


@router.get("/serp/google")
def serp_google(
    response: Response,
    q: str = Query(..., description="Search query"),
    num: int = Query(10, ge=10, le=100, description="Results per page (10..100)"),
    start: int = Query(0, ge=0, le=900, description="Pagination offset (0, 10, 20, ...)"),
    hl: str = Query("en", description="Interface language, e.g. 'en', 'es'"),
    gl: str = Query("us", description="Country code, e.g. 'us', 'gb', 'in'"),
    safe: Optional[str] = Query(None, description="Safe search: 'active' or 'off'"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run a google.com web search and return the SERP HTML.

    DEPRECATED 2026-04-29: Google + DDG paths return degraded results.
    Use /v1/scrape/bing-search instead. This endpoint will be removed
    2026-06-01. Per smoke test (commit 85f76ae), Google returns the
    JS-required interstitial (no organic results in HTML); residential
    IP doesn't help because the gate is browser-side.

    NOTE: Google heavily rate-limits scraped traffic; if you 429 on a
    burst, back off to one request per 5 seconds and the residential IP
    rotation will let you sustain ~10–20 queries/min steady-state.
    """
    _mark_deprecated(response)
    parts = [
        f"q={quote_plus(q)}",
        f"num={num}",
        f"hl={quote_plus(hl)}",
        f"gl={quote_plus(gl)}",
    ]
    if start:
        parts.append(f"start={start}")
    if safe in {"active", "off"}:
        parts.append(f"safe={safe}")
    url = "https://www.google.com/search?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/serp/google-images")
def serp_google_images(
    response: Response,
    q: str = Query(...),
    hl: str = Query("en"),
    gl: str = Query("us"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run a Google Images search.

    DEPRECATED 2026-04-29: Google + DDG paths return degraded results.
    Use /v1/scrape/bing-search instead. This endpoint will be removed
    2026-06-01.
    """
    _mark_deprecated(response)
    parts = [
        f"q={quote_plus(q)}",
        "tbm=isch",
        f"hl={quote_plus(hl)}",
        f"gl={quote_plus(gl)}",
    ]
    url = "https://www.google.com/search?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/serp/bing")
def serp_bing(
    response: Response,
    q: str = Query(...),
    count: int = Query(10, ge=10, le=50),
    first: int = Query(1, ge=1, le=901, description="1-indexed offset (1, 11, 21, ...)"),
    cc: str = Query("US", description="Country code, e.g. 'US', 'GB'"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run a bing.com web search and return the SERP HTML.

    DEPRECATED 2026-04-29: Google + DDG paths return degraded results.
    Use /v1/scrape/bing-search instead — it returns parsed JSON
    organic results rather than raw HTML. This endpoint will be
    removed 2026-06-01.
    """
    _mark_deprecated(response)
    parts = [
        f"q={quote_plus(q)}",
        f"count={count}",
        f"first={first}",
        f"cc={quote_plus(cc)}",
    ]
    url = "https://www.bing.com/search?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/serp/duckduckgo")
def serp_duckduckgo(
    response: Response,
    q: str = Query(...),
    kl: str = Query("us-en", description="Region+language, e.g. 'us-en', 'uk-en', 'de-de'"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run a duckduckgo.com search.

    DEPRECATED 2026-04-29: Google + DDG paths return degraded results.
    Use /v1/scrape/bing-search instead. This endpoint will be removed
    2026-06-01. Per smoke test (commit 85f76ae), DDG serves an
    anti-bot anomaly-check page from residential IPs.

    DuckDuckGo is more scraping-tolerant than Google but its HTML is
    paginated via POST requests — for paging beyond the first page,
    fetch /v1/scrape/serp/duckduckgo with the next URL cursor manually.
    """
    _mark_deprecated(response)
    parts = [f"q={quote_plus(q)}", f"kl={quote_plus(kl)}"]
    url = "https://html.duckduckgo.com/html/?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )
