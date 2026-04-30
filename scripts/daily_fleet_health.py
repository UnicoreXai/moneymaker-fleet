"""
moneymaker-fleet — Daily Fleet Health entry point
=================================================
Runs as a scheduled task (cron / systemd timer / Windows Task Scheduler).
Pulls health signals across the fleet, updates the dashboard, optionally
commits + pushes any docs that changed.

Phases
------
  1a. Residential-proxy health (provider API)
  1b. Vendor Docker image watchdog (Docker Hub digests)
  1c. Per-service balance scrape (optional — disabled by default; many
      DePIN dashboards are Cloudflare-protected and will fail in
      headless. Layer 1 on-chain collector is the source of truth.)
  2.  Update local docs + index.html
  3.  Commit + push (skipped on --dry-run)

Usage
-----
  python scripts/daily_fleet_health.py [--dry-run] [--skip-scrape]

Env
---
  Pulls the rest from `.env` via your shell or systemd EnvironmentFile=.

Customize for your stack:
  - Replace `proxycheap_health` with your residential-proxy provider's
    API client.
  - Wire the per-service Layer 2 collector if you have one.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def git_commit_push(date: str, dry_run: bool = False) -> bool:
    """Stage docs that changed during the run, commit, push to origin/master.

    Files listed below are the ones the daily run is allowed to mutate.
    Anything else changing is a bug, not a daily-run artifact.
    """
    commit_msg = f"chore(daily): fleet health {date}"

    files_to_add = [
        "tmp/proxy_health.json",
        "tmp/vendor_image_digests.json",
        "tmp/vendor_image_watchdog.json",
        "tmp/roi_projection.json",
        "docs/MASTER_STATUS.md",
        "docs/ACTIVE_WORK.md",
        "index.html",
    ]

    if dry_run:
        print(f"\n[DRY RUN] Would commit: {commit_msg}")
        print(f"[DRY RUN] Files: {files_to_add}")
        return True

    try:
        for f in files_to_add:
            fpath = REPO / f
            if fpath.exists():
                if f.startswith("tmp/"):
                    subprocess.run(
                        ["git", "add", "-f", f], cwd=REPO, check=False, capture_output=True
                    )
                else:
                    subprocess.run(
                        ["git", "add", f], cwd=REPO, check=False, capture_output=True
                    )

        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        staged = result.stdout.strip()

        if not staged:
            print("  No changes to commit")
            return True

        print(f"  Staged files:\n    {staged.replace(chr(10), chr(10) + '    ')}")

        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"  Committed: {commit_msg}")

        result = subprocess.run(
            ["git", "push", "origin", "master"], cwd=REPO, capture_output=True, text=True
        )
        if result.returncode == 0:
            print("  Pushed to origin/master")
            return True
        print(f"  Push failed: {result.stderr[:200]}")
        return False

    except subprocess.CalledProcessError as e:
        print(f"  Git error: {e}")
        return False


def print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="moneymaker-fleet daily fleet-health driver"
    )
    parser.add_argument("--dry-run", action="store_true", help="Run without committing")
    parser.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Skip Layer 2 dashboard scraping; only run on-chain + image watchdog",
    )
    args = parser.parse_args()

    run_time = datetime.now(timezone.utc)
    date_str = run_time.strftime("%Y-%m-%d")

    print_section(f"moneymaker-fleet daily run — {run_time.isoformat()}")

    # ── Phase 1a: residential proxy provider API ─────────────────────────────
    print_section("Phase 1a: Residential proxy health")
    try:
        # Replace this import with your provider's client. The reference
        # implementation polls Proxy-Cheap; if you use a different provider,
        # swap it for one that returns proxy_id, status, bandwidth_mb,
        # expires_at, days_left.
        print("  (No proxy provider client wired in this open-source build.)")
        print("  Drop your provider's health-check module at scripts/proxy_health.py")
        print("  and import it here as e.g. `from scripts.proxy_health import check`.")
    except Exception as e:
        print(f"  Proxy health check failed: {e}")

    # ── Phase 1b: vendor Docker image digests ────────────────────────────────
    print_section("Phase 1b: Vendor Image Watchdog")
    try:
        from scripts.vendor_image_watchdog import check_images, write_to_active_work as wd_write
        watchdog_result = check_images()
        images = watchdog_result.get("images", [])
        alerts = watchdog_result.get("alerts", [])
        changed = [i for i in images if i.get("changed")]

        print(f"  Status: {watchdog_result.get('status', 'ERROR')}")
        print(f"  Watched: {len(images)} images | Changed since last check: {len(changed)}")
        for i in changed:
            sev = "[CRITICAL]" if i["priority"] == "critical" else "[INFO]"
            print(f"    {sev}  {i['image']}:{i['tag']} — {i['note']}")
        if alerts:
            wd_write(alerts, REPO)
    except Exception as e:
        print(f"  Watchdog failed: {e}")
        import traceback
        traceback.print_exc()

    # ── Phase 1c: optional balance scraping ──────────────────────────────────
    if not args.skip_scrape:
        print_section("Phase 1c: Balance Scraping (optional)")
        print("  No scraper wired — Layer 1 on-chain collector is the source of truth.")
        print("  See scripts/realized_revenue_collector.py.")
    else:
        print_section("Phase 1c: Skipped (--skip-scrape)")

    # ── Phase 3: commit + push ───────────────────────────────────────────────
    print_section("Phase 3: Commit + Push")
    git_commit_push(date_str, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
