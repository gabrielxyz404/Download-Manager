"""Speed limiter shared by any number of connections."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Pacing limiter: ``reserve(n)`` returns how long to wait before using n bytes.

    ``rate`` is in bytes per second; 0 means unlimited. Many threads can share
    one limiter, so a global limit is split fairly among all connections.
    """

    def __init__(self, rate: float = 0):
        self.rate = rate
        self._lock = threading.Lock()
        self._next = time.monotonic()

    def reserve(self, nbytes: int) -> float:
        if self.rate <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + nbytes / self.rate
            return self._next - now


GLOBAL_LIMITER = RateLimiter(0)
