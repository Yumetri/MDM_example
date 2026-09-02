"""Application-level service readiness behavior."""

from typing import Protocol


class ReadinessUnavailable(RuntimeError):
    """Raised when a dependency required to serve traffic is unavailable."""


class DatabaseProbe(Protocol):
    """Boundary used to verify that the primary data store is reachable."""

    async def ping(self) -> None:
        """Raise ReadinessUnavailable when the data store cannot be reached."""


class ReadinessCheck(Protocol):
    """Boundary consumed by the API readiness endpoint."""

    async def execute(self) -> None:
        """Raise ReadinessUnavailable when the service is not ready."""


class CheckReadiness:
    """Check all dependencies required before accepting traffic."""

    def __init__(self, database: DatabaseProbe) -> None:
        self._database = database

    async def execute(self) -> None:
        await self._database.ping()
