#!/usr/bin/env python3
"""
mint_key.py — generate or seed an API key into keys.json.

Usage:
    # First-time bootstrap (also creates the file):
    python scripts/scraping_api/mint_key.py \\
        --keys-path config/scraping_api/keys.json \\
        --label internal-test --monthly-cap 10000 --rate-per-minute 30

    # Add a customer key:
    python scripts/scraping_api/mint_key.py \\
        --keys-path /etc/scraping-api/keys.json \\
        --label hobby --monthly-cap 50000 --rate-per-minute 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from scripts.scraping_api.auth import mint_key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys-path", type=Path, required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--monthly-cap", type=int, required=True)
    ap.add_argument("--rate-per-minute", type=int, required=True)
    args = ap.parse_args()

    args.keys_path.parent.mkdir(parents=True, exist_ok=True)
    if args.keys_path.exists():
        data = json.loads(args.keys_path.read_text())
    else:
        data = {
            "_comment": "GITIGNORED. MM Scraping API customer keys. Add via mint_key.py.",
            "keys": {},
        }

    key, meta = mint_key(args.label, args.monthly_cap, args.rate_per_minute)
    data["keys"][key] = meta

    args.keys_path.write_text(json.dumps(data, indent=2))
    try:
        os.chmod(args.keys_path, 0o600)
    except Exception:  # noqa: BLE001
        pass
    print(f"Minted: {key}")
    print(f"  label:           {args.label}")
    print(f"  monthly_cap:     {args.monthly_cap}")
    print(f"  rate_per_minute: {args.rate_per_minute}")
    print(f"  keys file:       {args.keys_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
