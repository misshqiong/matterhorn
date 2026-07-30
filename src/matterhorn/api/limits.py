"""Small process-local fixed-window limits for expensive Console writes."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from threading import Lock

from matterhorn.errors import RateLimitExceededError


class MemoryRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ):
        if limit < 1:
            raise ValueError("rate limit MUST be positive")
        if window_seconds <= 0:
            raise ValueError("rate-limit window MUST be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self.clock = clock
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = self.clock()
        cutoff = now - self.window_seconds
        with self._lock:
            calls = self._calls[key]
            while calls and calls[0] <= cutoff:
                calls.popleft()
            if len(calls) >= self.limit:
                raise RateLimitExceededError(
                    "Too many requests; retry after the rate-limit window."
                )
            calls.append(now)
