"""
LinkedIn Jobs vertical wrapper.

Wraps GET /v1/scrape/linkedin-jobs?keywords=...&location=...&...
into a POST against the generic ScrapeRequest. All fetching goes through
the same residential proxy pool. We do NOT parse the HTML server-side —
parsing is the customer's job (and changes when LinkedIn re-skins). We
do build the canonical search URL with all the documented filters so
customers don't have to remember LinkedIn's URL grammar.

Mount on main app:
    from scripts.scraping_api.verticals.linkedin_jobs import router
    app.include_router(router, prefix="/v1/scrape", tags=["linkedin-jobs"])

Not mounted by default — opt in via app.include_router(router).
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Header, HTTPException, Query

from scripts.scraping_api.app import ScrapeOptions, ScrapeRequest, scrape

router = APIRouter()

# LinkedIn job-search filter mappings. These are the URL params LinkedIn
# uses on its public /jobs/search/ page. Stable since ~2022.
EXPERIENCE_LEVELS = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid-senior": "4",
    "director": "5",
    "executive": "6",
}
JOB_TYPES = {
    "full-time": "F",
    "part-time": "P",
    "contract": "C",
    "temporary": "T",
    "volunteer": "V",
    "internship": "I",
}
REMOTE_FILTER = {
    "on-site": "1",
    "remote": "2",
    "hybrid": "3",
}
TIME_POSTED = {
    "past-day": "r86400",
    "past-week": "r604800",
    "past-month": "r2592000",
}


def _build_linkedin_jobs_url(
    keywords: str,
    location: Optional[str],
    experience: Optional[str],
    job_type: Optional[str],
    remote: Optional[str],
    posted_since: Optional[str],
    start: int,
) -> str:
    parts = [f"keywords={quote_plus(keywords)}"]
    if location:
        parts.append(f"location={quote_plus(location)}")
    if experience and experience.lower() in EXPERIENCE_LEVELS:
        parts.append(f"f_E={EXPERIENCE_LEVELS[experience.lower()]}")
    if job_type and job_type.lower() in JOB_TYPES:
        parts.append(f"f_JT={JOB_TYPES[job_type.lower()]}")
    if remote and remote.lower() in REMOTE_FILTER:
        parts.append(f"f_WT={REMOTE_FILTER[remote.lower()]}")
    if posted_since and posted_since.lower() in TIME_POSTED:
        parts.append(f"f_TPR={TIME_POSTED[posted_since.lower()]}")
    if start > 0:
        parts.append(f"start={start}")
    return "https://www.linkedin.com/jobs/search/?" + "&".join(parts)


@router.get("/linkedin-jobs")
def linkedin_jobs_search(
    keywords: str = Query(..., description="Search terms, e.g. 'senior python engineer'"),
    location: Optional[str] = Query(None, description="City or country, e.g. 'San Francisco' or 'United States'"),
    experience: Optional[str] = Query(
        None,
        description="One of: internship, entry, associate, mid-senior, director, executive",
    ),
    job_type: Optional[str] = Query(
        None,
        description="One of: full-time, part-time, contract, temporary, volunteer, internship",
    ),
    remote: Optional[str] = Query(None, description="One of: on-site, remote, hybrid"),
    posted_since: Optional[str] = Query(
        None, description="One of: past-day, past-week, past-month"
    ),
    start: int = Query(0, ge=0, le=975, description="Pagination offset (0, 25, 50, ...)"),
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Search LinkedIn public job listings through a US residential IP.

    Returns the raw HTML from LinkedIn's job-search results page. Parse
    the embedded JSON-LD blocks or the listing cards client-side.
    """
    url = _build_linkedin_jobs_url(
        keywords, location, experience, job_type, remote, posted_since, start
    )
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/linkedin-jobs/posting/{job_id}")
def linkedin_jobs_posting(
    job_id: str,
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch a single LinkedIn job posting page by its numeric job ID.

    Use the public guest-view URL so we don't need an authenticated
    session. Returns full HTML; parse the description/recruiter info
    from the JSON-LD or DOM client-side.
    """
    if not job_id.isdigit():
        raise HTTPException(400, "job_id must be numeric")
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )


@router.get("/linkedin-jobs/company/{company_slug}")
def linkedin_jobs_company(
    company_slug: str,
    timeout_s: int = Query(20, ge=3, le=60),
    authorization: Optional[str] = Header(None),
):
    """Fetch the public 'Jobs at <company>' page by URL slug.

    Slug is the path segment after `/company/` on linkedin.com — e.g.
    `microsoft`, `anthropic`, `openai`. Returns the company's public
    jobs landing page HTML.
    """
    safe = company_slug.strip().lower()
    if not safe.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(400, "company_slug must be alphanumeric, dashes/underscores only")
    url = f"https://www.linkedin.com/company/{safe}/jobs/"
    return scrape(
        ScrapeRequest(url=url, options=ScrapeOptions(timeout_s=timeout_s)),
        authorization=authorization,
    )
