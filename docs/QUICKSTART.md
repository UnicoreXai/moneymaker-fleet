# Quick start

A first-day walkthrough. Assumes Linux/macOS dev environment + a
single fresh VPS to deploy onto.

## 0. Prerequisites

- Python 3.11+
- Node.js 18+ (for the dashboard host)
- Docker on your fleet nodes
- A residential proxy (Proxy-Cheap, IPRoyal, etc.) — at minimum one
  proxy URL of the form `http://USER:PASS@HOST:PORT`
- A wallet you control (MetaMask is fine). Any EVM chain payouts
  land here. Optionally a Solana wallet.

## 1. Clone + bootstrap

```bash
git clone https://github.com/<your-fork>/moneymaker-fleet.git
cd moneymaker-fleet

# Configure
cp config/example.env .env
$EDITOR .env

# Set at minimum:
#   WALLET_EVM = <your 0x... wallet>
#   ETHERSCAN_KEY = <free Etherscan v2 key>
#   FLEET_SSH_PASS = <your fleet root password>
```

## 2. Install Python deps

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 3. Bootstrap one node

```bash
# Edit fleet inventory
cp config/example.fleet.json config/fleet.json
$EDITOR config/fleet.json   # add one node

# Edit the per-node service spec
cp config/services.example.json config/services.json
$EDITOR config/services.json

# Deploy
export FLEET_SSH_PASS='<root password>'
python scripts/single_node_setup.py \
    --host <node-ip> \
    --label node-01 \
    --proxy-url 'http://USER:PASS@HOST:PORT'
```

After ~2 minutes, SSH into the node and verify:

```bash
ssh root@<node-ip>
docker ps
# expect: tun2socks + pawns + repocket + earnfm + ... all Running
```

## 4. Backfill realized revenue

Once you've been earning for a few days and at least one service has
paid out:

```bash
# One-shot backfill
python scripts/realized_revenue_collector.py
```

The script writes any new transfers to your dashboard backend. Check
`tmp/realized_revenue_unknown_senders.json` for any unknown senders —
the most common case is a new service whose hot wallet hasn't been
mapped in `config/sender_addresses.json` yet.

To classify an unknown:

  1. Open the tx in an explorer.
  2. Copy the `from` address.
  3. Add it to the matching service in
     `config/sender_addresses.json` under `addresses` with the right
     chain.
  4. Re-run the collector.

## 5. Run the dashboard locally

```bash
DASHBOARD_PASSWORD=hunter2 npm start
# -> http://localhost:8787
```

The default `index.html` is a static page that reads pre-baked data
embedded at build time. To populate it from real data, run:

```bash
python scripts/regen_revenue_projection.py
```

## 6. Schedule the daily run

```bash
# Linux: crontab -e
0 7 * * * cd /path/to/moneymaker-fleet && \
    /path/to/.venv/bin/python scripts/daily_fleet_health.py

# Or use a systemd timer (template not shipped — pattern is the same
# as config/hermes/mm-agent.timer).
```

## 7. (Optional) Spin up the Hermes worker swarm

The autonomous worker swarm is intentionally optional — the rest of
the toolkit works fine without it. See `docs/HERMES_SWARM.md` for the
deeper setup.

---

## Common first-day issues

- **`tun2socks` container exits immediately.** Check the proxy URL
  format: `http://USER:PASS@HOST:PORT`. If the password contains `@`,
  `:`, or `$`, URL-encode them.
- **`pawns` restart loop.** The Pawns CLI is sensitive to shell-special
  characters in the password. Use an alphanumeric password.
- **`repocket` exits with `e { error: {} }`.** Your `RP_API_KEY` is
  silently invalid. Rotate it in the Repocket dashboard and recreate
  the container.
- **`earnfm` reports `auth_locked` / "User is limited".** You're past
  the per-account IP fan-out cap. Use the multi-account split pattern
  in `scripts/earnfm_split_redeploy.py`.
