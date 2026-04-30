"""
Bing Web Search vertical wrapper.

GET /v1/scrape/bing-search?q=...&count=...&offset=...

Purpose-built Bing wrapper. Fetches a bing.com SERP through your
residential proxy pool, parses the organic `<li class="b_algo">`
result blocks, and returns a clean JSON shape:

    {
      "query": "...",
      "results": [{"title", "url", "snippet", "rank"}, ...],
      "engine": "bing",
      "ip_used": "192.0.2.1"
    }

Why Bing-only? Per smoke test (see docs/SERVICE_RESEARCH.md):
  - Bing → real organic results, residential IP egress
  - Google → JS-gated, returns no-JS interstitial (delisted)
  - DuckDuckGo → anti-bot challenge page (delisted)

Listing only what works avoids paid-listing bad-review/refund risk.

Mount on main app:
    from scripts.scraping_api.verticals.bing_search import router
    app.include_router(router, prefix="/v1/scrape", tags=["bing-search"])
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Header, Query

from scripts.scraping_api.app import ScrapeOptions, ScrapeRequest, scrape

router = APIRouter()


class _BingResultParser(HTMLParser):
    """Stream-parses Bing SERP HTML and collects organic results.

    Bing's organic-result DOM (as of 2026-04):
        <li class="b_algo">
          <h2><a href="https://example.com/path">Title text</a></h2>
          <div class="b_caption"><p>Snippet text...</p></div>
        </li>

    The exact div/p classes inside b_caption have churned over the years;
    we collect ALL text inside the b_algo block and treat the post-title
    text as the snippet, which is robust to Bing's class renames.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        # State flags for the b_algo block we're currently inside.
        self._in_algo = 0  # nested-li depth
        self._in_h2 = 0
        self._in_anchor = 0
        self._cur_url: Optional[str] = None
        self._cur_title_chunks: list[str] = []
        self._cur_body_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attr_dict = {k: (v or "") for k, v in attrs}
        cls = attr_dict.get("class", "")
        if tag == "li" and "b_algo" in cls.split():
            self._in_algo += 1
            self._cur_url = None
            self._cur_title_chunks = []
            self._cur_body_chunks = []
            return
        if not self._in_algo:
            return
        if tag == "h2":
            self._in_h2 += 1
        elif tag == "a" and self._in_h2 and self._cur_url is None:
            href = attr_dict.get("href", "")
            if href.startswith("http://") or href.startswith("https://"):
                self._cur_url = href
            self._in_anchor += 1
        elif tag == "a" and self._in_h2:
            self._in_anchor += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._in_algo:
            return
        if tag == "a" and self._in_anchor:
            self._in_anchor -= 1
        elif tag == "h2" and self._in_h2:
            self._in_h2 -= 1
        elif tag == "li" and self._in_algo:
            self._in_algo -= 1
            if self._in_algo == 0:
                self._flush_current()

    def handle_data(self, data: str) -> None:
        if not self._in_algo:
            return
        if self._in_h2:
            self._cur_title_chunks.append(data)
        else:
            self._cur_body_chunks.append(data)

    def _flush_current(self) -> None:
        if not self._cur_url:
            return
        title = re.sub(r"\s+", " ", "".join(self._cur_title_chunks)).strip()
        snippet = re.sub(r"\s+", " ", "".join(self._cur_body_chunks)).strip()
        if not title:
            return
        self.results.append(
            {
                "title": title,
                "url": self._cur_url,
                "snippet": snippet,
                # rank assigned in the caller after collection
            }
        )


def _parse_bing_organic(html: str) -> list[dict]:
    """Parse Bing SERP HTML and return a list of organic result dicts.

    Returns an empty list if parsing fails or no b_algo blocks are present
    (e.g. Bing served a CAPTCHA or empty results page).
    """
    if not html:
        return []
    parser = _BingResultParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001
        # HTMLParser is fairly tolerant but malformed input can still raise.
        # Return whatever was collected before the exception.
        return parser.results
    return parser.results


@router.get("/bing-search")
def bing_search(
    q: str = Query(..., description="Search query, e.g. 'best running shoes 2026'"),
    count: int = Query(10, ge=1, le=50, description="Results per page (1..50)"),
    offset: int = Query(
        0,
        ge=0,
        le=900,
        description="0-indexed offset (0, 10, 20, ...). Maps to Bing's 1-indexed `first`.",
    ),
    cc: str = Query("US", description="Country code, e.g. 'US', 'GB', 'DE'"),
    timeout_s: int = Query(15, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run a bing.com web search and return parsed organic results as JSON.

    Returns a clean `{query, results, engine, ip_used}` shape — no raw HTML.
    Each result has `{title, url, snippet, rank}` where rank is 1-indexed
    within the returned page (not the global SERP rank).
    """
    first = offset + 1  # Bing's `first` is 1-indexed
    parts = [
        f"q={quote_plus(q)}",
        f"count={count}",
        f"first={first}",
        f"cc={quote_plus(cc)}",
    ]
    url = "https://www.bing.com/search?" + "&".join(parts)
    backend = scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )

    results = _parse_bing_organic(backend.body)
    # Trim to requested count + assign 1-indexed rank
    trimmed: list[dict] = []
    for i, r in enumerate(results[:count], start=1):
        trimmed.append(
            {
                "title": r["title"],
                "url": r["url"],
                "snippet": r["snippet"],
                "rank": i,
            }
        )
    return {
        "query": q,
        "results": trimmed,
        "engine": "bing",
        "ip_used": backend.ip_used,
    }
