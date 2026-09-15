"""Provider-neutral authentication request quota boundary."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """One atomic quota outcome; capacity exhaustion admits only untracked keys."""

    allowed: bool
    retry_after: int = 0
    capacity_exceeded: bool = False


class RateLimitStore(Protocol):
    """Shared credential quota using only HMAC IP digest keys."""

    async def consume(self, key: str) -> RateLimitDecision:
        """Consume one of thirty requests in the first request's sixty-second window."""
        ...
