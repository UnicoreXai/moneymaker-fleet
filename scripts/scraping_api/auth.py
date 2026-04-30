"""
auth.py — minimal API-key authentication + per-key rate limiting and monthly cap.

Keys live in `keys.json` (gitignored, mode 600 on disk). Format:

{
  "_comment": "API keys for the MM scraping API. Regenerate via mint_key().",
  "keys": {
    "<api_key>": {
      "label": "free | hobby | startup | pro | internal-test",
      "monthly_cap": 1000,
      "rate_per_minute": 10,
      "created_ts": 1714000000,
      "active": true
    }
  }
}

Counter state is held in-memory only (single-process MVP). When we add
multiple workers, swap this for a SQLite-backed counter (see usage.py TODO).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class KeyState:
    label: str
    monthly_cap: int
    rate_per_minute: int
    active: bool
    requests_this_month: int = 0
    last_reset_month: int = 0  # epoch month (year*12 + month-1)
    minute_window_start: float = 0.0
    minute_window_count: int = 0


@dataclass
class AuthBackend:
    keys_path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _state: dict[str, KeyState] = field(default_factory=dict)

    @classmethod
    def load(cls, keys_path: str | os.PathLike) -> "AuthBackend":
        p = Path(keys_path)
        if not p.exists():
            raise FileNotFoundError(
                f"auth.py: {p} not found. Run mint_key.py to seed an initial key."
            )
        data = json.loads(p.read_text())
        backend = cls(keys_path=p)
        for key, meta in data.get("keys", {}).items():
            if not meta.get("active", True):
                continue
            backend._state[key] = KeyState(
                label=meta.get("label", "unknown"),
                monthly_cap=int(meta.get("monthly_cap", 1000)),
                rate_per_minute=int(meta.get("rate_per_minute", 10)),
                active=True,
            )
        return backend

    @staticmethod
    def _epoch_month(ts: float) -> int:
        t = time.gmtime(ts)
        return t.tm_year * 12 + (t.tm_mon - 1)

    def check(self, api_key: Optional[str]) -> tuple[bool, str, Optional[KeyState]]:
        """Returns (ok, error_string, state). Increments counters on success."""
        if not api_key:
            return False, "missing_api_key", None
        with self._lock:
            state = self._state.get(api_key)
            if not state:
                return False, "invalid_api_key", None
            now = time.time()
            month = self._epoch_month(now)
            if month != state.last_reset_month:
                state.requests_this_month = 0
                state.last_reset_month = month
            if state.requests_this_month >= state.monthly_cap:
                return False, "monthly_cap_exceeded", state
            # rate limit: rolling 60-second window
            if now - state.minute_window_start >= 60:
                state.minute_window_start = now
                state.minute_window_count = 0
            if state.minute_window_count >= state.rate_per_minute:
                return False, "rate_limit_exceeded", state
            state.minute_window_count += 1
            state.requests_this_month += 1
            return True, "", state

    def usage(self, api_key: str) -> Optional[dict[str, object]]:
        with self._lock:
            state = self._state.get(api_key)
            if not state:
                return None
            now = time.time()
            return {
                "label": state.label,
                "monthly_cap": state.monthly_cap,
                "requests_this_month": state.requests_this_month,
                "rate_per_minute": state.rate_per_minute,
                "rate_used_in_window": state.minute_window_count,
                "rate_window_resets_in_s": max(0, int(60 - (now - state.minute_window_start))),
            }


def mint_key(label: str, monthly_cap: int, rate_per_minute: int) -> tuple[str, dict[str, object]]:
    """Generate a fresh API key and the metadata blob to drop into keys.json."""
    key = "mm_sk_" + secrets.token_urlsafe(24)
    meta = {
        "label": label,
        "monthly_cap": monthly_cap,
        "rate_per_minute": rate_per_minute,
        "created_ts": int(time.time()),
        "active": True,
    }
    return key, meta
