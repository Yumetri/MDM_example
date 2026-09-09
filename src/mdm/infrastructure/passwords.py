"""Bounded Argon2id password hashing outside the event loop."""

import asyncio
from collections.abc import Callable
from typing import Protocol, TypeVar

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from mdm.application.auth import InvalidStoredPasswordHash, PasswordHashUnavailable
from mdm.domain.auth import PlainPassword
from mdm.infrastructure.settings import Settings

ResultT = TypeVar("ResultT")


class SyncPasswordBackend(Protocol):
    """Synchronous CPU-bound functions delegated to worker threads."""

    def hash(self, password: str) -> str:
        """Return an encoded password hash."""
        ...

    def verify(self, encoded_hash: str, password: str) -> bool:
        """Verify a password against an encoded hash."""
        ...


class _Argon2Backend:
    def __init__(self, *, memory_mib: int, iterations: int, parallelism: int) -> None:
        self._hasher = Argon2Hasher(
            memory_cost=memory_mib * 1024,
            time_cost=iterations,
            parallelism=parallelism,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, encoded_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(encoded_hash, password)
        except VerifyMismatchError:
            return False
        except (InvalidHashError, VerificationError):
            raise InvalidStoredPasswordHash("stored password hash is invalid") from None


class BoundedArgon2PasswordHasher:
    """Run Argon2id in worker threads guarded by an async concurrency limit."""

    def __init__(
        self,
        *,
        memory_mib: int = 19,
        iterations: int = 2,
        parallelism: int = 1,
        max_concurrency: int = 2,
        acquire_timeout_seconds: float = 2.0,
        backend: SyncPasswordBackend | None = None,
    ) -> None:
        if memory_mib < 1 or iterations < 1 or parallelism < 1:
            raise ValueError("Argon2 parameters must be positive")
        if not 1 <= max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        if not 0.1 <= acquire_timeout_seconds <= 10:
            raise ValueError("acquire_timeout_seconds must be between 0.1 and 10")
        self._backend = backend or _Argon2Backend(
            memory_mib=memory_mib,
            iterations=iterations,
            parallelism=parallelism,
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._acquire_timeout_seconds = acquire_timeout_seconds

    async def hash(self, password: PlainPassword) -> str:
        return await self._run(self._backend.hash, password.reveal())

    async def verify(self, password: PlainPassword, encoded_hash: str) -> bool:
        return await self._run(self._backend.verify, encoded_hash, password.reveal())

    async def _run(self, function: Callable[..., ResultT], *args: object) -> ResultT:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._acquire_timeout_seconds,
            )
        except TimeoutError:
            raise PasswordHashUnavailable("password hashing capacity is unavailable") from None

        work = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
            work.result()
            raise
        finally:
            self._semaphore.release()


def build_password_hasher(settings: Settings) -> BoundedArgon2PasswordHasher:
    """Build the adapter exclusively from validated typed settings."""
    return BoundedArgon2PasswordHasher(
        memory_mib=settings.auth_password_hash_memory_mib,
        iterations=settings.auth_password_hash_iterations,
        parallelism=settings.auth_password_hash_parallelism,
        max_concurrency=settings.auth_password_hash_max_concurrency,
        acquire_timeout_seconds=settings.auth_password_hash_acquire_timeout_seconds,
    )
