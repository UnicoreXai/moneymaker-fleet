#!/usr/bin/env python3
"""
Repocket multi-account split redeploy across N nodes (paramiko parallel SSH).

Pattern: many bandwidth-sharing services rate-limit per-account once a single
account fans out to more than ~5–10 IPs simultaneously. The fix is to register
N accounts and assign each one to a slice of the fleet, so each account stays
under the limit.

Per-node:
  1. Capture exact tun2socks netns binding via:
       docker inspect repocket --format '{{.HostConfig.NetworkMode}}'
     (Always preserved verbatim. We never recreate tun2socks.)
  2. Capture old env (for backup log only — keys redacted to last-4 in stdout).
  3. docker rm -f repocket
  4. docker run -d --name repocket --network <captured-netns> --restart unless-stopped
       -e RP_EMAIL=<acct> -e RP_API_KEY=<key> repocket/repocket:latest
  5. Sleep 60s, verify Running=true, RestartCount<=1.

Hard rules:
  - DO NOT touch tun2socks. Preserve netns string verbatim.
  - If repocket missing on a node, log + skip (no fresh container without
    netns context — would break egress routing).
  - NEVER print full RP_API_KEYs in logs.

Inputs:
  config/fleet.json                    — node inventory (host, label per node)
  tmp/repocket_split_keys.json         — per-account credentials (gitignored)

Output:
  tmp/repocket_split_results.json
"""
import json
import os
import sys
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
FLEET_FILE = ROOT / "config" / "fleet.json"
KEYS_FILE = ROOT / "tmp" / "repocket_split_keys.json"
RESULTS_FILE = ROOT / "tmp" / "repocket_split_results.json"
SSH_PASS = os.environ.get("FLEET_SSH_PASS", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("repocket-split")


def last4(s: str) -> str:
    return f"...{s[-4:]}" if s else "None"


def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 30):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def deploy_node(node: dict, acct: dict) -> dict:
    name = node["label"]
    ip = node["host"]
    email = acct["email"]
    api_key = acct["api_key"]
    result = {
        "node": name,
        "ip": ip,
        "email": email,
        "api_key_last4": last4(api_key),
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

        # 1. Capture netns
        rc, netns, err = ssh_exec(
            client, "docker inspect repocket --format '{{.HostConfig.NetworkMode}}'"
        )
        if rc != 0 or not netns:
            result["status"] = "skip-no-container"
            result["error"] = err or "no repocket container present"
            log.warning(f"[{name}] skip — no repocket container ({err})")
            return result
        result["netns"] = netns
        log.info(f"[{name}] netns = {netns}")

        # 2. Capture old env (record only — never log full)
        rc, old_env, _ = ssh_exec(
            client, "docker inspect repocket --format '{{json .Config.Env}}'"
        )
        log.info(f"[{name}] old env captured ({len(old_env)} chars)")

        # 3. Remove old container
        rc, out, err = ssh_exec(client, "docker rm -f repocket")
        if rc != 0:
            result["status"] = "fail-rm"
            result["error"] = err
            log.error(f"[{name}] rm failed: {err}")
            return result

        # 4. Recreate with new account
        run_cmd = (
            f"docker run -d --name repocket "
            f"--network {netns} "
            f"--restart unless-stopped "
            f"-e RP_EMAIL='{email}' "
            f"-e RP_API_KEY='{api_key}' "
            f"repocket/repocket:latest"
        )
        rc, cid, err = ssh_exec(client, run_cmd, timeout=60)
        if rc != 0:
            result["status"] = "fail-run"
            result["error"] = err
            log.error(f"[{name}] run failed: {err}")
            return result
        log.info(f"[{name}] launched {cid[:12]} acct={email} key={last4(api_key)}")

        # 5. Wait + verify
        time.sleep(60)
        rc, state, err = ssh_exec(
            client,
            "docker inspect repocket --format '{{.State.Running}} {{.RestartCount}}'",
        )
        result["state"] = state
        if rc != 0:
            result["status"] = "fail-verify"
            result["error"] = err
            return result

        parts = state.split()
        running = parts[0] == "true" if parts else False
        restart_ct = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 999

        if running and restart_ct <= 1:
            result["status"] = "ok"
            log.info(f"[{name}] OK running=true restarts={restart_ct}")
        else:
            result["status"] = "fail-unstable"
            result["error"] = f"running={running} restarts={restart_ct}"
            log.warning(f"[{name}] unstable: {state}")

        return result
    except Exception as e:
        result["status"] = "fail-exception"
        result["error"] = str(e)
        log.error(f"[{name}] exception: {e}")
        return result
    finally:
        try:
            client.close()
        except Exception:
            pass


def main():
    if not SSH_PASS:
        raise SystemExit("Set FLEET_SSH_PASS env var (your fleet's root password)")
    if not KEYS_FILE.exists():
        raise SystemExit(
            f"Missing {KEYS_FILE}. Format: "
            '{ "accounts": { "A": {...}, "B": {...} }, "assignments": [{"node": "node-01", "acct": "A"}, ...] }'
        )
    if not FLEET_FILE.exists():
        raise SystemExit(f"Missing {FLEET_FILE}. See config/example.fleet.json")

    fleet = json.loads(FLEET_FILE.read_text())
    by_label = {n["label"]: n for n in fleet["nodes"]}

    data = json.loads(KEYS_FILE.read_text())
    accounts = data["accounts"]
    assignments = data["assignments"]

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {}
        for asn in assignments:
            node = by_label.get(asn["node"])
            acct = accounts.get(asn["acct"])
            if not node or not acct:
                log.warning(f"skip — missing node {asn['node']} or acct {asn['acct']}")
                continue
            futs[pool.submit(deploy_node, node, acct)] = asn["node"]

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
