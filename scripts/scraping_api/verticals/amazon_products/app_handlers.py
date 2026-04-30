"""
Amazon Products vertical wrapper.

GET /v1/scrape/amazon-products/search?keyword=...&domain=...
GET /v1/scrape/amazon-products/product/{asin}?domain=...
GET /v1/scrape/amazon-products/reviews/{asin}?domain=...&page=...

All routes hit the public Amazon storefront through a US residential IP
and return raw HTML. Parsing (price extraction, ASIN lists, etc.) is
the customer's job; we hand back the page Amazon's anti-bot saw.

Mount on main app:
    from scripts.scraping_api.verticals.amazon_products import router
    app.include_router(router, prefix="/v1/scrape", tags=["amazon"])

Not mounted by default — opt in via app.include_router(router).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Header, HTTPException, Query

from scripts.scraping_api.app import ScrapeOptions, ScrapeRequest, scrape

router = APIRouter()

# Domain → host mapping. We support the regional storefronts our customers
# care about. Default is .com (US, our IP geography).
SUPPORTED_DOMAINS = {
    "com": "www.amazon.com",
    "co.uk": "www.amazon.co.uk",
    "de": "www.amazon.de",
    "ca": "www.amazon.ca",
    "com.au": "www.amazon.com.au",
    "co.jp": "www.amazon.co.jp",
    "fr": "www.amazon.fr",
    "es": "www.amazon.es",
    "it": "www.amazon.it",
    "in": "www.amazon.in",
}


def _host(domain: str) -> str:
    d = domain.lower().lstrip(".")
    if d not in SUPPORTED_DOMAINS:
        raise HTTPException(400, f"unsupported domain '{domain}', supported: {list(SUPPORTED_DOMAINS)}")
    return SUPPORTED_DOMAINS[d]


def _validate_asin(asin: str) -> str:
    a = asin.strip().upper()
    if not (len(a) == 10 and a.isalnum()):
        raise HTTPException(400, "asin must be a 10-character alphanumeric Amazon Standard Identification Number")
    return a


@router.get("/amazon-products/search")
def amazon_search(
    keyword: str = Query(..., description="Search term, e.g. 'wireless mouse'"),
    domain: str = Query("com", description="Amazon TLD: com, co.uk, de, ca, com.au, co.jp, fr, es, it, in"),
    page: int = Query(1, ge=1, le=20, description="Search results page (1..20)"),
    sort: Optional[str] = Query(
        None,
        description="One of: price-asc-rank, price-desc-rank, review-rank, date-desc-rank",
    ),
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Run an Amazon product search and return the search-results page HTML.

    NOTE: non-US domains are fetched through our US residential IPs, which
    means Amazon will sometimes redirect non-US storefronts to .com. If
    you need true geo-local egress for .de or .co.uk, this is not the
    right tier — see the long-form docs.
    """
    host = _host(domain)
    parts = [f"k={quote_plus(keyword)}", f"page={page}"]
    if sort:
        parts.append(f"s={quote_plus(sort)}")
    url = f"https://{host}/s?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/amazon-products/product/{asin}")
def amazon_product(
    asin: str,
    domain: str = Query("com"),
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch a single product detail page by ASIN."""
    a = _validate_asin(asin)
    host = _host(domain)
    url = f"https://{host}/dp/{a}"
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/amazon-products/reviews/{asin}")
def amazon_reviews(
    asin: str,
    domain: str = Query("com"),
    page: int = Query(1, ge=1, le=10),
    star_filter: Optional[str] = Query(
        None,
        description="One of: all_stars, five_star, four_star, three_star, two_star, one_star",
    ),
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch the reviews page for an ASIN."""
    a = _validate_asin(asin)
    host = _host(domain)
    parts = [f"pageNumber={page}"]
    if star_filter:
        parts.append(f"filterByStar={quote_plus(star_filter)}")
    url = f"https://{host}/product-reviews/{a}/?" + "&".join(parts)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/amazon-products/bestsellers")
def amazon_bestsellers(
    category: str = Query(..., description="Category slug, e.g. 'electronics' or 'books'"),
    domain: str = Query("com"),
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch the bestsellers list for a top-level category."""
    host = _host(domain)
    safe = category.strip().lower()
    if not safe.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "category must be alphanumeric (with dashes or underscores)")
    url = f"https://{host}/gp/bestsellers/{safe}/"
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )
