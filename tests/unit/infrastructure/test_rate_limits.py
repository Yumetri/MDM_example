import asyncio
import hashlib

import pytest

from mdm.infrastructure.rate_limits import InMemoryRateLimitStore


def key(value: int) -> str:
    return "mdm:rate-limit:auth:" + hashlib.sha256(str(value).encode()).hexdigest()


@pytest.mark.unit
async def test_concurrent_requests_allow_exactly_thirty_in_one_fixed_window() -> None:
    now = 10.0
    store = InMemoryRateLimitStore(clock=lambda: now)
    decisions = await asyncio.gather(*(store.consume(key(1)) for _ in range(31)))

    assert sum(item.allowed for item in decisions) == 30
    assert [item.retry_after for item in decisions if not item.allowed] == [60]
    now = 69.1
    assert (await store.consume(key(1))).retry_after == 1
    now = 70.0
    assert (await store.consume(key(1))).allowed


@pytest.mark.unit
async def test_capacity_fail_open_preserves_existing_key_limits_and_reclaims_expiry() -> None:
    now = 0.0
    store = InMemoryRateLimitStore(clock=lambda: now, capacity=2)
    for _ in range(30):
        await store.consume(key(1))
    now = 1.0
    await store.consume(key(2))

    overflow = await store.consume(key(3))
    assert overflow.allowed and overflow.capacity_exceeded
    assert not (await store.consume(key(1))).allowed
    assert store.entry_count == 2

    now = 60.0
    admitted = await store.consume(key(3))
    assert admitted.allowed and not admitted.capacity_exceeded
    assert store.entry_count == 2
    assert (await store.consume(key(2))).allowed


@pytest.mark.unit
async def test_default_hard_cap_is_one_hundred_thousand() -> None:
    store = InMemoryRateLimitStore(clock=lambda: 0.0)
    for index in range(100_000):
        result = await store.consume(key(index))
        assert result.allowed and not result.capacity_exceeded
    assert (await store.consume(key(100_000))).capacity_exceeded
    assert store.entry_count == 100_000


@pytest.mark.unit
async def test_expiry_cleanup_is_bounded_and_repeated_requests_reclaim_remaining_entries() -> None:
    now = 0.0
    store = InMemoryRateLimitStore(clock=lambda: now, capacity=1_000)
    for index in range(1_000):
        await store.consume(key(index))
    now = 60.0
    await store.consume(key(1_000))
    assert 1 < store.entry_count < 1_000
    for index in range(1_001, 1_010):
        await store.consume(key(index))
    assert store.entry_count == 10


@pytest.mark.unit
async def test_store_rejects_raw_ip_or_arbitrary_keys() -> None:
    store = InMemoryRateLimitStore()
    with pytest.raises(ValueError, match="digest"):
        await store.consume("192.0.2.1")


@pytest.mark.unit
async def test_new_store_starts_with_empty_quota() -> None:
    original = InMemoryRateLimitStore(clock=lambda: 0.0)
    for _ in range(30):
        await original.consume(key(1))
    assert not (await original.consume(key(1))).allowed
    restarted = InMemoryRateLimitStore(clock=lambda: 0.0)
    assert (await restarted.consume(key(1))).allowed
