"""Bounded, process-local, atomic fixed-window authentication quotas."""

import asyncio
import math
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from mdm.application.rate_limits import RateLimitDecision

_KEY_PATTERN = re.compile(r"mdm:rate-limit:auth:[0-9a-f]{64}")
_CLEANUP_BATCH = 256


@dataclass(slots=True)
class _Window:
    expires_at: float
    count: int


class InMemoryRateLimitStore:
    """One instance per event loop/process, replaced before multi-worker deployment."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = 100_000,
    ) -> None:
        if capacity < 1:
            raise ValueError("rate limit capacity must be positive")
        self._clock = clock
        self._capacity = capacity
        self._lock = asyncio.Lock()
        # Fixed TTL and monotonic insertion times keep entries in expiry order.
        self._windows: OrderedDict[str, _Window] = OrderedDict()

    @property
    def entry_count(self) -> int:
        """Expose capacity usage without revealing any stored keys."""
        return len(self._windows)

    async def consume(self, key: str) -> RateLimitDecision:
        """Check and update one digest quota without awaiting inside the critical section."""
        if _KEY_PATTERN.fullmatch(key) is None:
            raise ValueError("rate limit key must contain only the namespaced IP digest")
        async with self._lock:
            now = self._clock()
            for _ in range(_CLEANUP_BATCH):
                if not self._windows:
                    break
                oldest = next(iter(self._windows.values()))
                if oldest.expires_at > now:
                    break
                self._windows.popitem(last=False)

            window = self._windows.get(key)
            if window is not None and window.expires_at <= now:
                del self._windows[key]
                window = None
            if window is not None:
                if window.count >= 30:
                    return RateLimitDecision(
                        allowed=False,
                        retry_after=max(1, math.ceil(window.expires_at - now)),
                    )
                window.count += 1
                return RateLimitDecision(allowed=True)
            if len(self._windows) >= self._capacity:
                return RateLimitDecision(allowed=True, capacity_exceeded=True)
            self._windows[key] = _Window(expires_at=now + 60.0, count=1)
            return RateLimitDecision(allowed=True)
