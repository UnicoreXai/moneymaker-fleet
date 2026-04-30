# Architecture

A 10,000-foot view of how the pieces fit. For day-to-day ops, read
`QUICKSTART.md` instead.

## Core idea

Most fleet-management tooling for DePIN ops scrapes service
dashboards. That's brittle: dashboards add Cloudflare, change DOM
structure, log you out, return wrong totals. The few hours/week you
save by automating scraping you give back debugging breakage.

The alternative is to invert the problem:

  1. **Every service eventually pays you on-chain.**
  2. **You control the destination address.**
  3. **An on-chain transfer to your address is mathematically truth.**

So instead of scraping, the toolkit watches the wallet (via
Etherscan v2 + Solscan) and classifies incoming transfers by sender.
That's the **Layer 1 collector** — `realized_revenue_collector.py`.
It produces a stream of `{tx_hash, chain, ts, from, to, amount,
token, service, usd_at_time}` rows, idempotent on `tx_hash`.

Some services don't pay frequently (pending-balance accumulators with
$5/$10/$15/$50 thresholds). For those, you either wait or run a
**Layer 2 collector** — `layer2_revenue_collector.py` — which polls
service-side APIs or container logs and reports pending balances.
Layer 2 is best-effort; Layer 1 stays the source of truth.

```
                     ┌────────────────────────────────────┐
                     │ Your wallet (EVM + Solana)         │
                     │ — receives all on-chain payouts —  │
                     └─────────────┬──────────────────────┘
                                   │
                ┌──────────────────┴───────────────────┐
                │                                      │
   ┌────────────▼─────────────┐         ┌─────────────▼────────────────┐
   │ Layer 1 — on-chain       │         │ Layer 2 — fleet daemons      │
   │ realized_revenue_        │         │ daily_fleet_health.py +      │
   │ collector.py             │         │ vendor_image_watchdog.py +   │
   │ (Etherscan v2 + Solscan, │         │ proxycheap_health.py + per-  │
   │ classifies tx by sender) │         │ service Layer 2 collector    │
   └─────────────┬────────────┘         └──────────────┬───────────────┘
                 │                                      │
                 │ POST /api/realized-revenue           │ POST /api/layer2
                 │                                      │
                 └──────────┬───────────────────────────┘
                            │
                ┌───────────▼────────────┐
                │ data/*.jsonl (append-  │
                │ only, dedupe by hash)  │
                └───────────┬────────────┘
                            │
                ┌───────────▼────────────┐
                │ index.html             │
                │ (static, Chart.js fan  │
                │ charts, fleet matrix)  │
                └────────────────────────┘
```

## Per-node anatomy

```
   ┌─────────────────── one node (4 vCPU / 8 GB / 80 GB) ────────────────────┐
   │                                                                          │
   │  ┌───────────────┐                                                       │
   │  │ tun2socks     │◄── residential proxy URL (http://USER:PASS@IP:PORT)   │
   │  │ (Docker)      │                                                       │
   │  └──────┬────────┘                                                       │
   │         │ all egress traffic exits through residential IP                │
   │         │                                                                │
   │  ┌──────▼─────┐  ┌──────▼─────┐  ┌──────▼─────┐  ┌──────▼─────┐          │
   │  │ pawns      │  │ repocket   │  │ earnfm     │  │ traffmone- │  ...     │
   │  │            │  │            │  │            │  │ tizer      │          │
   │  └────────────┘  └────────────┘  └────────────┘  └────────────┘          │
   │                                                                          │
   │  Each container:                                                         │
   │     --network=container:tun2socks                                        │
   │     --restart unless-stopped                                             │
   │     -e <SERVICE_API_KEY|TOKEN>=...                                       │
   │                                                                          │
   │  Optional: mm-agent (Hermes worker)                                      │
   │     systemd timer every 15-30 min, posts to forum, pulls swarm-skills    │
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘
```

## Why tun2socks

DePIN services that pay residential-IP rates discount or reject
datacenter ASNs. The cheapest way to get residential egress for a
Hetzner/DO/Vultr box is to route the box's traffic through a paid
residential proxy. `tun2socks` is the lightest userspace
implementation of that pattern: a single container that creates a TUN
device and forwards packets through a SOCKS5 (or HTTP) proxy.

Other DePIN containers attach to tun2socks via Docker's
`--network=container:tun2socks` flag, which means they share its
network namespace and inherit the residential egress.

## Why one tun2socks per node, not one shared

Two reasons:

  1. **Per-node residential IPs == per-node revenue ceiling.** Most
     services cap earnings per-IP. Sharing one residential IP across
     N nodes caps the entire fleet at one node's worth of revenue.
  2. **Failure isolation.** When one residential proxy expires, only
     that node goes dark. A shared proxy means a single proxy outage
     kills the whole fleet's earnings.

## Multi-account fan-out

Most bandwidth services rate-limit per-account once you fan out across
more than ~5–10 IPs simultaneously (typical limits: Repocket
2 devices/IP across all your accounts; EarnFM 1 device/IP). Past that
point, registering more accounts and slicing the fleet across them is
the only way to scale.

The `*_split_redeploy.py` scripts implement the deterministic
fan-out:

  1. Capture each node's existing `tun2socks` netns binding.
  2. `docker rm -f <service>` (preserving the netns).
  3. `docker run -d --network <captured-netns> -e <NEW_TOKEN>=... <image>`
  4. Sleep 60s, verify `Running=true` and `RestartCount<=1`.

The pattern preserves residential egress while swapping accounts. If
you ever rebuild tun2socks, do it FIRST and rebuild the dependent
service containers SECOND.

## Hermes worker swarm

Optional. Each node optionally runs a small autonomous LLM worker
(Hermes-Agent) every 15–30 minutes. Workers post to a shared
forum-style chat, pull queen-distilled skills from this repo, and
attempt one ops + one revenue task per cycle. See
`docs/HERMES_SWARM.md` for setup.

## Dashboard

A single static `index.html` with embedded Chart.js fan-charts. Hosted
behind a tiny password-gated Node server (`server.js`). Production
deployment is Cloudflare Pages (frontend) + Railway (Node + API),
both free tiers.

The two writeable endpoints:

- `POST /api/realized-revenue` — Layer 1 collector pushes a new
  classified transfer here. Idempotent on `tx_hash`.
- `POST /api/layer2` — Layer 2 collector pushes a fleet-wide
  pending-balance snapshot.

Both use a shared header `X-Forum-Secret: <hex>` for auth.
