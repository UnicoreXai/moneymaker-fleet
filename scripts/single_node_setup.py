#!/usr/bin/env python3
"""
single_node_setup.py — bootstrap one fresh node for the moneymaker-fleet stack.

This is the "deploy onto a brand-new VPS" entrypoint. Run it once per
node. After it succeeds, the node is in steady state — ongoing changes
go through the multi-account split scripts and the Hermes worker.

What it does
------------
  1. SSH in (paramiko) using --host + FLEET_SSH_PASS.
  2. Install Docker + jq + a couple of conveniences.
  3. Pull a tun2socks image and create the tun2socks container bound to
     a residential proxy URL you provide.
  4. (Optional) launch one or more DePIN containers attached via
     `--network=container:tun2socks`. These are read from
     config/services.example.json — copy + edit per your fleet.

Inputs
------
  --host        Required. Public IP of the new node.
  --label       Required. A friendly fleet label (e.g. "node-01").
  --proxy-url   Required. Residential-proxy URL (http://USER:PASS@IP:PORT).
  --services    Optional. Path to a service spec JSON. Defaults to
                config/services.example.json.

Env
---
  FLEET_SSH_PASS    Required.

This script intentionally stops short of:
  - Wallet provisioning (out of scope for the OSS template; see
    /scripts/wallet_provisioner.py for the pattern in your private fork).
  - Hermes worker install (run scripts/hermes/fleet_propagate.py instead
    once your queen is set up).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]


INSTALL_CMDS = [
    "command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh",
    "apt-get update -y && apt-get install -y jq curl ca-certificates",
    "systemctl enable --now docker",
]

TUN2SOCKS_RUN = (
    "docker rm -f tun2socks 2>/dev/null || true; "
    "docker run -d --name tun2socks --restart unless-stopped "
    "--cap-add NET_ADMIN --device /dev/net/tun "
    "-e PROXY='{proxy_url}' "
    "xjasonlyu/tun2socks:latest"
)


def ssh_exec(client: paramiko.SSHClient, cmd: str, timeout: int = 60):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--proxy-url", required=True)
    ap.add_argument("--services", default=str(ROOT / "config" / "services.example.json"))
    args = ap.parse_args()

    pw = os.environ.get("FLEET_SSH_PASS")
    if not pw:
        sys.exit("Set FLEET_SSH_PASS env var (your fleet's root password)")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(args.host, username="root", password=pw, timeout=30)

    print(f"[{args.label}] connected to {args.host}")

    print(f"[{args.label}] installing base packages")
    for cmd in INSTALL_CMDS:
        rc, out, err = ssh_exec(client, cmd, timeout=180)
        if rc != 0:
            print(f"  WARN: cmd `{cmd[:60]}...` exit={rc} err={err[:120]}")

    print(f"[{args.label}] launching tun2socks")
    rc, out, err = ssh_exec(client, TUN2SOCKS_RUN.format(proxy_url=args.proxy_url), timeout=60)
    if rc != 0:
        print(f"  ERROR: tun2socks launch failed: {err[:300]}")
        return 1
    print(f"  tun2socks: {out[:24]}")

    services_path = Path(args.services)
    if services_path.exists():
        services = json.loads(services_path.read_text())
        for svc in services.get("services", []):
            name = svc["name"]
            image = svc["image"]
            envs = " ".join(f"-e {k}='{v}'" for k, v in svc.get("env", {}).items())
            run = (
                f"docker rm -f {name} 2>/dev/null || true; "
                f"docker run -d --name {name} --network=container:tun2socks "
                f"--restart unless-stopped {envs} {image}"
            )
            print(f"[{args.label}] launching {name}")
            rc, out, err = ssh_exec(client, run, timeout=120)
            if rc != 0:
                print(f"  WARN: {name} failed: {err[:200]}")
            else:
                print(f"  {name}: {out[:24]}")
    else:
        print(f"  ({services_path} missing — skipping service launch.)")

    client.close()
    print(f"[{args.label}] done. Verify with `docker ps` over SSH.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
