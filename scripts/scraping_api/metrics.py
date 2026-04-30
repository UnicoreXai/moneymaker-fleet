"""
metrics.py — append-only JSONL request log. One line per scrape attempt.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MetricsLogger:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _started: float = field(default_factory=time.time)
    _request_count: int = 0

    @classmethod
    def open(cls, path: str | os.PathLike) -> "MetricsLogger":
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so disk perms are correct from first write.
        if not p.exists():
            p.touch()
            try:
                os.chmod(p, 0o640)
            except Exception:  # noqa: BLE001
                pass
        return cls(path=p)

    def log(
        self,
        *,
        request_id: str,
        api_key: str,
        url: str,
        proxy_node: str,
        proxy_egress_ip: str,
        status_code: int,
        latency_ms: int,
        error: str = "",
    ) -> None:
        record = {
            "ts": int(time.time()),
            "request_id": request_id,
            "api_key_prefix": api_key[:14] if api_key else "",
            "url": url[:500],
            "proxy_node": proxy_node,
            "proxy_egress_ip": proxy_egress_ip,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error": error[:200] if error else "",
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            self._request_count += 1
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line)

    def uptime_s(self) -> int:
        return int(time.time() - self._started)

    def total_requests(self) -> int:
        return self._request_count
