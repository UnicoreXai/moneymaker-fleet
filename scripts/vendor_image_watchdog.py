"""
Vendor Docker Image Watchdog
============================
Polls Docker Hub for new digests on vendor images used across the fleet.
Emits alerts when a new image is published so you know when:
  - A currently-broken vendor has shipped a fix
  - A fleet-wide update is rolling out via watchtower
  - A long-stale image suddenly moves again

State file: tmp/vendor_image_digests.json
  {
    "iproyal/pawns-cli:latest": {
      "digest": "sha256:...",
      "last_updated": "2026-01-15T12:34:56Z",
      "last_checked": "2026-04-18T22:10:00Z"
    },
    ...
  }

First run just records baseline — no alerts.
Subsequent runs compare digest; if changed, emit NEW_IMAGE alert.

Usage:
  python3.11 scripts/vendor_image_watchdog.py           # human-readable output
  python3.11 scripts/vendor_image_watchdog.py --json    # JSON to stdout
  python3.11 scripts/vendor_image_watchdog.py --init    # seed state file, suppress alerts
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).parent.parent
STATE_FILE = REPO / "tmp" / "vendor_image_digests.json"

# ── Images to watch ──────────────────────────────────────────────────────────
# (image, tag, priority, note)
# priority: "critical" = currently broken, alert loudly when new digest appears
#           "standard" = normal monitoring
WATCHED_IMAGES = [
    # Mark as "critical" anything that's currently broken on your fleet — the
    # script will alert loudly when a new digest is published, so you know to
    # bump and redeploy.
    ("iproyal/pawns-cli",           "latest", "standard", "Pawns / IPRoyal Pawns"),
    ("repocket/repocket",           "latest", "standard", "Repocket"),
    ("mysteriumnetwork/myst",       "latest", "standard", "Mysterium node"),
    ("traffmonetizer/cli_v2",       "latest", "standard", "Traffmonetizer"),
    ("earnfm/earnfm-client",        "latest", "standard", "EarnFM"),
    ("proxyrack/pop",               "latest", "standard", "ProxyRack"),
    ("bringyour/community-provider","latest", "standard", "URnetwork"),
    ("techroy23/docker-wipter",     "latest", "standard", "Wipter"),

    # Infrastructure
    ("xjasonlyu/tun2socks",         "latest", "standard", "tun2socks (residential routing)"),
]

DOCKERHUB_API = "https://hub.docker.com/v2/repositories/{repo}/tags/{tag}/"


def _dockerhub_url(repo: str, tag: str) -> str:
    """Build Docker Hub API URL. Handles official images (no namespace)."""
    if "/" not in repo:
        repo = f"library/{repo}"
    return DOCKERHUB_API.format(repo=repo, tag=tag)


def fetch_digest(image: str, tag: str, timeout: int = 15) -> dict | None:
    """
    Fetch current digest + last_updated for an image tag.

    Returns:
        {"digest": "sha256:...", "last_updated": "ISO8601", "full_size": int}
        or None on failure.
    """
    url = _dockerhub_url(image, tag)
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e)}

    # Docker Hub v2 returns "digest" at the top level for a single-platform tag,
    # or "images" array for multi-arch. Prefer linux/amd64.
    digest = data.get("digest")
    if not digest and "images" in data:
        for img in data["images"]:
            if img.get("architecture") == "amd64" and img.get("os") == "linux":
                digest = img.get("digest")
                break
        if not digest and data["images"]:
            digest = data["images"][0].get("digest")

    return {
        "digest":        digest,
        "last_updated":  data.get("last_updated"),
        "full_size":     data.get("full_size"),
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def check_images(init_only: bool = False) -> dict:
    """
    Poll all watched images, compare to state, emit alerts on change.

    Returns:
        {
          "timestamp": "ISO8601",
          "images":   [{image, tag, priority, digest, last_updated, changed, note}],
          "alerts":   [...],
          "status":   "OK" | "ALERT" | "ERROR",
          "init":     bool,
        }
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    state = load_state()
    result = {
        "timestamp": now_iso,
        "images":    [],
        "alerts":    [],
        "status":    "OK",
        "init":      init_only,
    }

    for image, tag, priority, note in WATCHED_IMAGES:
        key = f"{image}:{tag}"
        fetched = fetch_digest(image, tag)

        entry = {
            "image":        image,
            "tag":          tag,
            "priority":     priority,
            "note":         note,
            "digest":       None,
            "last_updated": None,
            "changed":      False,
            "error":        None,
        }

        if fetched is None or "error" in (fetched or {}):
            entry["error"] = (fetched or {}).get("error", "fetch_failed")
            result["alerts"].append(
                f"FETCH_ERROR: {key} — {entry['error']}"
            )
            result["images"].append(entry)
            continue

        entry["digest"]       = fetched["digest"]
        entry["last_updated"] = fetched["last_updated"]

        prior = state.get(key, {})
        prior_digest = prior.get("digest")

        if prior_digest and prior_digest != entry["digest"] and not init_only:
            entry["changed"] = True
            severity = "CRITICAL" if priority == "critical" else "INFO"
            result["alerts"].append(
                f"{severity}_NEW_IMAGE: {key} — digest changed "
                f"(was {prior_digest[:19]}..., now {entry['digest'][:19]}..., "
                f"published {entry['last_updated']}). {note}"
            )

        # Update state for next run
        state[key] = {
            "digest":       entry["digest"],
            "last_updated": entry["last_updated"],
            "last_checked": now_iso,
            "priority":     priority,
            "note":         note,
        }

        result["images"].append(entry)

    save_state(state)

    if result["alerts"]:
        result["status"] = "ALERT"

    return result


def write_to_active_work(alerts: list[str], repo: Path):
    """Append watchdog alerts to ACTIVE_WORK.md (idempotent via marker)."""
    if not alerts:
        return

    aw_path = repo / "docs" / "ACTIVE_WORK.md"
    if not aw_path.exists():
        return

    content = aw_path.read_text()
    alert_block = "\n".join(f"  - {a}" for a in alerts)
    marker = "<!-- vendor-image-alerts -->"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    new_section = (
        f"\n\n### Vendor Image Alerts ({today})\n"
        f"{alert_block}\n"
        f"{marker}"
    )

    if marker in content:
        import re
        content = re.sub(
            r"\n\n### Vendor Image Alerts.*?" + re.escape(marker),
            new_section,
            content,
            flags=re.DOTALL,
        )
    else:
        content += new_section

    aw_path.write_text(content)


def main(json_output: bool = False, init_only: bool = False) -> dict:
    result = check_images(init_only=init_only)

    if json_output:
        print(json.dumps(result, indent=2))
        return result

    # ── Human-readable output ─────────────────────────────────────────────────
    print(f"\n{'─'*72}")
    print(f"  Vendor Image Watchdog — {result['timestamp'][:19]}Z"
          + ("  [INIT MODE]" if init_only else ""))
    print(f"{'─'*72}")

    print(f"\n  {'Image':<34} {'Priority':<10} {'Last updated':<22} {'Status'}")
    print(f"  {'─'*34} {'─'*10} {'─'*22} {'─'*15}")
    for img in result["images"]:
        key = f"{img['image']}:{img['tag']}"
        lu  = (img["last_updated"] or "?")[:19]
        if img["error"]:
            status_str = f"ERR: {img['error'][:30]}"
        elif img["changed"]:
            status_str = "NEW IMAGE ⚠"
        else:
            status_str = "unchanged"
        print(f"  {key:<34} {img['priority']:<10} {lu:<22} {status_str}")

    if result["alerts"]:
        print(f"\n  ⚠️  {len(result['alerts'])} alert(s):")
        for a in result["alerts"]:
            print(f"    • {a}")
    else:
        print(f"\n  ✅ No alerts")

    print(f"\n  Overall status: {result['status']}")
    print(f"  State file:     {STATE_FILE.relative_to(REPO)}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="JSON output to stdout")
    parser.add_argument("--init", action="store_true",
                        help="Seed state file, suppress change alerts on this run")
    args = parser.parse_args()
    result = main(json_output=args.json, init_only=args.init)
    sys.exit(0 if result["status"] in ("OK", "ALERT") else 1)
