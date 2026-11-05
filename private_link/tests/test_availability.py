"""Availability regressions using only simulated TLS streams and temporary files."""

import asyncio
import sqlite3

import pytest
from test_adapter import wire
from test_transport import MockTls, Writer, service

from private_link.adapter import Adapter, Config, decode_packet, encode


@pytest.mark.asyncio
async def test_rejected_then_valid_envelopes_keep_same_authenticated_session(caplog):
    caplog.set_level("INFO", logger="private_link.transport")
    s = service()
    calls = []
    delivered = asyncio.Event()

    async def planner(request):
        calls.append(request)
        delivered.set()

    adapter = Adapter(
        Config("local", enabled=True, inbound={("peer", "rf"): "rf"}),
        planner,
        s.transports,
        clock=lambda: 100,
    )
    s.receiver = adapter.peer_received
    raw = wire()
    good = dict(
        version=1,
        origin="peer",
        radio="rf",
        packet_id=decode_packet(raw)[2],
        issued_ms=100000,
        expires_ms=115000,
        hops=1,
        width=2,
        raw=raw.hex(),
    )
    rejected = [
        dict(good, issued_ms=80000, expires_ms=95000),
        dict(good, issued_ms=103000, expires_ms=118000),
        dict(good, radio="wrong"),
        dict(good, hops=2),
        dict(good, expires_ms=200000),
    ]
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    task = asyncio.create_task(s._accept(reader, writer))
    try:
        reader.feed_data(b"".join(encode(e) for e in [*rejected, good]))
        await asyncio.wait_for(delivered.wait(), 0.5)
        assert len(calls) == 1
        assert s.transports["peer"].owns(writer) and not task.done()
        assert s.failures == 0
        assert s.rejections == {"time": 2, "route": 1, "hop_policy": 1, "lifetime": 1}
        assert "Private peer connected: peer" in caplog.text
        assert "Private packet rejected: time" in caplog.text
        assert raw.hex() not in caplog.text and good["packet_id"] not in caplog.text
    finally:
        reader.feed_eof()
        await task
        await s.close()


@pytest.mark.asyncio
async def test_sqlite_lock_is_irrelevant_to_default_daemon_cache(tmp_path, monkeypatch):
    from test_daemon import cleanup, setup

    from private_link.daemon import install_from_config

    path = tmp_path / "test-only.sqlite"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("CREATE TABLE test_only(value)")
    db.execute("BEGIN IMMEDIATE")
    rig, daemon, transport = await setup()
    try:
        await daemon.private_island.close()
        daemon.config["private_island"].update(
            replay_path=str(path), initialize_replay=True
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("Replay must not open SQLite")

        monkeypatch.setattr(sqlite3, "connect", forbidden)
        daemon.private_island = await install_from_config(
            daemon, transports={"remote": transport}
        )
        store = daemon.private_island.owned_replay
        assert await store.reserve("a" * 64, 115000, 100000)
        db.execute("ROLLBACK")
        assert await store.reserve("b" * 64, 115000, 100000)
        assert store.healthy
    finally:
        db.close()
        await cleanup(daemon)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "origin", "programming"])
async def test_corrupt_or_untrusted_or_unexpected_failure_closes_session(failure):
    s = service()

    async def planner(_):
        raise RuntimeError("programming error must remain fatal")

    adapter = Adapter(
        Config("local", enabled=True, inbound={("peer", "rf"): "rf"}),
        planner,
        s.transports,
        clock=lambda: 100,
    )
    s.receiver = adapter.peer_received
    raw = wire()
    envelope = dict(
        version=1,
        origin="peer",
        radio="rf",
        packet_id=decode_packet(raw)[2],
        issued_ms=100000,
        expires_ms=115000,
        hops=1,
        width=2,
        raw=raw.hex(),
    )
    if failure == "origin":
        envelope["origin"] = "impostor"
    data = b"\x00\x00\x00\x02{}" if failure == "malformed" else encode(envelope)
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    reader.feed_data(data)
    await asyncio.wait_for(s._accept(reader, writer), 0.5)
    assert writer.closed and s.failures == 1
    assert not s.rejections
    await s.close()


@pytest.mark.asyncio
async def test_adapter_loop_cache_uses_monotonic_time_across_wall_correction():
    from test_adapter import pair

    a, _, transport, _, calls, wall = pair(max_seen=1)
    tick = [10.0]
    a.monotonic_clock = lambda: tick[0]
    raw = wire()
    assert (await a.radio_received(raw, "rf"))[1] == "transport_accepted"
    wall[0] += 9000
    assert (await a.radio_received(raw, "rf"))[1] == "duplicate_or_full"
    wall[0] -= 9000
    assert (await a.radio_received(wire(text=b"new"), "rf"))[1] == "duplicate_or_full"
    tick[0] += 15
    assert (await a.radio_received(raw, "rf"))[1] == "transport_accepted"
    assert len(calls["a"]) == 4 and len(transport.frames) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("ingress", ["hardware", "peer"])
@pytest.mark.parametrize("correction_at", [2, 3, 4])
async def test_admission_clock_correction_keeps_both_caches_bounded(
    ingress, correction_at
):
    from test_adapter import pair
    from private_link.replay import MemoryReplayStore

    a, _, transport, _, calls, _ = pair(max_seen=1)
    tick = [10.0]
    reads = 0

    def correcting_wall():
        nonlocal reads
        reads += 1
        return 9100.0 if reads < correction_at else 100.0

    a.clock = correcting_wall
    a.monotonic_clock = lambda: tick[0]
    a.replay_store = MemoryReplayStore(capacity=1, clock=lambda: tick[0])

    async def receive(raw, issued):
        if ingress == "hardware":
            return (await a.radio_received(raw, "rf"))[1]
        return await a.peer_received(
            encode(
                dict(
                    version=1,
                    origin="b",
                    radio="rf",
                    packet_id=decode_packet(raw)[2],
                    issued_ms=issued,
                    expires_ms=issued + 15000,
                    hops=1,
                    width=2,
                    raw=raw.hex(),
                )
            ),
            transport,
        )

    first = await receive(wire(), 9100000)
    assert max(a._seen.values()) - tick[0] <= 15
    expected = "transport_accepted" if ingress == "hardware" else "rf_planner_accepted"
    assert first == expected
    assert max(a.replay_store._seen.values()) - tick[0] <= 15
    a.clock = lambda: 100.0
    tick[0] += 16
    assert await receive(wire(text=b"fresh"), 100000) == expected
    assert len(calls["a"]) == 2
    assert len(transport.frames) == (2 if ingress == "hardware" else 0)
