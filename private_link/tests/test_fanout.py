import asyncio

import pytest

from private_link.adapter import Adapter, Config, decode
from private_link.tests.test_adapter import TestOnlyTransport, wire


def setup(**bounds):
    now = [100.0]
    calls = []
    peers = {name: TestOnlyTransport(name) for name in ("b", "c", "d")}

    async def local(request):
        calls.append(request)
        return "local"

    adapter = Adapter(
        Config(
            "a",
            enabled=True,
            outbound={"rf": list(peers)},
            inbound={("b", "rf"): "rf"},
            **bounds,
        ),
        local,
        peers,
        lambda: now[0],
    )
    return adapter, peers, calls, now


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [2, 3])
async def test_each_destination_gets_same_packet_once_and_rf_echo_stays_local(width):
    a, peers, calls, _ = setup()
    raw = wire(width, hops=0)
    local, outcomes = await a.radio_received(raw, "rf")
    assert local == "local"
    assert outcomes == {p: "transport_accepted" for p in peers}
    frames = [t.frames[0] for t in peers.values()]
    assert len(set(frames)) == 1
    assert decode(frames[0])["width"] == width
    assert decode(frames[0])["origin"] == "a"
    # Return RF has an altered path, but unchanged payload/packet identity.
    assert (await a.radio_received(wire(width, hops=2), "rf"))[1] == "duplicate_or_full"
    assert len(calls) == 2 and all(len(t.frames) == 1 for t in peers.values())


@pytest.mark.asyncio
async def test_one_byte_runs_rf_without_any_internet_copy():
    a, peers, calls, _ = setup()
    assert await a.radio_received(wire(1), "rf") == ("local", "ineligible")
    assert len(calls) == 1 and all(not t.frames for t in peers.values())


@pytest.mark.asyncio
async def test_slow_and_offline_peers_do_not_block_fast_peer_or_later_rf():
    a, peers, calls, _ = setup(max_inflight=1, send_timeout=0.05)
    peers["b"].block = True
    peers["d"].up = False
    task = asyncio.create_task(a.radio_received(wire(), "rf"))
    for _ in range(100):
        if peers["c"].frames:
            break
        await asyncio.sleep(0)
    assert peers["c"].frames and not task.done() and len(calls) == 1
    _, second = await a.radio_received(wire(text=b"second"), "rf")
    assert second == {"b": "busy", "c": "transport_accepted", "d": "disconnected"}
    assert len(calls) == 2 and len(peers["c"].frames) == 2
    _, first = await task
    assert first == {
        "b": "transport_failed_or_uncertain",
        "c": "transport_accepted",
        "d": "disconnected",
    }
    assert a._outbound_inflight == 0 and not any(a._peer_inflight.values())
    peers["b"].block = False
    peers["b"].up = peers["d"].up = True
    assert (await a.radio_received(wire(), "rf"))[1] == "duplicate_or_full"
    assert not peers["b"].frames and not peers["d"].frames  # No reconnect replay.
    _, fresh = await a.radio_received(wire(text=b"fresh"), "rf")
    assert all(x == "transport_accepted" for x in fresh.values())


@pytest.mark.asyncio
async def test_expiry_while_waiting_on_one_peer_does_not_send_stale_copy():
    a, peers, calls, now = setup()
    lock = a._send_locks["b"]
    await lock.acquire()
    task = asyncio.create_task(a.radio_received(wire(), "rf"))
    for _ in range(100):
        if peers["c"].frames and peers["d"].frames:
            break
        await asyncio.sleep(0)
    assert peers["c"].frames and peers["d"].frames
    now[0] += 16
    lock.release()
    _, outcomes = await task
    assert outcomes == {
        "b": "expired",
        "c": "transport_accepted",
        "d": "transport_accepted",
    }
    assert not peers["b"].frames


@pytest.mark.asyncio
async def test_peer_ingress_does_not_fanout_or_reexport_when_heard_on_rf():
    a, peers, calls, _ = setup()
    incoming = TestOnlyTransport("a")

    async def local(request):
        return None

    b = Adapter(
        Config("b", enabled=True, outbound={"rf": "a"}),
        local,
        {"a": incoming},
        lambda: 100.0,
    )
    await b.radio_received(wire(), "rf")
    assert (
        await a.peer_received(incoming.frames[0], peers["b"]) == "rf_planner_accepted"
    )
    assert len(calls) == 1 and calls[0].origin == "peer"
    assert all(not t.frames for t in peers.values())
    assert (await a.radio_received(wire(hops=3), "rf"))[1] == "duplicate_or_full"
    assert len(calls) == 2 and all(not t.frames for t in peers.values())


@pytest.mark.asyncio
async def test_reservation_is_shared_across_destinations_and_forgotten_on_restart(
    tmp_path,
):
    from private_link.replay import MemoryReplayStore

    a, peers, calls, _ = setup()
    a.replay_store = MemoryReplayStore()
    try:
        _, outcomes = await a.radio_received(wire(), "rf")
        assert all(value == "transport_accepted" for value in outcomes.values())
    finally:
        await a.replay_store.close()
    b, peers2, calls2, _ = setup()
    b.replay_store = MemoryReplayStore()
    try:
        assert all(
            value == "transport_accepted"
            for value in (await b.radio_received(wire(), "rf"))[1].values()
        )
        assert len(calls2) == 1 and all(len(t.frames) == 1 for t in peers2.values())
    finally:
        await b.replay_store.close()


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["b", "b"],
        None,
        4,
        ["b", None],
        ["a"],
        ["bad name"],
        [str(n) for n in range(17)],
    ],
)
def test_invalid_peer_lists_are_rejected(value):
    with pytest.raises(ValueError):
        Config("a", outbound={"rf": value})


def test_caller_list_mutation_cannot_change_routes():
    peers = ["b", "c"]
    cfg = Config("a", outbound={"rf": peers})
    peers.append("d")
    assert cfg.outbound == {"rf": ("b", "c")}
    assert Config("a", outbound={"rf": "b"}).outbound == {"rf": ("b",)}


@pytest.mark.asyncio
async def test_one_peer_raising_does_not_cancel_other_copies():
    a, peers, calls, _ = setup()

    async def rejected(frame):
        raise ConnectionError("test peer failure")

    peers["b"].send = rejected
    _, result = await a.radio_received(wire(), "rf")
    assert result == {
        "b": "transport_failed_or_uncertain",
        "c": "transport_accepted",
        "d": "transport_accepted",
    }
    assert len(calls) == 1 and not peers["b"].frames
    assert len(peers["c"].frames) == len(peers["d"].frames) == 1
