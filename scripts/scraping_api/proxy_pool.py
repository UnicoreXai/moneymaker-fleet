"""
proxy_pool.py — least-recently-used selection across the residential proxy fleet.

Loads `fleet_proxies.json` (gitignored) and rotates through the per-node
residential proxy URLs. Pure stdlib + threading, no extra deps.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class ProxySlot:
    name: str            # e.g. "node-25"
    proxy_url: str       # http://user:pass@residential_ip:port
    host_ip: str         # public IP of the node hosting this proxy (for ops)
    last_used_ts: float = 0.0
    in_flight: int = 0
    fail_count: int = 0
    cooldown_until: float = 0.0  # epoch seconds; 0 = available


@dataclass
class ProxyPool:
    slots: list[ProxySlot] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    cooldown_after_failures: int = 3
    cooldown_seconds: int = 60

    @classmethod
    def from_file(cls, path: str | os.PathLike) -> "ProxyPool":
        data = json.loads(Path(path).read_text())
        slots: list[ProxySlot] = []
        for name, entry in data.get("proxies", {}).items():
            slots.append(
                ProxySlot(
                    name=name,
                    proxy_url=entry["proxy_url"],
                    host_ip=entry.get("host_ip", ""),
                )
            )
        if not slots:
            raise RuntimeError(
                f"proxy_pool: no usable proxies in {path}. "
                "Run scripts/scraping_api/regen_fleet_proxies.py."
            )
        return cls(slots=slots)

    def size(self) -> int:
        return len(self.slots)

    def acquire(self, prefer_country: Optional[str] = None) -> ProxySlot:
        """Pick the LRU available slot. prefer_country is currently ignored;
        a future revision can map slots to country once you buy proxy
        blocks from multiple regions."""
        now = time.time()
        with self._lock:
            available = [s for s in self.slots if s.cooldown_until <= now]
            if not available:
                # all cooling down — pick the one cooling down soonest
                available = sorted(self.slots, key=lambda s: s.cooldown_until)[:1]
            # LRU first, ties broken by lower in_flight
            available.sort(key=lambda s: (s.last_used_ts, s.in_flight))
            slot = available[0]
            slot.last_used_ts = now
            slot.in_flight += 1
            return slot

    def release(self, slot: ProxySlot, ok: bool) -> None:
        with self._lock:
            slot.in_flight = max(0, slot.in_flight - 1)
            if ok:
                slot.fail_count = 0
                slot.cooldown_until = 0
            else:
                slot.fail_count += 1
                if slot.fail_count >= self.cooldown_after_failures:
                    slot.cooldown_until = time.time() + self.cooldown_seconds
                    slot.fail_count = 0

    def stats(self) -> dict[str, object]:
        with self._lock:
            now = time.time()
            return {
                "total": len(self.slots),
                "available": sum(1 for s in self.slots if s.cooldown_until <= now),
                "in_flight_total": sum(s.in_flight for s in self.slots),
                "by_node": [
                    {
                        "name": s.name,
                        "host_ip": s.host_ip,
                        "in_flight": s.in_flight,
                        "fail_count": s.fail_count,
                        "cooldown_remaining_s": max(0, int(s.cooldown_until - now)),
                        "last_used_age_s": int(now - s.last_used_ts) if s.last_used_ts else None,
                    }
                    for s in self.slots
                ],
            }
