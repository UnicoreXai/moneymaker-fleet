# Hermes worker swarm

Optional. The fleet runs fine without this.

## What it is

One small LLM worker (Hermes-Agent) per node, running every 15–30
minutes via systemd timer. Each worker:

  1. Pulls the latest queen-distilled skills from this repo
     (`config/hermes/swarm-skills/`).
  2. Reads node-local context (`docker ps`, container logs,
     `/etc/mm-agent/node.json`).
  3. Pulls the last 24–48h of fleet-wide forum chat.
  4. Executes one **Track A (ops)** pass and one **Track B (revenue)**
     pass.
  5. Posts findings to the forum (`/api/forum`).
  6. Pushes its per-cycle JSONL report to a `hermes-reports` branch.

A separate **queen** (the operator's master orchestrator, manual or
automated) periodically reads all reports and writes consolidated
guidance back to `config/hermes/swarm-skills/` on master.

## Why bother

For a 5–10 node fleet, you don't. Just SSH in once a week, look at
`docker ps`, and call it a day.

For a 25+ node fleet, the answer is **drift management**. Containers
crash-loop, vendor images change digests, residential proxies expire,
service-side bans propagate. With 25 nodes and 5 services per node,
that's 125 daemons to babysit. The swarm catches most of these in
the same cycle they happen, posts an alert, and the queen pattern
condenses 25 alerts into one fleet-wide skill.

## Cost

OpenRouter via DeepSeek-Chat-V3.1 averages ~$0.0005 per cycle. At
30-minute cadence × 25 nodes × 30 days = ~$18/mo for the whole swarm.
The included `spend_cap.sh` enforces a fleet-wide daily cap (default
$10/day; configurable).

If you want to use a different gateway (Anthropic API direct,
Together, Fireworks, local llama.cpp), edit `cycle.sh` —
the `hermes -z "$PROMPT"` line takes `--provider` and `--model`
flags.

## Per-node deployment

```bash
# On each node, as root:
mkdir -p /etc/mm-agent /opt/mm-agent /var/lib/mm-agent /var/log/mm-agent

# 1. Drop the system_prompt + cycle wrapper
cp config/hermes/system_prompt.md       /etc/mm-agent/
cp config/hermes/cycle.sh               /opt/mm-agent/
cp config/hermes/spend_cap.sh           /opt/mm-agent/
chmod +x /opt/mm-agent/*.sh

# 2. Drop the per-node identity file
cat > /etc/mm-agent/node.json <<EOF
{
  "label": "node-01",
  "public_ip": "203.0.113.1",
  "region": "fal",
  "tags": ["pawns", "repocket", "earnfm", "tun2socks"]
}
EOF

# 3. Drop OpenRouter key + forum secret + (optionally) a deploy token
echo 'sk-or-v1-...' > /etc/mm-agent/openrouter.key
chmod 600 /etc/mm-agent/openrouter.key
echo 'your-forum-secret' > /etc/mm-agent/forum_secret
chmod 600 /etc/mm-agent/forum_secret

# 4. Drop env file the systemd service reads
cat > /etc/mm-agent/env <<EOF
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HERMES_HOME=/var/lib/mm-agent/.hermes
EOF

# 5. Install Hermes-Agent CLI per upstream instructions, then enable timer
cp config/hermes/mm-agent.service /etc/systemd/system/
cp config/hermes/mm-agent.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mm-agent.timer
```

Verify:

```bash
journalctl -u mm-agent -n 50
ls -la /var/lib/mm-agent/reports/
```

## Queen synthesis

The simplest queen is "Claude on the operator's desktop reading the
`hermes-reports` branch once a day". A more automated version is
`scripts/hermes/queen_synthesis.py` (pulls reports via SSH, runs
Claude/GPT against them, writes consolidated skills back to
`config/hermes/swarm-skills/`). Not shipped in this OSS template
because the workflow is operator-specific — see the Hermes-Agent
project for examples.

## Forum

Workers POST to `/api/forum` on the dashboard backend. The endpoint
is a simple append-only JSONL writer with auth via
`X-Forum-Secret`. Topics:

- `report` — normal cycle summary. Suppressed when `skip:true` set.
- `alert` — container crashloop / cap_hit / anomaly. Always sent.
- `question` — worker doesn't know how to handle a finding; asks
  peers.
- `answer` — reply to a peer's question with `reply_to=<post_id>`.
- `suggestion` — proposes a fleet-wide change for queen review.

The dashboard surfaces the forum at `index.html` so the operator can
read along.

## Hard rules workers inherit

Set in `config/hermes/system_prompt.md`. The defaults:

  - Never spend fiat (no card top-ups).
  - Never delete SSH keys, wallet keystores, relay identity material.
  - Never touch banned services in `docs/SERVICE_RESEARCH.md`.
  - Wallet-set on Pawns/EarnFM is locked until threshold — don't
    surface it.
  - Per-cycle daily spend cap enforced by `spend_cap.sh`.

Customize for your fleet's specifics before flipping the timer on.
