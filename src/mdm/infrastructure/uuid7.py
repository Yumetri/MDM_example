"""Local UUIDv7 generation for application-created change-set identifiers."""

import secrets
import time
from collections.abc import Callable
from uuid import UUID


class Uuid7Generator:
    """Generate RFC 9562 UUIDv7 values without a framework dependency."""

    def __init__(
        self,
        *,
        millisecond_clock: Callable[[], int] | None = None,
        random_bits: Callable[[int], int] = secrets.randbits,
    ) -> None:
        self._millisecond_clock = millisecond_clock or _unix_milliseconds
        self._random_bits = random_bits

    def new(self) -> UUID:
        """Return a time-ordered UUID with random RFC variant payload bits."""
        timestamp = self._millisecond_clock()
        if not 0 <= timestamp < (1 << 48):
            raise ValueError("UUIDv7 timestamp is outside its 48-bit range")

        random_a = self._random_bits(12)
        random_b = self._random_bits(62)
        if not 0 <= random_a < (1 << 12) or not 0 <= random_b < (1 << 62):
            raise ValueError("UUIDv7 random source returned an out-of-range value")

        value = (timestamp << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
        return UUID(int=value)


def _unix_milliseconds() -> int:
    return time.time_ns() // 1_000_000
