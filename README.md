# moneymaker-fleet

**Open-source toolkit for small DePIN fleet operators.**

If you run somewhere between 5 and 50 VPS or home boxes earning passive
crypto/USD income from bandwidth-sharing and decentralized-compute
services, this repo gives you the same plumbing the maintainer uses on a
production 25-node Hetzner fleet:

- **Layer 1** — on-chain revenue collector that watches a payout wallet
  and classifies incoming transfers by sending address (no scraping
  service dashboards).
- **Layer 2** — autonomous fleet collector that polls services exposing
  per-account API balances, plus a paramiko-based parallel SSH harness
  for fleet-wide ops.
- **Hierarchical agent swarm** ("Hermes") — one autonomous LLM worker
  per node coordinating through a forum-style shared chat, with a queen
  pattern that distills successful per-node fixes into fleet-wide
  skills.
- **Multi-account split scripts** — once a single account hits a
  service's per-IP cap, fan out to N accounts deterministically across
  the fleet without losing tun2socks/netns bindings.
- **Residential-IP scraping API** — FastAPI service that turns the
  fleet's egress proxies into a customer-facing scraping product, with
  five vertical wrappers (SERP, Amazon products, Google News, LinkedIn
  jobs, real estate) plus a clean `/v1/scrape` core.
- **Static dashboard** — single `index.html` with Chart.js fan-chart
  projections, fleet coverage matrix, and live revenue feed; pairs with
  a tiny Node server for password-gated hosting on Railway/Fly/etc.

Production-tested. Battle-scarred. Comes with a lot of opinions.

---

## Affiliate disclosure

The signup links throughout this README and `docs/AFFILIATE_DISCLOSURE.md`
are referral URLs from the maintainer's actual fleet. Signing up via these
links earns a small recurring commission at no cost to you (in most cases
the same link also gives **you** a signup bonus). Full per-service
breakdown of bonuses and kickbacks: `docs/AFFILIATE_DISCLOSURE.md`. If you
prefer to sign up without using these links, the bare service URLs are
listed there too.

---

## Who this is for

You probably want this repo if:

- You run **5–50 nodes** (anything smaller, the manual approach beats
  this; anything larger, you'll outgrow the JSONL+SSH model).
- Your target revenue is **$100–500/mo** of passive income.
- You're already comfortable with Linux, Docker, paramiko/ssh, and a
  little Python.
- You want a head start on the **"which service actually works"**
  research question — see `docs/SERVICE_RESEARCH.md` for a 3000-word
  ban-list of services the maintainer has tried, why they failed, and
  what to use instead.

You probably **don't** want this repo if:

- You want a one-click GUI installer. This is a toolkit, not a product.
- You're hoping to mass-farm airdrops. Most of the included services
  have anti-Sybil filters that will ban an IP cluster on sight.
- You expect the maintainer to debug your fleet. PRs are welcome,
  support is not offered.

---

## Architecture

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

The 25 nodes themselves are Hetzner CX33 boxes (4 vCPU / 8 GB RAM /
80 GB NVMe / Falkenstein or Helsinki). Each one runs:

- A `tun2socks` Docker container holding the residential-egress proxy.
- 5–10 DePIN service containers attached via
  `--network=container:tun2socks` (Pawns, Repocket, EarnFM,
  TraffMonetizer, ProxyRack, Mysterium, URnetwork, Bitping).
- Optionally, an `mm-agent` Hermes worker that runs every 15 min via
  systemd timer, posts to a shared forum, and pulls queen-distilled
  skills from this repo.

---

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/<your-fork>/moneymaker-fleet.git
cd moneymaker-fleet

# 2. Copy the example env, fill in the placeholders
cp config/example.env .env
$EDITOR .env

# 3. Install deps (Python 3.11+ recommended)
pip install -r requirements.txt

# 4. Bootstrap a single test node, then propagate
python scripts/single_node_setup.py --host <ip> --label node-01
python scripts/daily_fleet_health.py --dry-run
```

Read `docs/QUICKSTART.md` for the full first-day walkthrough.

---

## Stack

| Layer            | Tech                                                |
|------------------|-----------------------------------------------------|
| Fleet ops        | paramiko parallel SSH (multi-account/IP-pinned)     |
| Residential exit | tun2socks → Verizon residential proxy (US AS701)    |
| Service hosting  | Hetzner Cloud CX33 (Falkenstein / Helsinki)         |
| Scraping API     | FastAPI 0.115 + uvicorn[standard], in-memory state  |
| Frontend         | Vanilla JS + Chart.js 4.4 — single-file `index.html`|
| Hosting          | Cloudflare Pages (frontend) + Railway (Node + API)  |
| Worker swarm     | Hermes-Agent 0.11 over OpenRouter (DeepSeek default)|
| On-chain index   | Etherscan v2 unified (EVM-6) + Solscan / public RPC |

---

## Service signup playbook

These are the services the maintainer is currently running on the
production fleet. Each link is a referral URL — signing up through it
earns the maintainer a recurring commission at no extra cost to you, and
in most cases nets you a signup bonus too.

### Infrastructure (highest-LTV — recurring revenue)

These pay much more than DePIN signup bonuses. A single referred
customer who sticks around 12 months often exceeds the lifetime payout
of 50+ DePIN bandwidth signups.

- **Hetzner Cloud** — VPS provider. CX33 boxes ($8.59/mo) hit the
  sweet spot for the included DePIN stack (4 vCPU lets tun2socks +
  ~7 daemons coexist without contention). €20 / $20 referee credit
  (~2 months CX33 free) + €10 / $10 referrer credit. Get a code:
  <https://www.hetzner.com/legal/referrals>
- **Proxy-Cheap** — Verizon AS701 residential proxies, $3.60/mo each
  for 1 GB/mo. Sign up: <https://www.proxy-cheap.com>
  Partner program: <https://www.proxy-cheap.com/affiliate-program>
- **IPRoyal** — alternative residential proxy provider. **10% lifetime,
  up to $2,000/client**, 60-day cookie, recurring on subscription.
  Partner portal: <https://iproyal.com/affiliate-program/>
- **Bright Data** — enterprise residential proxies for the segment that
  outgrows Proxy-Cheap. **50% revenue share, up to $2,500/client**,
  90-day cookie, recurring. Partner portal:
  <https://brightdata.com/affiliate>
- **Decodo** (formerly SmartProxy) — residential proxies with free
  trial. Up to 50% recurring (tiered). Partner portal:
  <https://decodo.com/affiliate>

### Bandwidth-sharing services (small recurring + signup bonuses)

- **Pawns.app** (formerly IPRoyal Pawns) — $1 × your first 3 payouts,
  then 10% lifetime, plus $3 mutual signup bonus. Sign up:
  <https://pawns.app/?r=19368951>
- **EarnFM** — 10% lifetime, no cap, no expiry, + $5 joinee bonus,
  1 device per IP. Sign up: <https://earn.fm/ref/KEVIS33F>
- **Repocket** — 10% lifetime + $5 (paid only after your first
  payout), max 2 devices/IP. Sign up: <https://link.repocket.com/>
  *(refcode pending — fill in the placeholder once you have it)*
- **TraffMonetizer** — 10% lifetime on referral payouts + $5 joinee
  bonus. Sign up: <https://traffmonetizer.com/?aff=2116090>
- **ByteLixir** — **50% LIFETIME** payouts on referral revenue + $1
  joinee (highest DePIN-side rate). Sign up:
  <https://bytelixir.com/r/5TQHS6GNCJQF>
- **Bitping** — flat $5/$5 mutual on signup. Sign up:
  <https://app.bitping.com>
- **ProxyRack Peer Program** — 10% on referral earnings + $5 to
  invitee. Sign up:
  <https://peer.proxyrack.com/ref/i2ip7fsyw5osb9l5tecenvnenl7lz3qvwxevvvtx>
  *(standalone "Affiliate Program" is in re-launch waitlist)*
- **Wipter** — up to 10% commission (treat as ceiling, not floor).
  Sign up: <https://www.wipter.com/en>

### DePIN compute / relay

- **Anyone Protocol** (Tor-fork relay network) — operator referral
  varies by cohort. Sign up: <https://anyone.io>
  *(refcode pending — check operator dashboard after first 100 ANYONE stake)*
- **URnetwork (ur.io)** — 50% of referral earnings + 50% of their
  signup bonus. Sign up: <https://ur.io/c?bonus=K34ILE>
- **Titan Edge / TitanNet** — referral terms TBD. Sign up:
  <https://edge.titannet.info/signup?inviteCode=W362AVG2>

### Airdrop / points (high anti-Sybil risk — see `docs/SERVICE_RESEARCH.md`)

- **Grass** — 20% L1 / 10% L2 / 5% L3 lifetime points. **One
  account per person**, IP-cluster ban if multiple devices share a
  /24. Sign up: <https://app.grass.io/register?referralCode=KK4YrG7se6ZN-4k>
- **Nodepay** — 10% L1 / 5% L2. Same anti-cluster rules as Grass. Sign
  up: <https://app.nodepay.ai/register>
  *(refcode pending — Nodepay uses your Solana wallet address as the
  refcode, so the maintainer's link is intentionally omitted from the OSS
  build to avoid leaking a wallet address. See `docs/SERVICE_RESEARCH.md`
  on the Nodepay HARD BAN before deploying.)*
- **Teneo Protocol** — 5,000 pts/referral + 2,500 pts joinee. Sign up:
  <https://dashboard.teneo.pro/auth/signup?referralCode=Mr4ku>
- **Gradient Network (Sentry)** — 20 pts on 72h uptime + 10% L1 + 5%
  L2. Sign up: <https://app.gradient.network/signup?code=R48OZS>
- **PacketStream** — 20% lifetime promotional. Sign up:
  <https://packetstream.io/?psr=7yqG>

For each service: read `docs/SERVICE_RESEARCH.md` for current
status, anti-Sybil notes, and known failure modes before deploying
fleet-wide. The maintainer has burned multiple accounts learning this
the hard way.

---

## Repo layout

```
moneymaker-fleet/
├── README.md                  ← you are here
├── LICENSE                    ← MIT
├── requirements.txt
├── package.json               ← Node server (dashboard host)
├── server.js
├── index.html                 ← single-file dashboard (with example data)
├── config/
│   ├── example.env
│   ├── example.fleet.json
│   ├── example.sender_addresses.json
│   ├── hermes/                ← per-node Hermes worker config
│   └── scraping_api/
│       ├── example.keys.json
│       └── example.fleet_proxies.json
├── scripts/
│   ├── realized_revenue_collector.py   ← Layer 1 (on-chain)
│   ├── layer2_revenue_collector.py     ← Layer 2 (fleet daemons)
│   ├── regen_revenue_projection.py     ← GBM fan-chart projector
│   ├── daily_fleet_health.py           ← entry point for cron
│   ├── vendor_image_watchdog.py        ← Docker Hub digest poll
│   ├── proxycheap_health.py            ← Proxy-Cheap API health
│   ├── single_node_setup.py            ← bootstrap one node
│   ├── repocket_split_redeploy.py      ← multi-account split pattern
│   ├── earnfm_split_redeploy.py        ← (same pattern, EarnFM)
│   ├── hermes/
│   │   ├── queen_synthesis.py          ← queen → swarm-skills
│   │   └── fleet_propagate.py          ← push skills update
│   └── scraping_api/                   ← FastAPI service
│       ├── app.py
│       ├── auth.py
│       ├── proxy_pool.py
│       ├── metrics.py
│       ├── mint_key.py
│       ├── Dockerfile
│       ├── docker-compose.yml
│       ├── openapi.yaml
│       ├── requirements.txt
│       └── verticals/
│           ├── serp/
│           ├── amazon_products/
│           ├── google_news/
│           ├── linkedin_jobs/
│           └── real_estate/
└── docs/
    ├── QUICKSTART.md
    ├── ARCHITECTURE.md
    ├── SERVICE_RESEARCH.md             ← what works / what doesn't
    ├── AFFILIATE_DISCLOSURE.md
    └── HERMES_SWARM.md
```

---

## License

MIT — see `LICENSE`. The maintainer makes no warranty about which
DePIN services will still be paying out by the time you read this. Use
at your own risk; on-chain receives are the only data you should
actually trust.
