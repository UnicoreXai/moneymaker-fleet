#!/usr/bin/env python3
"""
queen_synthesis.py — pull all per-node Hermes worker reports, emit one
consolidated digest the operator can review, and (optionally) draft new
fleet-wide skills.

This is a skeleton. The "synthesis" step is intentionally left as a
stub for the OSS template — the right LLM, prompt, and convergence
heuristic depend on the operator's own setup. Drop in your model of
choice (Claude/GPT/local) at the marked spot.

What it does ship:
  - SSH-pulls JSONL reports from each node's /var/lib/mm-agent/reports/
  - Aggregates by node, by topic, by hour
  - Writes the aggregate to data/hermes_chat.jsonl on master
  - Prints a per-node summary so the operator can manually synthesize

Inputs:
  config/fleet.json           — node inventory (host, label per node)
Env:
  FLEET_SSH_PASS              — root password (or use --key for key-based auth)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[2]
FLEET_FILE = ROOT / "config" / "fleet.json"
OUT_FILE = ROOT / "data" / "hermes_chat.jsonl"


def pull_reports(node: dict) -> list[dict]:
    pw = os.environ.get("FLEET_SSH_PASS", "")
    if not pw:
        return []
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    out: list[dict] = []
    try:
        client.connect(node["host"], username="root", password=pw, timeout=30)
        stdin, stdout, stderr = client.exec_command(
            "find /var/lib/mm-agent/reports -name '*.jsonl' -mtime -1 -type f -printf '%p\\n' | head -100"
        )
        files = stdout.read().decode("utf-8", errors="replace").strip().split("\n")
        for f in files:
            if not f.strip():
                continue
            stdin, stdout, stderr = client.exec_command(f"cat '{f}'")
            txt = stdout.read().decode("utf-8", errors="replace")
            for line in txt.split("\n"):
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                    obj["_node"] = node["label"]
                    out.append(obj)
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        print(f"[{node['label']}] pull error: {e}", file=sys.stderr)
    finally:
        try:
            client.close()
        except Exception:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--write-chat",
        action="store_true",
        help="Append all pulled posts to data/hermes_chat.jsonl",
    )
    args = ap.parse_args()

    if not FLEET_FILE.exists():
        sys.exit(f"Missing {FLEET_FILE}; see config/example.fleet.json")
    fleet = json.loads(FLEET_FILE.read_text())

    all_posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(pull_reports, n): n["label"] for n in fleet["nodes"]}
        for f in as_completed(futs):
            all_posts.extend(f.result())

    print(f"\nPulled {len(all_posts)} total reports across {len(fleet['nodes'])} nodes")
    by_node = {}
    for p in all_posts:
        by_node.setdefault(p["_node"], []).append(p)
    for label, posts in sorted(by_node.items()):
        print(f"  {label}: {len(posts)} posts")
        last_summary = next(
            (p for p in reversed(posts) if p.get("action") == "cycle_end"), None
        )
        if last_summary:
            print(f"    last cycle: {last_summary.get('summary', '')[:120]}")

    if args.write_chat and all_posts:
        OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with OUT_FILE.open("a", encoding="utf-8") as f:
            for p in all_posts:
                f.write(json.dumps(p, separators=(",", ":")) + "\n")
        print(f"\nAppended {len(all_posts)} posts to {OUT_FILE}")

    # ---- INTENT: synthesis hook ---------------------------------------------
    # At this point you have all_posts in memory (typed: list[dict]). The
    # production fleet's flow is:
    #   1. Group by topic + hour
    #   2. Identify patterns (≥3 nodes report the same fix → promote skill)
    #   3. LLM-summarize with a prompt that asks "what should the queen
    #      broadcast tomorrow?" and emits proposed swarm-skill markdown.
    #   4. Operator reviews, commits, pushes to master.
    #
    # The OSS template stops here. Plug in your synthesis LLM at this point.
    # -------------------------------------------------------------------------
    return 0


if __name__ == "__main__":
    sys.exit(main())
