import asyncio
import threading

import pytest
from argon2 import extract_parameters

from mdm.application.auth import InvalidStoredPasswordHash, PasswordHashUnavailable
from mdm.domain.auth import PlainPassword
from mdm.infrastructure.passwords import BoundedArgon2PasswordHasher, build_password_hasher
from mdm.infrastructure.settings import Settings


class BlockingBackend:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.hash_calls = 0

    def hash(self, password: str) -> str:
        del password
        self.hash_calls += 1
        self.started.set()
        if not self.release.wait(timeout=2):
            raise AssertionError("test backend was not released")
        return "encoded"

    def verify(self, encoded_hash: str, password: str) -> bool:
        del encoded_hash, password
        return True


async def _wait_until_set(event: threading.Event) -> None:
    for _ in range(100):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("worker did not start")


@pytest.mark.unit
async def test_argon2_uses_the_contract_parameters() -> None:
    hasher = BoundedArgon2PasswordHasher()
    password = PlainPassword("correct horse battery staple")

    encoded = await hasher.hash(password)
    parameters = extract_parameters(encoded)

    assert parameters.memory_cost == 19 * 1024
    assert parameters.time_cost == 2
    assert parameters.parallelism == 1
    assert await hasher.verify(password, encoded) is True
    assert await hasher.verify(PlainPassword("wrong password value"), encoded) is False


@pytest.mark.unit
async def test_argon2_adapter_is_built_from_typed_settings() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://mdm:secret@localhost:5432/mdm",
        auth_password_hash_memory_mib=20,
        auth_password_hash_iterations=3,
        auth_password_hash_parallelism=2,
        auth_password_hash_max_concurrency=1,
        auth_password_hash_acquire_timeout_seconds=0.5,
        _env_file=None,
    )

    encoded = await build_password_hasher(settings).hash(
        PlainPassword("correct horse battery staple")
    )
    parameters = extract_parameters(encoded)

    assert parameters.memory_cost == 20 * 1024
    assert parameters.time_cost == 3
    assert parameters.parallelism == 2


@pytest.mark.unit
async def test_hashing_does_not_block_the_event_loop_and_limits_concurrency() -> None:
    backend = BlockingBackend()
    hasher = BoundedArgon2PasswordHasher(
        backend=backend,
        max_concurrency=1,
        acquire_timeout_seconds=0.1,
    )
    first = asyncio.create_task(hasher.hash(PlainPassword("first password value")))
    await _wait_until_set(backend.started)

    await asyncio.sleep(0)
    assert not first.done()
    with pytest.raises(PasswordHashUnavailable):
        await hasher.hash(PlainPassword("second password value"))

    backend.release.set()
    assert await first == "encoded"


@pytest.mark.unit
async def test_cancellation_before_slot_acquisition_does_not_start_hash() -> None:
    backend = BlockingBackend()
    hasher = BoundedArgon2PasswordHasher(
        backend=backend,
        max_concurrency=1,
        acquire_timeout_seconds=1,
    )
    first = asyncio.create_task(hasher.hash(PlainPassword("first password value")))
    await _wait_until_set(backend.started)
    waiting = asyncio.create_task(hasher.hash(PlainPassword("second password value")))
    await asyncio.sleep(0)

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    backend.release.set()
    await first

    assert backend.hash_calls == 1


@pytest.mark.unit
async def test_cancellation_after_hash_starts_keeps_the_slot_until_completion() -> None:
    backend = BlockingBackend()
    hasher = BoundedArgon2PasswordHasher(backend=backend, max_concurrency=1)
    task = asyncio.create_task(hasher.hash(PlainPassword("first password value")))
    await _wait_until_set(backend.started)

    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()

    backend.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.unit
async def test_repeated_cancellation_cannot_release_a_running_hash_slot() -> None:
    backend = BlockingBackend()
    hasher = BoundedArgon2PasswordHasher(backend=backend, max_concurrency=1)
    first = asyncio.create_task(hasher.hash(PlainPassword("first password value")))
    await _wait_until_set(backend.started)

    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    second = asyncio.create_task(hasher.hash(PlainPassword("second password value")))
    try:
        await asyncio.sleep(0.01)
        assert backend.hash_calls == 1
    finally:
        backend.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second


@pytest.mark.unit
async def test_invalid_stored_hash_raises_sanitized_error() -> None:
    hasher = BoundedArgon2PasswordHasher()

    with pytest.raises(InvalidStoredPasswordHash) as raised:
        await hasher.verify(PlainPassword("correct horse battery staple"), "secret-broken-hash")

    assert "secret-broken-hash" not in str(raised.value)
