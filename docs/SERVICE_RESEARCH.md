# Service research — what works, what doesn't

Distilled from a year of running a 25-node residential-egress fleet.
Written from the operator's chair: only services we've actually deployed
or seriously evaluated.

The hierarchy:

  1. **HARD BAN** — do not redeploy under any circumstance. Rationale
     listed per service.
  2. **CONDITIONAL** — pilot first, scale only on green. Pre-conditions
     listed.
  3. **WORKING** — currently earning on the production fleet.

Status as of 2026-04 (the date this open-source build was cut). DePIN
churn is high; spot-check before betting on anything.

---

## HARD BAN — do not redeploy

### Honeygain (legacy fleet attempts)
Account fingerprint cascade via shared home IP. Multiple accounts on
shared egress get mass-disabled within 2 weeks of fan-out. Honeygain's
IP-reputation engine (IPHub/IPQS-class) also rejects most residential-
proxy IPs, so even one-account-per-IP doesn't survive on rotating
residential blocks. **Do not deploy on a clustered fleet.**

### JumpTask (downline wallet 2)
Wallet-2 was banned at JumpTask side. Any wallet ever linked to it
returns 401. Auto-claim becomes a no-op.

### AntGain
Service defunct — domain listed for sale. Containers run with no
backend. Sweep and forget.

### Storj
Economically broken at the 50 GB / /24 cap that residential CGNAT
imposes. Killed twice on this fleet plus a final image-and-data sweep.
Don't try unless you have static IPv4 per node and dedicated 1+ TB
storage budget — at which point this isn't your fleet's profile anyway.

### Grass (community Docker images)
The Chrome driver fails on every public community image (mrcolorrain
et al). Removed fleet-wide. **Grass-the-service is conditional via the
official client only — see below.**

### EarnApp
VM/datacenter detection blocks the fleet 100%. 0/N nodes ever started
earning. EarnApp's bot-detection is heuristic + IP-reputation; once
flagged, the account is sticky-banned across re-signup attempts.

### WizardGain
Broken upstream image (duplicate-arg entrypoint). Don't redeploy until
vendor fixes; check `docker pull wizardgain/...` digest before
attempting.

### Checker Network / Zinnia / Spark
Filecoin-derived reward curve unproven at our scale. Silent fleet sweep
after no measurable payout signal in 60 days.

### Rivalz rClient
Pre-mainnet, unstable. Re-evaluate after mainnet.

### eBesucher
Re-banned post-research. PayPal restriction was lifted but three
independent structural blockers remain: (1) browser-only architecture
(only Linux path is engageub/InternetIncome Chromium+VNC at ~600MB
RAM/node — same class as the banned Grass community images);
(2) hard 1-device-per-IP cap + active VPN/proxy blocklist that rejects
most residential-proxy ASNs; (3) realistic earnings $0–12/mo at
fleet scale vs e.g. Pawns at $115/mo on the same RAM budget. No path
to revival absent a lightweight headless Linux client.

### proxies.sx Farmer Portal
SDK key deployed across fleet — total $0.00 earned, near-zero GB
routed. Datacenter IPs flagged "Hosting" at 30–40/100 quality;
residential IPs through tun2socks fail the "Download speed >= 0.5
Mbps" minimum. The portal's quality engine doesn't accept tunneled
residential traffic.

### ByteLixir downline pilot
Peer client is Windows + Android only; no Linux/Docker. Master uses
Proxy-paste model with all proxy slots filled — moving proxies
master → sub-account is zero-sum -50% net.

### URnetwork on tun2socks-bound nodes
The tun2socks netns blocks URnetwork's control-plane (verified by
raw-egress diag: nodes ran clean on raw VPS public IP, panicked behind
tun2socks). Don't redeploy URnetwork on any tun2socks-bound node;
either run it without tun2socks (datacenter IP — lower payout but
stable) or pick a different bandwidth service.

### Naptha (NapthaAI Node)
Per official `docker-compose.yml`, the node requires a 4-container
stack PER node: pgvector, RabbitMQ, node-app, LiteLLM proxy. node-app
`depends_on` pgvector + rabbitmq, no worker-only mode. Min footprint
~1–1.5 GB RAM/node before optional Ollama (4+ GB). Doesn't fit in
8 GB shared with other DePIN containers. Revenue speculative
($NAPTHA gate-listed, no per-node payout schedule). Don't redeploy
unless Naptha ships a worker-only mode pointing at an external shared
Postgres+RabbitMQ.

### Gradient Network Sentry
No official Linux CLI or Docker image — Sentry ships as a Chrome
extension only (per-account email signup + browser fingerprint).
Third-party wrappers exist but no wallet-keyed binary. Airdrop/points
only, no liquid token = $0 current value. Don't deploy. Reattempt
only if Gradient ships an official wallet-keyed Linux binary.

### OpenLedger
`openledger/node:latest` doesn't exist on Docker Hub. Real images
`openledgerhub/worker` + `/scraper` are a third party's personal
account, would earn for them, not for you. Official OpenLedger node
is an Electron GUI `.deb` requiring per-account email signup +
xvfb on each node + manual GUI setup-click. No wallet-keyed headless
mode. Skip until OpenLedger ships a CLI/wallet-keyed binary.

### OpenLoop
Domain `openloop.so` is parked on GoDaddy/Afternic — project
abandoned despite a 2024-12 raise. No working signup/login. Don't
pilot.

### GagaNode (jepbura/gaganode community image)
The community image's install script downloads `apphub-linux` from
an HTTP endpoint that now returns empty `DOWNLOADLINK=` /
`FILENAME=`, so containers loop forever on `apphub-linux does not
exist` without ever executing the agent. Dashboard "GAGA earned"
counters are zombie counters, not real income. Don't redeploy unless
GagaNode ships an official Docker image with verified payout history.

### AIOZ Network worker node
Worker `aioz-depin-cli v1.2.6` ran clean: registered, polled
`GetVerifyingContract` every ~65s, healthy heartbeat. Got exactly
ONE storage contract over 9h pilot; failed 123× with `client credit
is not enough` — this is **client-side** (the requester didn't fund
their AIOZ account), not your worker. Then silence for 7+ hours, no
further contracts dispatched. Indicates AIOZ supply-side saturation:
workers hugely outnumber paid demand, new entrants get scraps from
underfunded clients. Speculative projection of 4 AIOZ token/mo/node
turned out to be $0/mo realized. Don't redeploy unless tokenomics
shift.

### Akash Network (CPU provider)
Three-way reject: (a) the Hetzner CX33 class (4 vCPU/8 GB/80 GB) is
HALF Akash's official single-node minimum (8 vCPU/16 GB/150 GB SSD)
on every dimension — Akash docs reserve ~4 cores for k8s system
components, so a CX33 has *negative* tenant capacity before any lease;
(b) Hetzner Cloud system policies prohibit "applications used to mine
crypto currencies" and Akash provider rewards include AKT inflation/
take-fee distributions — single-account blast radius is your entire
fleet; (c) Akash CPU demand is structurally absent (Messari Q2 2025
reports <50% utilization fleet-wide), network pivoting to GPU/AI
which our class can't serve. Setup also requires k3s + own Cosmos
RPC node + public IPv4 + wildcard DNS + Cosmos `akash1...` address.
Don't redeploy unless Akash ships a worker-only mode <2 GB RAM AND
your provider permits it AND AKT spot recovers + CPU utilization
clears 70% sustained.

### Nodepay
The most-used community image is **archived** by its own author:
"Nodepay migrated to V2 and nothing they do makes any sense...
archived for reference". Image uses Chromium+Selenium extension-
scraping, same architecture class as the banned Grass community
images. JWT tokens expire in 1h, useless for static deploy. Manual
rotation × N nodes is operationally untenable.

### Mysterium (on Hetzner ASN)
Hetzner ASN24940 is consumer-deprioritized + Hetzner firewall blocks
inbound consumer dial-in. Daemons run, register, but earn ~$0 because
no consumer ever connects. Drop from revenue forecast. Daemons can
keep running idle (50 MB RAM/node) until natural removal at infra
churn — they don't hurt anything, they just don't earn.

### ProxyRack quarantined cohort
Server-side per-UUID quarantine. UUIDs registered outside a specific
deployment window get silently graylisted by ProxyRack's edge. No
client-side fix works (env, recreate, restart, network all tested).
Restoration would require new UUIDs (i.e. new ProxyRack accounts).
Don't redeploy on quarantined UUIDs.

---

## CONDITIONAL — pilot first, scale only on green

### Repocket
Works after `RP_API_KEY` rotation. The fleet historically broke when
keys silently invalidated; now stable post-rotation. Use the multi-
account split pattern (`scripts/repocket_split_redeploy.py`) to stay
under the 2-devices-per-IP cap.

### PacketStream
Account-status verification required before redeploy. Some accounts
get banned and the only signal is "we don't pay you" — log into the
dashboard and confirm referral/standing first.

### Honeygain (revival via non-cluster proxy)
PILOT FAILED on residential proxy: auth succeeded but service
returned `API Error: Network Unusable` — Honeygain's IP-reputation
engine rejects most residential-proxy IPs. Would need a different
residential proxy vendor with cleaner reputation (likely paid-tier
ISP proxy, not rotating residential).

### Pawns
The `iproyal/pawns-cli:latest` image works as long as the password
contains no shell-special characters (`$`, `` ` ``, etc.). Pick an
alphanumeric password to avoid fleet-wide restart loops on env
templating bugs.

---

## WORKING — currently on the production fleet

| Service        | Image                              | Notes                                                |
|----------------|------------------------------------|------------------------------------------------------|
| Pawns          | `iproyal/pawns-cli`                | Alphanumeric password only.                          |
| Repocket       | `repocket/repocket`                | Multi-account split required >5 nodes/account.       |
| EarnFM         | `earnfm/earnfm-client`             | Multi-account split required >5 IPs/account.         |
| TraffMonetizer | `traffmonetizer/cli_v2`            | Stable.                                              |
| ProxyRack      | `proxyrack/pop`                    | Avoid the per-UUID quarantine band.                  |
| Bitping        | (download per OS)                  | Pays in BTC. Currently outside Layer 1 collector.    |
| ByteLixir      | (proxy-paste, master account)      | 50% lifetime ref kickback if you can refer.          |
| Wipter         | `techroy23/docker-wipter`          | Pays USDT 1st-5th of month, $20 min.                 |
| Anyone Protocol| (per-relay binary)                 | Tor-fork relay; needs 100 ANYONE stake per relay.    |

### Pawns / EarnFM wallet-set rule

Both Pawns and EarnFM lock their payout-wallet UI until the account
hits the service's minimum withdrawal threshold ($5 Pawns, $15
EarnFM). Don't surface "set the Pawns/EarnFM wallet" as a follow-up
in any plan, runbook, or recommendation. Wait passively until the
dashboards report threshold-met.

---

## Update protocol

Before touching any banned service: re-check upstream. DePIN ops
turn over fast — a service that was dead 6 months ago might have
shipped a Linux client, fixed an image, or pivoted business model.
Update this file and PR if you find a service should be lifted.
