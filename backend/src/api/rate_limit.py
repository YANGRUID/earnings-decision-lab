"""Minimal in-memory rate limiter for the one endpoint that spends real
money per call (/research/query, which runs several LLM calls). Global,
not per-client — this is a single-developer personal research tool with
no auth layer (see docs/engineering_decisions.md for why that's an
intentional, defensible scope choice), so protecting against runaway cost
matters more than protecting against any specific abusive client.

Not a distributed rate limiter (no Redis) — a single-process in-memory
sliding window is honest about this project's actual deployment shape.
"""

import time
from collections import deque


class SlidingWindowRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > self._window_seconds:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max_requests:
            return False
        self._timestamps.append(now)
        return True
