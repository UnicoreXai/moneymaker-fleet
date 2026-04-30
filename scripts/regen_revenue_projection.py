#!/usr/bin/env python3
"""Compute 30-day forward revenue-projection bands for crypto-payout services.

For each token your fleet receives payouts in:
  1. Pull 90 days of daily USD prices from CoinGecko (free API)
  2. Compute log-return daily volatility sigma
  3. Project price 30 days forward at +/-0,1,2,3 sigma using GBM
  4. Multiply by expected token earnings/month -> USD revenue band
  5. Embed result into index.html between BEGIN_PROJ_DATA / END_PROJ_DATA

Stable USD/USDT services aren't volatile and are aggregated as a flat
baseline (BASELINE_USD).

To customize for your fleet:
  - Edit the TOKENS list below: CoinGecko id, symbol, expected tokens/month,
    a human label.
  - Update BASELINE_USD with your average flat-USD service revenue.
  - Adjust HORIZON_DAYS or sigma bands to taste.
"""
import json
import math
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INDEX = REPO / "index.html"

# ----- example token list -----
# Replace with the tokens your fleet actually earns. CoinGecko slugs at
# https://www.coingecko.com/en/api/documentation (search the coin, the
# slug is at the end of the URL). tokens_per_month is your best estimate
# of monthly token earnings for the entire fleet.
TOKENS = [
    {"id": "airtor-protocol", "symbol": "ANYONE", "tokens_per_month": 0.65, "label": "Anyone Protocol relays"},
    # Add more here as your fleet expands. Each entry creates one fan chart
    # in the dashboard.
]

# Stable-USD baseline: sum of monthly revenue from services that pay in USDC/
# USDT or fiat-pegged stables (Pawns, Repocket, TraffMonetizer, ProxyRack,
# Bitping, EarnFM, etc.). These don't have price risk — bundle as a flat add.
BASELINE_USD = 0.0  # your stable-USD monthly average

HORIZON_DAYS = 30


def fetch_prices(token_id: str, days: int = 90):
    url = (
        f"https://api.coingecko.com/api/v3/coins/{token_id}/market_chart"
        f"?vs_currency=usd&days={days}&interval=daily"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "moneymaker-fleet/1.0"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    return [p[1] for p in data["prices"]]


def daily_log_returns(prices):
    out = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0 and prices[i] > 0:
            out.append(math.log(prices[i] / prices[i - 1]))
    return out


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def project_token(token):
    try:
        prices = fetch_prices(token["id"])
    except Exception as ex:
        print(f"  {token['symbol']}: fetch FAILED ({ex})")
        return None
    if not prices:
        return None
    spot = prices[-1]
    rets = daily_log_returns(prices)
    sigma_d = stdev(rets)
    days = list(range(0, HORIZON_DAYS + 1))
    series = {}
    for k in (-3, -2, -1, 0, 1, 2, 3):
        series[k] = []
        for d in days:
            s = sigma_d * math.sqrt(d)
            p = spot * math.exp(k * s)
            series[k].append(round(p * token["tokens_per_month"], 4))
    return {
        "symbol": token["symbol"],
        "label": token["label"],
        "spot_usd": round(spot, 6),
        "tokens_per_month": token["tokens_per_month"],
        "sigma_daily": round(sigma_d, 5),
        "sigma_30d_pct": round(sigma_d * math.sqrt(30) * 100, 2),
        "days": days,
        "bands": {str(k): v for k, v in series.items()},
    }


def main():
    print("Fetching prices...")
    out = {
        "generated": int(time.time()),
        "horizon_days": HORIZON_DAYS,
        "baseline_usd": BASELINE_USD,
        "monthly_cost_usd": 0.0,  # set this to your fleet's monthly variable cost
        "tokens": [],
    }
    for t in TOKENS:
        proj = project_token(t)
        if proj:
            out["tokens"].append(proj)
            print(
                f"  {proj['symbol']}: spot=${proj['spot_usd']} "
                f"sigma_d={proj['sigma_daily']} sigma_30d={proj['sigma_30d_pct']}%"
            )
        time.sleep(1.5)  # be kind to free CoinGecko

    if out["tokens"]:
        days = out["tokens"][0]["days"]
        blended_bands = {}
        for k in (-3, -2, -1, 0, 1, 2, 3):
            blended_bands[str(k)] = []
            for i, d in enumerate(days):
                v = BASELINE_USD + sum(t["bands"][str(k)][i] for t in out["tokens"])
                blended_bands[str(k)].append(round(v, 4))
        out["blended"] = {
            "label": (
                f"Total monthly revenue (USD baseline ${BASELINE_USD:.0f} + "
                f"{len(out['tokens'])} token streams)"
            ),
            "days": days,
            "bands": blended_bands,
        }

    js = (
        "// BEGIN_PROJ_DATA\n  const REVENUE_PROJECTION = "
        + json.dumps(out, indent=2)
        + ";\n  // END_PROJ_DATA"
    )
    if not INDEX.exists():
        print(f"index.html not found at {INDEX}; printing payload instead")
        print(js)
        return
    html = INDEX.read_text(encoding="utf-8")
    if "// BEGIN_PROJ_DATA" not in html:
        raise SystemExit("BEGIN_PROJ_DATA anchor not yet in index.html — add it first")
    pre, _, rest = html.partition("// BEGIN_PROJ_DATA")
    _, _, post = rest.partition("// END_PROJ_DATA")
    INDEX.write_text(pre + js + post, encoding="utf-8")
    print(f"Embedded projection for {len(out['tokens'])} tokens")


if __name__ == "__main__":
    main()
