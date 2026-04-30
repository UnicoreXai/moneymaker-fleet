"""
Real Estate (Zillow / Redfin) vertical wrapper.

GET /v1/scrape/real-estate/zillow/search?location=...&for_sale=true&min_price=...
GET /v1/scrape/real-estate/zillow/property/{zpid}
GET /v1/scrape/real-estate/redfin/search?location=...
GET /v1/scrape/real-estate/redfin/property/{property_url_path:path}

All routes hit the consumer-facing Zillow / Redfin pages through a US
residential IP and return raw HTML. We do NOT bypass paywalls or hit
internal-only APIs — only the same pages a logged-out user would see.

Mount on main app:
    from scripts.scraping_api.verticals.real_estate import router
    app.include_router(router, prefix="/v1/scrape", tags=["real-estate"])

Not mounted by default — opt in via app.include_router(router).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Header, HTTPException, Query

from scripts.scraping_api.app import ScrapeOptions, ScrapeRequest, scrape

router = APIRouter()


def _zillow_search_url(
    location: str,
    for_sale: bool,
    for_rent: bool,
    min_price: Optional[int],
    max_price: Optional[int],
    beds_min: Optional[int],
    page: int,
) -> str:
    # Zillow uses a complex 'searchQueryState' query param (URL-encoded JSON).
    # For the wrapper we use the simpler /homes/<location>/ path which Zillow
    # serves the same listings off. Filters are applied as query params on
    # that path.
    safe_loc = quote_plus(location.strip())
    if for_rent and not for_sale:
        path = f"/homes/for_rent/{safe_loc}_rb/"
    elif for_sale:
        path = f"/homes/for_sale/{safe_loc}_rb/"
    else:
        path = f"/homes/{safe_loc}_rb/"
    qs = []
    if min_price:
        qs.append(f"price_min={min_price}")
    if max_price:
        qs.append(f"price_max={max_price}")
    if beds_min:
        qs.append(f"beds_min={beds_min}")
    if page > 1:
        qs.append(f"p={page}")
    base = f"https://www.zillow.com{path}"
    return base + ("?" + "&".join(qs) if qs else "")


@router.get("/real-estate/zillow/search")
def zillow_search(
    location: str = Query(..., description="ZIP code, neighborhood, or 'City, ST', e.g. '90210' or 'Austin, TX'"),
    for_sale: bool = Query(True, description="Include for-sale listings"),
    for_rent: bool = Query(False, description="Include for-rent listings"),
    min_price: Optional[int] = Query(None, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    beds_min: Optional[int] = Query(None, ge=0, le=10),
    page: int = Query(1, ge=1, le=20),
    timeout_s: int = Query(25, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Search Zillow listings in a location with optional price/beds filters.

    Returns raw HTML. The listing data is embedded as JSON in a
    `<script id="__NEXT_DATA__">` block — parse that client-side
    rather than scraping individual cards.
    """
    url = _zillow_search_url(location, for_sale, for_rent, min_price, max_price, beds_min, page)
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/real-estate/zillow/property/{zpid}")
def zillow_property(
    zpid: str,
    timeout_s: int = Query(25, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch a single Zillow property detail page by ZPID."""
    if not zpid.isdigit():
        raise HTTPException(400, "zpid must be numeric")
    url = f"https://www.zillow.com/homedetails/{zpid}_zpid/"
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/real-estate/redfin/search")
def redfin_search(
    location: str = Query(..., description="City, ZIP, or neighborhood, e.g. 'Seattle' or '98103'"),
    for_sale: bool = Query(True),
    min_price: Optional[int] = Query(None, ge=0),
    max_price: Optional[int] = Query(None, ge=0),
    timeout_s: int = Query(25, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Search Redfin listings.

    Redfin's search URL grammar differs from Zillow — we route through
    `/city/...` or `/zipcode/...` heuristically based on whether the
    location is numeric.
    """
    safe = location.strip()
    if safe.replace("-", "").isdigit():
        path = f"/zipcode/{quote_plus(safe)}"
    else:
        path = f"/city/{quote_plus(safe)}"
    qs = []
    if not for_sale:
        qs.append("filter=for-rent")
    if min_price:
        qs.append(f"min-price={min_price}")
    if max_price:
        qs.append(f"max-price={max_price}")
    url = f"https://www.redfin.com{path}" + ("?" + "&".join(qs) if qs else "")
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/real-estate/redfin/property")
def redfin_property(
    url: str = Query(
        ...,
        description="Full Redfin property URL, e.g. 'https://www.redfin.com/WA/Seattle/123-Main-St-98103/home/123456'",
    ),
    timeout_s: int = Query(25, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch a single Redfin property page by full URL.

    Redfin's property URLs are not stable enough to abstract into a slug
    pattern, so we accept the full URL as a query param.
    """
    if not url.startswith("https://www.redfin.com/"):
        raise HTTPException(400, "url must be a https://www.redfin.com/ URL")
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )
