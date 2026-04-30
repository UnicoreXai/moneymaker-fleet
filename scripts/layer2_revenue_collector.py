#!/usr/bin/env python3
"""
Layer 2 revenue collector — pending balances on services that DON'T pay
on-chain (or haven't paid yet because they're below threshold).

Architecture
------------
The Layer 1 collector (`realized_revenue_collector.py`) is the source of
truth: it sums actual incoming token transfers to the payout wallet.
Layer 2 fills in the gap until those transfers happen — tracking the
$ of pending balance accumulating on each service's dashboard.

Strategy
--------
For each service, this script tries (in order):

  1. **Public balance API** — services that expose a per-account REST
     endpoint with a bearer token. Cheap, fast, headless-friendly.
  2. **SSH-into-the-node + parse logs / container env** — for services
     whose only exposed signal is in the daemon's stdout.
  3. **Skip** — if neither works, fall back to manual or to Chrome MCP
     in your daily run.

The output is a snapshot JSON, POSTed to your dashboard backend at
`/api/layer2`. Latest snapshot wins for the summary endpoint; full
history is kept in append-only JSONL on the server side.

Customize this for your stack
-----------------------------
This file is a skeleton. You'll add per-service collectors as you
onboard them — see `_collect_pawns()` below for the API-bearer
pattern and `_collect_traffmonetizer_via_ssh()` for the log-parse
pattern.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

DASHBOARD_BASE = os.environ.get("DASHBOARD_BASE") or "https://dashboard.example.com"
FORUM_SECRET = os.environ.get("FORUM_SECRET") or ""

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("layer2-collector")


# ---------------------------------------------------------------------------
# Per-service collectors
# ---------------------------------------------------------------------------
def _collect_pawns() -> dict:
    """Pawns has no public balance API as of 2026-04. Returns empty until
    you scrape `dashboard.pawns.app` via Chrome MCP and write the result
    to tmp/pawns_balance.json."""
    cache = REPO / "tmp" / "pawns_balance.json"
    if not cache.exists():
        return {"service": "pawns", "balance_usd": None, "error": "no_cache"}
    data = json.loads(cache.read_text())
    return {
        "service": "pawns",
        "balance_usd": data.get("balance_usd"),
        "fetched_at": data.get("fetched_at"),
    }


def _collect_traffmonetizer_via_ssh() -> dict:
    """Skeleton: TraffMonetizer's CLI logs the per-day MB shared. SSH
    into each node, sum the day's traffic, multiply by published rate.
    Left empty for the OSS template — implement against your fleet."""
    return {"service": "traffmonetizer", "balance_usd": None, "error": "not_implemented"}


COLLECTORS = [
    _collect_pawns,
    _collect_traffmonetizer_via_ssh,
    # Add your own here
]


# ---------------------------------------------------------------------------
# POST helper
# ---------------------------------------------------------------------------
def post_snapshot(snapshot: dict) -> bool:
    if not FORUM_SECRET:
        log.warning("FORUM_SECRET not set; skipping POST, printing instead")
        print(json.dumps(snapshot, indent=2))
        return False
    url = f"{DASHBOARD_BASE.rstrip('/')}/api/layer2"
    body = json.dumps(snapshot).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Forum-Secret": FORUM_SECRET,
            "User-Agent": "moneymaker-fleet-layer2/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = r.status == 200
            if ok:
                log.info("layer2 snapshot POSTed (%d entries)", len(snapshot["entries"]))
            return ok
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        log.error("POST failed: %s", e)
        return False


def main() -> int:
    entries = []
    for fn in COLLECTORS:
        try:
            entries.append(fn())
        except Exception as e:
            log.error("collector %s failed: %s", fn.__name__, e)
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
        "total_pending_usd": sum(
            e["balance_usd"] for e in entries if isinstance(e.get("balance_usd"), (int, float))
        ),
    }
    post_snapshot(snapshot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
