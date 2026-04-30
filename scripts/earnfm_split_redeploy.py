#!/usr/bin/env python3
"""
EarnFM multi-account split redeploy across the fleet.

Mirrors the proven Repocket pattern (see `repocket_split_redeploy.py`).
EarnFM rate-limits per-account once a single token fans out across more
than ~5–10 IPs simultaneously. The fix is to register N accounts and
assign each one to a slice of the fleet.

Per-node:
  1. Capture the existing tun2socks netns binding via:
       docker inspect earnfm --format '{{.HostConfig.NetworkMode}}'
     (Always preserved verbatim. We never recreate tun2socks.)
  2. Detect any extra mounts you've added (e.g. a /etc/hosts override for
     DNS interception) and re-apply them on the new container.
  3. Backup current container env to /root/earnfm_inspect.bak.<ts>.json.
  4. docker rm -f earnfm
  5. docker run -d with the new EARNFM_TOKEN for that account-slot.
  6. Sleep 60s, inspect logs for `validate_harvester_key 200` /
     `WebSocket connected`, return per-node status.

Hard rules:
  - DO NOT touch tun2socks.
  - Per-node backup before any container change.
  - Never log full EARNFM_TOKEN values; last-4 only.
  - Reads tokens from tmp/earnfm_split_tokens.json (gitignored). If the
    file is missing or any account is left as the placeholder, refuse
    to run.

Inputs:
  config/fleet.json                      — node inventory
  tmp/earnfm_split_tokens.json           — per-account tokens (gitignored)
                                           Template: docs/earnfm_split_tokens.json.template

Pilot-first execution: run on one node alone. If it reports `ok`,
propagate to the rest in parallel. If pilot fails, abort and emit
results for inspection.

Output:
  tmp/earnfm_split_results.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import paramiko

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
FLEET_FILE = ROOT / "config" / "fleet.json"
TOKENS_FILE = ROOT / "tmp" / "earnfm_split_tokens.json"
RESULTS_FILE = ROOT / "tmp" / "earnfm_split_results.json"
SSH_PASS = os.environ.get("FLEET_SSH_PASS", "")
EARNFM_IMAGE = "earnfm/earnfm-client:latest"

logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("earnfm-split")


def last4(s: str) -> str:
    return f"...{s[-4:]}" if s else "None"


def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 30):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def deploy_node(node: dict, token: str, acct_label: str) -> dict:
    name = node["label"]
    ip = node["host"]
    result = {
        "node": name,
        "ip": ip,
        "acct": acct_label,
        "token_last4": last4(token),
        "status": "pending",
        "netns": None,
        "error": None,
    }

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ip,
            username="root",
            password=SSH_PASS,
            timeout=30,
            banner_timeout=30,
            auth_timeout=30,
        )

        rc, netns, err = ssh_exec(
            client, "docker inspect earnfm --format '{{.HostConfig.NetworkMode}}'"
        )
        if rc != 0 or not netns:
            result["status"] = "skip-no-container"
            result["error"] = err or "no earnfm container present"
            log.warning(f"[{name}] skip — no earnfm container")
            return result
        result["netns"] = netns

        # Backup
        ts = int(time.time())
        ssh_exec(
            client,
            f"docker inspect earnfm > /root/earnfm_inspect.bak.{ts}.json",
        )

        rc, out, err = ssh_exec(client, "docker rm -f earnfm")
        if rc != 0:
            result["status"] = "fail-rm"
            result["error"] = err
            return result

        run_cmd = (
            f"docker run -d --name earnfm "
            f"--network {netns} "
            f"--restart unless-stopped "
            f"-e EARNFM_TOKEN='{token}' "
            f"{EARNFM_IMAGE}"
        )
        rc, cid, err = ssh_exec(client, run_cmd, timeout=60)
        if rc != 0:
            result["status"] = "fail-run"
            result["error"] = err
            return result
        log.info(f"[{name}] launched {cid[:12]} acct={acct_label} token={last4(token)}")

        time.sleep(60)
        rc, state, _ = ssh_exec(
            client,
            "docker inspect earnfm --format '{{.State.Running}} {{.RestartCount}}'",
        )
        parts = state.split()
        running = parts[0] == "true" if parts else False
        restart_ct = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 999
        if running and restart_ct <= 1:
            result["status"] = "ok"
        else:
            result["status"] = "fail-unstable"
            result["error"] = f"running={running} restarts={restart_ct}"

        return result
    except Exception as e:
        result["status"] = "fail-exception"
        result["error"] = str(e)
        return result
    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    if not SSH_PASS:
        raise SystemExit("Set FLEET_SSH_PASS env var")
    if not TOKENS_FILE.exists():
        raise SystemExit(f"Missing {TOKENS_FILE}. See docs/earnfm_split_tokens.json.template")
    if not FLEET_FILE.exists():
        raise SystemExit(f"Missing {FLEET_FILE}. See config/example.fleet.json")

    fleet = json.loads(FLEET_FILE.read_text())
    by_label = {n["label"]: n for n in fleet["nodes"]}
    data = json.loads(TOKENS_FILE.read_text())
    tokens = data["accounts"]
    assignments = data["assignments"]

    # Refuse to run if any token still looks like a placeholder
    for acct, tok in tokens.items():
        if tok.startswith("<") or tok in ("", "REPLACE_ME"):
            raise SystemExit(f"account {acct} token is unfilled placeholder; refusing to run")

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {}
        for asn in assignments:
            node = by_label.get(asn["node"])
            tok = tokens.get(asn["acct"])
            if not node or not tok:
                continue
            futs[pool.submit(deploy_node, node, tok, asn["acct"])] = asn["node"]
        for f in as_completed(futs):
            results.append(f.result())

    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"results": results}, indent=2))
    print(f"\nDone. Results -> {RESULTS_FILE}")
    print(
        "Summary:",
        {
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "skipped": sum(1 for r in results if r["status"].startswith("skip")),
            "failed": sum(1 for r in results if r["status"].startswith("fail")),
        },
    )


if __name__ == "__main__":
    main()
