"""In-memory sliding-window rate limiter for authentication and public forms.

Limitation (documented): counters live in this server process; several processes or a restart reset
them. A production deployment would use a shared store (e.g. Redis) or a gateway limit.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()
        self._clock = clock

    def hit(self, key: str, limit: int, window_s: float) -> tuple[bool, float]:
        """Record an attempt. Returns (allowed, retry_after_seconds)."""
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= window_s:
                q.popleft()
            if len(q) >= limit:
                return False, max(1.0, window_s - (now - q[0]))
            q.append(now)
            return True, 0.0

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
