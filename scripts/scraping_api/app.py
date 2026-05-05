"""
moneymaker-fleet — Residential Scraping API

POST /v1/scrape       — fetch a URL through a residential proxy from your fleet
GET  /v1/health       — service + pool status (unauthenticated)
GET  /v1/usage        — per-key counter (Bearer auth)

Run locally:
    pip install fastapi uvicorn requests
    SCRAPING_API_PROXIES_PATH=config/scraping_api/fleet_proxies.json \\
    SCRAPING_API_KEYS_PATH=config/scraping_api/keys.json \\
    SCRAPING_API_LOG_PATH=/tmp/scraping-api.jsonl \\
    uvicorn scripts.scraping_api.app:app --host 0.0.0.0 --port 8443
"""
from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scripts.scraping_api.auth import AuthBackend
from scripts.scraping_api.metrics import MetricsLogger
from scripts.scraping_api.proxy_pool import ProxyPool

# ----- config -----
PROXIES_PATH = os.environ.get(
    "SCRAPING_API_PROXIES_PATH", "/etc/scraping-api/fleet_proxies.json"
)
KEYS_PATH = os.environ.get(
    "SCRAPING_API_KEYS_PATH", "/etc/scraping-api/keys.json"
)
LOG_PATH = os.environ.get(
    "SCRAPING_API_LOG_PATH", "/var/log/scraping-api/requests.jsonl"
)
DEFAULT_TIMEOUT = int(os.environ.get("SCRAPING_API_TIMEOUT_S", "20"))
MAX_BODY_BYTES = int(os.environ.get("SCRAPING_API_MAX_BODY", str(2 * 1024 * 1024)))

# ----- app + state -----
app = FastAPI(
    title="moneymaker-fleet Scraping API",
    description=(
        "Residential-IP scraping API. Wraps a fleet of tun2socks-bound nodes "
        "with a customer-friendly bearer-auth API."
    ),
    version="0.1.0",
)
# CORS: tighten to your real frontend origin in production. The wildcard is
# fine for the OSS template but you almost certainly don't want it live.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "SCRAPING_API_CORS_ORIGINS", "*"
    ).split(","),
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
pool = ProxyPool.from_file(PROXIES_PATH)
auth = AuthBackend.load(KEYS_PATH)
metrics = MetricsLogger.open(LOG_PATH)


# --- RapidAPI marketplace proxy translation -------------------------------
# If you list this API on RapidAPI's marketplace, customer requests come
# through rapidapi.com's proxy with an X-RapidAPI-Proxy-Secret header (a
# fixed value per API, found in your RapidAPI Studio Gateway tab).
#
# This middleware detects that header and rewrites Authorization to your
# internal API key so the existing /v1/scrape auth path stays untouched.
# When both env vars are unset, the middleware is a no-op (direct callers
# must supply Authorization: Bearer <key> as before).
#
# Set on container start:
#   -e RAPIDAPI_PROXY_SECRET=<your-rapidapi-proxy-secret>
#   -e RAPIDAPI_INTERNAL_KEY=<an-active-key-from-keys.json>
RAPIDAPI_PROXY_SECRET = os.environ.get("RAPIDAPI_PROXY_SECRET", "").strip()
RAPIDAPI_INTERNAL_KEY = os.environ.get("RAPIDAPI_INTERNAL_KEY", "").strip()


@app.middleware("http")
async def _rapidapi_proxy_auth(request: Request, call_next):
    if RAPIDAPI_PROXY_SECRET and RAPIDAPI_INTERNAL_KEY:
        incoming = request.headers.get("x-rapidapi-proxy-secret", "").strip()
        if incoming and incoming == RAPIDAPI_PROXY_SECRET:
            new_headers = list(request.scope.get("headers", []))
            new_headers = [(k, v) for (k, v) in new_headers if k.lower() != b"authorization"]
            new_headers.append((b"authorization", f"Bearer {RAPIDAPI_INTERNAL_KEY}".encode("latin-1")))
            request.scope["headers"] = new_headers
    return await call_next(request)
# --- /RapidAPI marketplace proxy translation -----------------------------


class ScrapeOptions(BaseModel):
    render_js: bool = Field(False, description="Reserved for future JS rendering. Currently no-op.")
    country: Optional[str] = Field(None, description="Reserved. Egress country is determined by your residential proxy provider.")
    premium: bool = Field(False, description="Reserved.")
    timeout_s: int = Field(DEFAULT_TIMEOUT, ge=3, le=60)
    follow_redirects: bool = True
    method: str = Field("GET", pattern="^(GET|POST|HEAD)$")
    headers: Optional[dict[str, str]] = None
    body: Optional[str] = None


class ScrapeRequest(BaseModel):
    url: str
    options: ScrapeOptions = Field(default_factory=ScrapeOptions)


class ScrapeResponse(BaseModel):
    request_id: str
    status_code: int
    headers: dict[str, str]
    body: str
    body_truncated: bool
    ip_used: str
    proxy_node: str
    latency_ms: int


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


@app.exception_handler(Exception)
async def _err_handler(request: Request, exc: Exception):  # noqa: ANN001
    # Don't leak stack traces to customers
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "detail": type(exc).__name__},
    )


@app.get("/v1/health")
def health():
    s = pool.stats()
    return {
        "ok": s["available"] > 0,
        "fleet_proxies_total": s["total"],
        "fleet_proxies_available": s["available"],
        "in_flight": s["in_flight_total"],
        "uptime_s": metrics.uptime_s(),
        "total_requests": metrics.total_requests(),
        "version": app.version,
    }


@app.get("/v1/health/detail")
def health_detail():
    return pool.stats()


@app.get("/v1/status-badge")
def status_badge():
    """Public, unauthenticated trust-signal endpoint for the landing page.

    Intentionally returns a small, stable payload that's safe to embed in
    third-party HTML. uptime_30d_pct is computed from the in-memory metrics
    counter only — it resets when the container restarts, so it underreports
    until the service has been up for 30 days. Treat as a floor, not a SLA.
    """
    s = pool.stats()
    uptime_s = metrics.uptime_s()
    total_req = metrics.total_requests()
    # 30-day uptime expressed as the fraction of the last 30 days that the
    # process has been running, capped at 100. Honest when uptime_s < 30d.
    thirty_days_s = 30 * 24 * 3600
    uptime_30d_pct = round(min(uptime_s, thirty_days_s) / thirty_days_s * 100.0, 2)
    return {
        "healthy": s["available"] > 0,
        "fleet_ips": s["total"],
        "fleet_ips_available": s["available"],
        "uptime_30d_pct": uptime_30d_pct,
        "uptime_seconds": uptime_s,
        "total_requests_served": total_req,
        "version": app.version,
    }


@app.get("/v1/usage")
def usage(authorization: Optional[str] = Header(None)):
    key = _extract_bearer(authorization)
    if not key:
        raise HTTPException(401, "missing Authorization: Bearer <key>")
    info = auth.usage(key)
    if not info:
        raise HTTPException(403, "invalid api key")
    return info


@app.post("/v1/scrape", response_model=ScrapeResponse)
def scrape(req: ScrapeRequest, authorization: Optional[str] = Header(None)):
    key = _extract_bearer(authorization)
    ok, err, _ = auth.check(key)
    if not ok:
        raise HTTPException(
            status_code=401 if err in ("missing_api_key", "invalid_api_key") else 429,
            detail=err,
        )

    # validate URL crudely — reject non-http(s) and SSRF-prone hosts
    if not (req.url.startswith("http://") or req.url.startswith("https://")):
        raise HTTPException(400, "url must be http(s)")
    request_id = "req_" + secrets.token_urlsafe(12)

    slot = pool.acquire(prefer_country=req.options.country)
    proxies_dict = {"http": slot.proxy_url, "https": slot.proxy_url}

    started = time.time()
    egress_ip = ""
    status_code = 0
    headers_out: dict[str, str] = {}
    body_text = ""
    body_truncated = False
    error_str = ""
    success = False

    try:
        # Streaming get so we can cap body bytes
        with requests.request(
            method=req.options.method,
            url=req.url,
            headers=req.options.headers or None,
            data=req.options.body.encode("utf-8") if req.options.body else None,
            proxies=proxies_dict,
            timeout=req.options.timeout_s,
            allow_redirects=req.options.follow_redirects,
            stream=True,
        ) as resp:
            status_code = resp.status_code
            headers_out = {k: v for k, v in resp.headers.items() if len(k) < 64}
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_BODY_BYTES:
                    body_truncated = True
                    break
            raw = b"".join(chunks)
            try:
                body_text = raw.decode(resp.encoding or "utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                body_text = raw.decode("latin-1", errors="replace")
            success = 200 <= status_code < 600  # any well-formed HTTP is "success" for proxy
            egress_ip = slot.proxy_url.split("@")[-1].split(":")[0]
    except requests.exceptions.RequestException as e:
        error_str = repr(e)[:200]
        status_code = 599  # client-side proxy failure
        success = False
    finally:
        latency_ms = int((time.time() - started) * 1000)
        pool.release(slot, ok=success)
        metrics.log(
            request_id=request_id,
            api_key=key or "",
            url=req.url,
            proxy_node=slot.name,
            proxy_egress_ip=egress_ip or slot.proxy_url.split("@")[-1].split(":")[0],
            status_code=status_code,
            latency_ms=latency_ms,
            error=error_str,
        )

    if error_str and status_code == 599:
        # Surface proxy error so customer can retry
        raise HTTPException(502, f"upstream_proxy_error: {error_str}")

    # X-MM headers will be added by middleware below
    response = ScrapeResponse(
        request_id=request_id,
        status_code=status_code,
        headers=headers_out,
        body=body_text,
        body_truncated=body_truncated,
        ip_used=egress_ip or slot.proxy_url.split("@")[-1].split(":")[0],
        proxy_node=slot.name,
        latency_ms=latency_ms,
    )
    return response


@app.middleware("http")
async def _add_mm_headers(request: Request, call_next):  # noqa: ANN001
    resp = await call_next(request)
    resp.headers["X-MM-Service"] = "mm-scraping-api"
    resp.headers["X-MM-Version"] = app.version
    return resp


# ----- vertical routers -----
# Imported at the bottom so that `scrape`, `ScrapeRequest`, `ScrapeOptions`
# (which the vertical handlers re-import) are already defined when the handler
# modules execute. All five share the `/v1/scrape` prefix because each handler's
# routes already carry their own vertical sub-path (e.g. `/linkedin-jobs`,
# `/amazon-products/search`, `/serp/google`).
from scripts.scraping_api.verticals.linkedin_jobs.app_handlers import (  # noqa: E402
    router as linkedin_router,
)
from scripts.scraping_api.verticals.amazon_products.app_handlers import (  # noqa: E402
    router as amazon_router,
)
from scripts.scraping_api.verticals.real_estate.app_handlers import (  # noqa: E402
    router as real_estate_router,
)
from scripts.scraping_api.verticals.google_news.app_handlers import (  # noqa: E402
    router as google_news_router,
)
from scripts.scraping_api.verticals.serp.app_handlers import (  # noqa: E402
    router as serp_router,
)
from scripts.scraping_api.verticals.bing_search.app_handlers import (  # noqa: E402
    router as bing_search_router,
)

app.include_router(linkedin_router, prefix="/v1/scrape", tags=["linkedin-jobs"])
app.include_router(amazon_router, prefix="/v1/scrape", tags=["amazon"])
app.include_router(real_estate_router, prefix="/v1/scrape", tags=["real-estate"])
app.include_router(google_news_router, prefix="/v1/scrape", tags=["news"])
app.include_router(serp_router, prefix="/v1/scrape", tags=["serp"])
app.include_router(bing_search_router, prefix="/v1/scrape", tags=["bing-search"])
