"""
moneymaker-fleet Scraping API — vertical sub-listings.

Each vertical wraps the generic /v1/scrape endpoint with vertical-specific
URL builders + (optionally) light HTML post-processing. The wrappers all
hit the same backend, share the same auth + proxy pool, and add zero new
infrastructure dependencies. They exist to give marketplace search-rank
surface area on vertical keywords (linkedin, amazon, zillow, etc.) that
the generic listing wouldn't rank for.

Status: handlers ready, NOT mounted on the main app.py by default. Each
module exports a `router: fastapi.APIRouter` that can be included on the
main app via `app.include_router(router)`. Mount only the verticals you
want to publish.
"""
