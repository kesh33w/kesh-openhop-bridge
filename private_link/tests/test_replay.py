import asyncio

import pytest

from private_link.replay import MemoryReplayStore


def digest(number):
    return f"{number:064x}"


@pytest.mark.asyncio
async def test_duplicate_capacity_expiry_and_restart():
    tick = [10.0]
    store = MemoryReplayStore(capacity=1, clock=lambda: tick[0])
    assert await store.reserve(digest(1), 115000, 100000)
    assert not await store.reserve(digest(1), 115000, 100000)
    assert store.last_rejection == "duplicate"
    assert not await store.reserve(digest(2), 115000, 100000)
    assert store.last_rejection == "full"
    tick[0] = 24.999
    assert not await store.reserve(digest(2), 115000, 100000)
    tick[0] = 25
    assert await store.reserve(digest(2), 115000, 100000)
    await store.close()
    assert not await store.reserve(digest(3), 115000, 100000)
    assert store.last_rejection == "closed" and not store.healthy
    await store.close()
    restarted = MemoryReplayStore(capacity=1)
    assert await restarted.reserve(digest(2), 115000, 100000)


@pytest.mark.asyncio
async def test_wall_clock_jump_and_correction_do_not_expire_or_latch_cache():
    tick = [0.0]
    store = MemoryReplayStore(clock=lambda: tick[0])
    assert await store.reserve(digest(1), 115000, 100000)
    assert await store.reserve(digest(2), 9015000, 9000000)
    assert not await store.reserve(digest(1), 9015000, 9000000)
    assert await store.reserve(digest(3), 115000, 100000)
    tick[0] = 15
    assert await store.reserve(digest(1), 115000, 100000)


@pytest.mark.asyncio
async def test_atomic_concurrent_reservation_has_one_winner():
    store = MemoryReplayStore()
    results = await asyncio.gather(
        *(store.reserve(digest(1), 115000, 100000) for _ in range(12))
    )
    assert results.count(True) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "packet,expiry,now",
    [
        ("bad", 115000, 100000),
        (digest(1), True, 100000),
        (digest(1), 100000, 100000),
        (digest(1), 162001, 100000),
        (digest(1), 115000, -1),
        (digest(1), 1 << 63, (1 << 63) - 1),
    ],
)
async def test_bad_input_never_reserves(packet, expiry, now):
    store = MemoryReplayStore()
    assert not await store.reserve(packet, expiry, now)
    assert store.healthy and store.last_rejection == "invalid"


@pytest.mark.parametrize(
    "options",
    [
        {"capacity": 0},
        {"capacity": 10001},
        {"capacity": True},
        {"max_ttl_ms": 99},
        {"max_ttl_ms": 62001},
    ],
)
def test_invalid_bounds(options):
    with pytest.raises(ValueError):
        MemoryReplayStore(**options)


@pytest.mark.asyncio
async def test_retention_accepts_clock_skew_bound():
    store = MemoryReplayStore()
    assert await store.reserve(digest(1), 162000, 100000)
    assert not await store.reserve(digest(2), 162001, 100000)
