#!/usr/bin/env python3
"""realized_revenue_collector.py — fleet-resident on-chain truth.

Polls Etherscan-family explorers (single Etherscan V2 key covers all EVM
chains) + Solana RPC for incoming transfers to your main payout wallet,
classifies each by sending address -> service, and POSTs new rows to the
dashboard `/api/realized-revenue` endpoint.

Why on-chain:
  Service dashboards lie / log out / get scraped wrong / require the operator to be
  at his desk. The sum of incoming token transfers + native receives to a
  hardcoded receive address is mathematically correct and zero-maintenance.

Idempotency:
  Each tx is keyed on (chain, tx_hash). The local cache file
  tmp/realized_revenue_cache.json tracks already-POSTed hashes. The server
  endpoint also dedupes by tx_hash, so double-posting is safe.

Out-of-scope (Phase 1):
  - TRON (Proxysell, ByteLixir USDT-TRC20)
  - Bitcoin (Bitping)
  - Titan L1
  - Per-tx historical USD price (we treat USDC/USDT as $1.00; native
    receives use a static fallback price hardcoded below — fine for our
    revenue scale where native sends are <$5/mo).

Inputs (env, loaded from /etc/mm-collector/api_keys.env when run as a daemon):
  ETHERSCAN_KEY     — Etherscan V2 key (covers ETH/Polygon/Arbitrum/Optimism/Base/BSC)
  SOLSCAN_KEY       — Solscan Pro key (optional; falls back to public Solana RPC)
  FORUM_SECRET      — shared secret for /api/realized-revenue POST
  DASHBOARD_BASE    — your dashboard backend (e.g. https://dashboard.example.com)
  WALLET_EVM        — your EVM payout wallet (required; no default)
  WALLET_SOL        — your Solana payout wallet (optional)
  LOOKBACK_DAYS     — defaults to 30
"""
from __future__ import annotations

import json
import os
import sys
import time
import logging
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone, timedelta

REPO = Path(__file__).resolve().parent.parent
# Sender lookup: search common locations so the script works both from the
# repo (config/sender_addresses.json) and from a deploy directory
# (alongside the script at /opt/mm-collector/sender_addresses.json).
def _resolve_sender_lookup() -> Path:
    here = Path(__file__).resolve().parent
    candidates = [
        REPO / "config" / "sender_addresses.json",
        here / "sender_addresses.json",
        here / "config" / "sender_addresses.json",
        Path("/opt/mm-collector/sender_addresses.json"),
        Path("/opt/mm-collector/config/sender_addresses.json"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # fall back; will error clearly on read

SENDER_LOOKUP = _resolve_sender_lookup()
CACHE_FILE = REPO / "tmp" / "realized_revenue_cache.json"
UNKNOWN_LOG = REPO / "tmp" / "realized_revenue_unknown_senders.json"
# When deployed under /opt etc., persist cache somewhere writable instead of repo/tmp.
if not str(REPO).startswith(("/opt", "/usr", "/var")):
    pass  # repo-mode (developer machine)
else:
    CACHE_FILE = Path("/var/lib/mm-collector/cache.json")
    UNKNOWN_LOG = Path("/var/lib/mm-collector/unknown_senders.json")

WALLET_EVM = (os.environ.get("WALLET_EVM") or "").lower()
WALLET_SOL = os.environ.get("WALLET_SOL") or ""
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS") or "30")
DASHBOARD_BASE = os.environ.get("DASHBOARD_BASE") or "https://dashboard.example.com"

if not WALLET_EVM:
    raise SystemExit("realized_revenue_collector: set WALLET_EVM in env (your payout wallet, lowercase 0x...)")

ETHERSCAN_KEY = os.environ.get("ETHERSCAN_KEY") or ""
SOLSCAN_KEY = os.environ.get("SOLSCAN_KEY") or ""
FORUM_SECRET = os.environ.get("FORUM_SECRET") or ""

# Etherscan V2 unified endpoint covers all EVM chains. chain id mapping per
# https://docs.etherscan.io/etherscan-v2/getting-started/v2-quickstart
EVM_CHAINS = {
    "ethereum": {"id": 1, "native": "ETH", "native_usd": 3500.0},
    "polygon":  {"id": 137, "native": "POL", "native_usd": 0.45},
    "arbitrum": {"id": 42161, "native": "ETH", "native_usd": 3500.0},
    "optimism": {"id": 10, "native": "ETH", "native_usd": 3500.0},
    "base":     {"id": 8453, "native": "ETH", "native_usd": 3500.0},
    "bsc":      {"id": 56, "native": "BNB", "native_usd": 600.0},
}

# Stablecoin-ish symbols treated as 1:1 USD
STABLES = {"USDC", "USDT", "USDC.E", "DAI", "BUSD"}

# Best-effort token USD fallbacks for non-stable receives (purely cosmetic;
# revenue is dominated by stables for this fleet).
TOKEN_USD_FALLBACK = {
    # Fill in the tokens your fleet actually receives. Values below are rough
    # 2026-04 spot prices and will drift — refresh periodically or wire in a
    # CoinGecko fetch.
    "ANYONE": 0.04,
    "MYST":   0.10,
    "POL":    0.45,
    "MATIC":  0.45,
    "BNB":    600.0,
    "ETH":    3500.0,
}

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("realized-revenue")


# -----------------------------------------------------------------------------
# Sender classification
# -----------------------------------------------------------------------------
def load_sender_lookup() -> dict:
    cfg = json.loads(SENDER_LOOKUP.read_text(encoding="utf-8"))
    # Build flat reverse map: (chain, lowercase from-address) -> service for
    # explicit per-chain matches, plus chain-agnostic flat for legacy
    # candidates_to_verify entries (treated as ethereum unless chains list narrows).
    by_sender_chain = {}     # (chain, addr_lower) -> service
    by_sender_any   = {}     # addr_lower -> service (chain-agnostic fallback)
    for service, meta in cfg.get("services", {}).items():
        for entry in meta.get("addresses", []) or []:
            addr = (entry.get("address") or "").lower()
            ch   = (entry.get("chain") or "").lower()
            if addr and ch:
                by_sender_chain[(ch, addr)] = service
        for addr in meta.get("candidates_to_verify", []) or []:
            by_sender_any[addr.lower()] = service

    # Exclusions
    excl = cfg.get("exclusions", {}) or {}
    self_transfers = {a.lower() for a in (excl.get("self_transfers") or [])}
    dex_routers = set()  # set of (chain, addr_lower)
    user_bridges = set()  # subset of dex_routers that are user-callable
                          # bridges (LiFi/ParaSwap) where token-heuristic is
                          # UNSAFE because the operator can self-bridge tokens
                          # through them. Receives via these routers
                          # demote to "unknown" instead of heuristic.
    for r in (excl.get("dex_routers") or []):
        ch = (r.get("chain") or "").lower()
        addr = (r.get("address") or "").lower()
        if ch and addr:
            dex_routers.add((ch, addr))
            if r.get("user_callable_bridge"):
                user_bridges.add((ch, addr))
    # Personal inbound (CEX withdrawals, wallet-to-wallet moves the operator made
    # himself, etc.). Hard skip — never count as revenue. Per-chain match;
    # entries are objects with chain+address like dex_routers.
    personal_inbound = set()  # set of (chain, addr_lower)
    for r in (excl.get("personal_inbound") or []):
        ch = (r.get("chain") or "").lower()
        addr = (r.get("address") or "").lower()
        if ch and addr:
            personal_inbound.add((ch, addr))
    # Spam-token matching is two-tier: exact (full equality) for short
    # ambiguous symbols, substring for shillware with embedded URLs/emoji.
    spam_exact = {s.upper() for s in (excl.get("spam_tokens_exact") or [])}
    spam_substr = [s.upper() for s in (excl.get("spam_tokens_substring") or [])]
    # Backward compat: if the old `spam_tokens` array still exists, treat
    # short tokens (<=4 chars, all alpha) as exact and the rest as substring.
    legacy = excl.get("spam_tokens") or []
    for s in legacy:
        u = s.upper()
        if len(u) <= 4 and u.isalpha():
            spam_exact.add(u)
        else:
            spam_substr.append(u)

    # Token-symbol heuristic
    token_heuristic = {
        sym.upper(): svc
        for sym, svc in (cfg.get("_token_service_heuristic") or {}).items()
        if not sym.startswith("_")
    }

    # Token-contract reverse map: chain -> {lowercase contract -> symbol}
    token_map = {}
    for chain, contracts in cfg.get("_token_contracts", {}).items():
        if chain.startswith("_"):
            continue
        token_map[chain] = {addr.lower(): sym for sym, addr in contracts.items()}

    return {
        "by_sender_chain": by_sender_chain,
        "by_sender_any": by_sender_any,
        "self_transfers": self_transfers,
        "dex_routers": dex_routers,
        "user_bridges": user_bridges,
        "personal_inbound": personal_inbound,
        "spam_exact": spam_exact,
        "spam_substr": spam_substr,
        "token_heuristic": token_heuristic,
        "tokens": token_map,
        "raw": cfg,
    }


def classify_or_skip(from_addr: str, chain: str, token: str, lookup: dict) -> tuple[str, bool, str]:
    """Returns (service, via_token_heuristic, skip_reason).
    If skip_reason is non-empty, caller MUST drop the entry (don't POST, don't log as unknown).
    Order: explicit address match -> exclusions -> token-symbol heuristic -> unknown.
    """
    f = (from_addr or "").lower()
    ch = (chain or "").lower()
    tk = (token or "").upper()

    # 1. Explicit per-chain address match wins
    svc = lookup["by_sender_chain"].get((ch, f))
    if svc:
        return svc, False, ""

    # 2. Chain-agnostic candidates_to_verify
    svc = lookup["by_sender_any"].get(f)
    if svc:
        return svc, False, ""

    # 3. Exclusions — order matters: self first, then personal_inbound (CEX
    # withdrawals etc., HARD skip even if token matches a service), then
    # routers (where token-heuristic still applies), then spam tokens.
    if f in lookup["self_transfers"]:
        return "skip", False, "self_transfer"
    if (ch, f) in lookup["personal_inbound"]:
        return "skip", False, "personal_inbound"
    if (ch, f) in lookup["dex_routers"]:
        # USER-CALLABLE bridges (LiFi/ParaSwap): never trust token-heuristic.
        # the operator can roundtrip his own tokens through these — token-symbol
        # match is NOT evidence of service revenue. Skip with "unknown" so
        # he can manually triage if needed.
        if (ch, f) in lookup["user_bridges"]:
            return "skip", False, "user_bridge_self_roundtrip_risk"
        # Other routers (RelayRouterV3, SwiftDest): if the operator hasn't sent
        # tokens to them outgoing (verified separately), token-heuristic is
        # reasonably safe. Fall through.
        heur = lookup["token_heuristic"].get(tk)
        if heur:
            return heur, True, ""
        return "skip", False, "dex_router"
    # Spam-token: exact match first (covers short ambiguous symbols
    # like AI/BIT/DOG that would substring-collide with legit tokens).
    if tk and tk in lookup["spam_exact"]:
        return "skip", False, "spam_token_exact"
    # Then substring match (covers shillware with embedded URLs/emoji).
    if tk:
        for spam in lookup["spam_substr"]:
            if spam in tk:
                return "skip", False, "spam_token_substr"

    # 4. Token-symbol heuristic (last resort before unknown)
    heur = lookup["token_heuristic"].get(tk)
    if heur:
        return heur, True, ""

    return "unknown", False, ""


# -----------------------------------------------------------------------------
# HTTP helper
# -----------------------------------------------------------------------------
def http_get_json(url: str, timeout: int = 25, headers: dict | None = None) -> dict | list | None:
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "mm-collector/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        log.warning("HTTP %s for %s: %s", e.code, url, e.reason)
        return None
    except urllib.error.URLError as e:
        log.warning("URL error for %s: %s", url, e)
        return None
    except json.JSONDecodeError as e:
        log.warning("JSON decode error for %s: %s", url, e)
        return None


def http_post_json(url: str, payload: dict, timeout: int = 25, headers: dict | None = None) -> tuple[int, dict | None]:
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")
            try:
                return r.status, json.loads(txt)
            except json.JSONDecodeError:
                return r.status, None
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", errors="replace")
            return e.code, json.loads(txt) if txt else None
        except Exception:
            return e.code, None
    except urllib.error.URLError as e:
        log.warning("POST URL error: %s", e)
        return 0, None


# -----------------------------------------------------------------------------
# EVM (Etherscan V2 unified)
# -----------------------------------------------------------------------------
def etherscan_v2(chain_id: int, params: dict) -> list:
    """Etherscan V2 unified API. Returns [] on any error."""
    base = "https://api.etherscan.io/v2/api"
    p = dict(params)
    p["chainid"] = str(chain_id)
    if ETHERSCAN_KEY:
        p["apikey"] = ETHERSCAN_KEY
    qs = "&".join(f"{k}={v}" for k, v in p.items())
    url = f"{base}?{qs}"
    data = http_get_json(url)
    if not data:
        return []
    if str(data.get("status")) == "1" and isinstance(data.get("result"), list):
        return data["result"]
    # Etherscan returns status="0" + message="No transactions found" on empty
    msg = (data.get("message") or "").lower()
    if "no transactions" in msg or "no records" in msg:
        return []
    log.warning("etherscan v2 chain=%s message=%s", chain_id, data.get("message"))
    return []


def collect_evm_chain(chain_name: str, chain_meta: dict, lookup: dict, since_ts: int) -> list[dict]:
    """Pull token + native incoming transfers for one EVM chain."""
    out = []
    chain_id = chain_meta["id"]
    addr = WALLET_EVM

    # 1. Token transfers (ERC-20) into our wallet
    toks = etherscan_v2(chain_id, {
        "module": "account",
        "action": "tokentx",
        "address": addr,
        "startblock": "0",
        "endblock": "99999999",
        "sort": "desc",
    })
    # Be polite to Etherscan rate limits (free tier: 5 req/s).
    time.sleep(0.25)
    for t in toks:
        try:
            ts = int(t["timeStamp"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts < since_ts:
            continue
        if (t.get("to") or "").lower() != addr:
            continue  # outgoing
        try:
            decimals = int(t.get("tokenDecimal") or "18")
            raw = int(t.get("value") or "0")
            amount = raw / (10 ** decimals)
        except (ValueError, TypeError):
            continue
        sym = (t.get("tokenSymbol") or "").upper()
        if sym in STABLES:
            usd = amount
        else:
            usd = amount * TOKEN_USD_FALLBACK.get(sym, 0.0)
        from_addr = (t.get("from") or "").lower()
        service, via_heur, skip = classify_or_skip(from_addr, chain_name, sym, lookup)
        if skip:
            log.debug("[%s] skip token=%s from=%s reason=%s", chain_name, sym, from_addr[:12], skip)
            continue
        entry = {
            "tx_hash": t.get("hash") or "",
            "chain": chain_name,
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "from": from_addr,
            "to": addr,
            "amount": round(amount, 8),
            "token": sym,
            "service": service,
            "usd_at_time": round(usd, 4),
        }
        if via_heur:
            entry["via_token_heuristic"] = True
        out.append(entry)

    # 2. Native receives (ETH/POL/BNB)
    natives = etherscan_v2(chain_id, {
        "module": "account",
        "action": "txlist",
        "address": addr,
        "startblock": "0",
        "endblock": "99999999",
        "sort": "desc",
    })
    time.sleep(0.25)
    for tx in natives:
        try:
            ts = int(tx["timeStamp"])
        except (KeyError, ValueError, TypeError):
            continue
        if ts < since_ts:
            continue
        if (tx.get("to") or "").lower() != addr:
            continue
        try:
            wei = int(tx.get("value") or "0")
        except (ValueError, TypeError):
            continue
        if wei == 0:
            continue
        amount = wei / 1e18
        sym = chain_meta["native"]
        usd = amount * TOKEN_USD_FALLBACK.get(sym, chain_meta["native_usd"])
        from_addr = (tx.get("from") or "").lower()
        service, via_heur, skip = classify_or_skip(from_addr, chain_name, sym, lookup)
        if skip:
            log.debug("[%s] skip native=%s from=%s reason=%s", chain_name, sym, from_addr[:12], skip)
            continue
        entry = {
            "tx_hash": tx.get("hash") or "",
            "chain": chain_name,
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "from": from_addr,
            "to": addr,
            "amount": round(amount, 8),
            "token": sym,
            "service": service,
            "usd_at_time": round(usd, 4),
        }
        if via_heur:
            entry["via_token_heuristic"] = True
        out.append(entry)

    return out


# -----------------------------------------------------------------------------
# Solana
# -----------------------------------------------------------------------------
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def solana_rpc(method: str, params: list) -> dict | None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SOLANA_RPC,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "mm-collector/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning("solana rpc %s failed: %s", method, e)
        return None


def collect_solana(lookup: dict, since_ts: int) -> list[dict]:
    """Best-effort Solana SPL transfer pull. Falls back gracefully if RPC
    rate-limits us — the next hourly run will catch up. Out of scope:
    full per-tx parse of token amounts (requires getTransaction with
    jsonParsed encoding which is heavier). Phase 1 just records signatures
    so a future pass can backfill USD amounts; for now we mark amount=0
    and rely on Solscan-key mode for accurate values when key exists."""
    out = []
    if not WALLET_SOL:
        return out

    # Use Solscan Pro if we have a key — gives clean token-transfer view.
    if SOLSCAN_KEY:
        url = (
            "https://pro-api.solscan.io/v2.0/account/transfer"
            f"?address={WALLET_SOL}&page=1&page_size=100&sort_by=block_time&sort_order=desc"
        )
        data = http_get_json(url, headers={
            "User-Agent": "mm-collector/1.0",
            "token": SOLSCAN_KEY,
        })
        if data and isinstance(data, dict):
            for t in data.get("data", []) or []:
                ts = int(t.get("block_time") or 0)
                if ts < since_ts:
                    continue
                if (t.get("flow") or "").lower() != "in":
                    continue
                amt = float(t.get("amount") or 0) / (10 ** int(t.get("token_decimals") or 6))
                sym = (t.get("token_symbol") or "").upper()
                from_addr = t.get("from_address") or ""
                usd = amt if sym in STABLES else amt * TOKEN_USD_FALLBACK.get(sym, 0.0)
                service, via_heur, skip = classify_or_skip(from_addr, "solana", sym, lookup)
                if skip:
                    log.debug("[solana] skip token=%s from=%s reason=%s", sym, from_addr[:12], skip)
                    continue
                entry = {
                    "tx_hash": t.get("trans_id") or "",
                    "chain": "solana",
                    "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "from": from_addr,
                    "to": WALLET_SOL,
                    "amount": round(amt, 8),
                    "token": sym,
                    "service": service,
                    "usd_at_time": round(usd, 4),
                }
                if via_heur:
                    entry["via_token_heuristic"] = True
                out.append(entry)
            return out

    # Keyless fallback: pull recent signatures only (no per-tx parse to keep
    # public RPC happy). Tag amount=0 + token='SIGNATURE_ONLY' so server can
    # represent presence without false revenue.
    res = solana_rpc("getSignaturesForAddress", [WALLET_SOL, {"limit": 50}])
    if not res or not isinstance(res, dict):
        return out
    sigs = (res.get("result") or [])
    for s in sigs:
        ts = int(s.get("blockTime") or 0)
        if ts < since_ts or s.get("err"):
            continue
        out.append({
            "tx_hash": s.get("signature") or "",
            "chain": "solana",
            "ts": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "from": "",
            "to": WALLET_SOL,
            "amount": 0.0,
            "token": "SIGNATURE_ONLY",
            "service": "unknown",
            "usd_at_time": 0.0,
        })
    return out


# -----------------------------------------------------------------------------
# Cache + POST
# -----------------------------------------------------------------------------
def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("cache file corrupt; starting fresh")
    return {"posted_tx_hashes": [], "last_run": None}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def post_entry(entry: dict) -> bool:
    """POST one row to /api/realized-revenue. Returns True on 200."""
    if not FORUM_SECRET:
        log.error("FORUM_SECRET not set; cannot POST")
        return False
    url = f"{DASHBOARD_BASE.rstrip('/')}/api/realized-revenue"
    code, body = http_post_json(url, entry, headers={"X-Forum-Secret": FORUM_SECRET})
    if code == 200 and body and body.get("ok"):
        return True
    log.warning("POST %s -> %s body=%s", entry.get("tx_hash"), code, body)
    return False


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> int:
    lookup = load_sender_lookup()
    since_ts = int((datetime.now(tz=timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp())
    cache = load_cache()
    seen = set(cache.get("posted_tx_hashes") or [])

    all_entries: list[dict] = []

    for name, meta in EVM_CHAINS.items():
        if not ETHERSCAN_KEY:
            log.info("[%s] no ETHERSCAN_KEY — using Etherscan V2 keyless (rate-limited)", name)
        try:
            rows = collect_evm_chain(name, meta, lookup, since_ts)
            log.info("[%s] %d incoming transfer(s) since %s", name, len(rows),
                     datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat())
            all_entries.extend(rows)
        except Exception as e:
            log.error("[%s] collection failed: %s", name, e)

    try:
        sol_rows = collect_solana(lookup, since_ts)
        log.info("[solana] %d signature(s)/transfer(s) since lookback", len(sol_rows))
        all_entries.extend(sol_rows)
    except Exception as e:
        log.error("[solana] collection failed: %s", e)

    # Dedupe by (chain, tx_hash) — multiple chains can produce same hash str
    # very rarely. Keep latest by ts.
    seen_keys = set()
    deduped = []
    for e in sorted(all_entries, key=lambda x: x.get("ts") or "", reverse=True):
        key = (e.get("chain"), e.get("tx_hash"))
        if not key[1]:
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(e)

    log.info("Total deduped: %d entries", len(deduped))

    # Track unknowns (for manual triage)
    unknowns = [e for e in deduped if e.get("service") == "unknown" and e.get("token") != "SIGNATURE_ONLY"]
    if unknowns:
        UNKNOWN_LOG.parent.mkdir(parents=True, exist_ok=True)
        UNKNOWN_LOG.write_text(json.dumps(unknowns, indent=2), encoding="utf-8")
        log.info("Wrote %d unknown-sender entries to %s", len(unknowns), UNKNOWN_LOG)

    # POST only new ones
    new_count = 0
    posted_now = []
    for e in deduped:
        h = e.get("tx_hash")
        if not h or h in seen:
            continue
        if post_entry(e):
            new_count += 1
            posted_now.append(h)
            seen.add(h)

    cache["posted_tx_hashes"] = sorted(seen)
    cache["last_run"] = datetime.now(tz=timezone.utc).isoformat()
    save_cache(cache)

    log.info("Posted %d new entries (cache size: %d)", new_count, len(seen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
