"""
Google News vertical wrapper.

GET /v1/scrape/google-news/search?q=...&hl=...&gl=...
GET /v1/scrape/google-news/topic/{topic_id}
GET /v1/scrape/google-news/rss?q=...

The /search and /topic routes return news.google.com HTML; the /rss
route returns Google News RSS XML which is much easier to parse and is
the recommended path for production usage.

Mount on main app:
    from scripts.scraping_api.verticals.google_news import router
    app.include_router(router, prefix="/v1/scrape", tags=["news"])

Not mounted by default — opt in via app.include_router(router).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Header, HTTPException, Query

from scripts.scraping_api.app import ScrapeOptions, ScrapeRequest, scrape

router = APIRouter()


@router.get("/google-news/search")
def google_news_search(
    q: str = Query(..., description="Search query, e.g. 'openai funding'"),
    hl: str = Query("en-US", description="Interface language, e.g. 'en-US', 'en-GB', 'es-ES'"),
    gl: str = Query("US", description="Country code, e.g. 'US', 'GB', 'IN'"),
    when: Optional[str] = Query(
        None,
        description="Time window: '1h', '1d', '7d', '1m', '1y'",
    ),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run a news.google.com search and return the results page HTML."""
    parts = [f"q={quote_plus(q)}"]
    if when and when in {"1h", "1d", "7d", "1m", "1y"}:
        parts[0] = f"q={quote_plus(q + ' when:' + when)}"
    parts.append(f"hl={quote_plus(hl)}")
    parts.append(f"gl={quote_plus(gl)}")
    parts.append(f"ceid={quote_plus(gl)}:en")
    url = "https://news.google.com/search?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/google-news/topic/{topic_id}")
def google_news_topic(
    topic_id: str,
    hl: str = Query("en-US"),
    gl: str = Query("US"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch a Google News topic page by topic ID.

    Topic IDs look like `CAAqIQgKIhtDQkFTRGdvSUwyMHZNRGRqTVhZU0FtVnVLQUFQAQ` —
    they're long base64-encoded blobs that appear in the URL when you
    click into a topic on news.google.com.
    """
    safe = topic_id.strip()
    if len(safe) < 10 or len(safe) > 200 or not safe.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "topic_id looks malformed")
    url = (
        f"https://news.google.com/topics/{safe}"
        f"?hl={quote_plus(hl)}&gl={quote_plus(gl)}&ceid={quote_plus(gl)}:en"
    )
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/google-news/rss")
def google_news_rss(
    q: str = Query(..., description="Search query"),
    hl: str = Query("en-US"),
    gl: str = Query("US"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch the Google News RSS feed for a query.

    RECOMMENDED for production — RSS is structured XML, much easier to
    parse than HTML, and Google rate-limits it less aggressively.
    """
    url = (
        f"https://news.google.com/rss/search?q={quote_plus(q)}"
        f"&hl={quote_plus(hl)}&gl={quote_plus(gl)}&ceid={quote_plus(gl)}:en"
    )
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )
