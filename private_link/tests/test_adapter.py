import asyncio
import pytest
from openhop_core.protocol import Packet, PathUtils
from private_link.adapter import (
    Adapter,
    AuthenticatedPeerTransport,
    Config,
    decode,
    encode,
)


def wire(width=2, hops=1, text=b"opaque"):
    p = Packet()
    p.header = (5 << 2) | 1
    # A structurally valid opaque group packet: tag/MAC plus padded ciphertext.
    payload = text.ljust(19 + max(0, (len(text) - 19 + 15) // 16) * 16, b"x")
    p.payload = bytearray(payload)
    p.payload_len = len(payload)
    p.set_path(bytes([17]) * width * hops, PathUtils.encode_path_len(width, hops))
    return p.write_to()


class TestOnlyTransport(AuthenticatedPeerTransport):
    __test__ = False

    def __init__(self, peer):
        self.peer = peer
        self.up = True
        self.frames = []
        self.block = False

    @property
    def verified_peer_id(self):
        return self.peer

    @property
    def connected(self):
        return self.up

    def abort(self):
        self.up = False

    async def send(self, frame):
        if self.block:
            await asyncio.sleep(1)
        self.frames.append(frame)


def pair(**kwargs):
    now = [100.0]
    ab, ba = TestOnlyTransport("b"), TestOnlyTransport("a")
    calls = {"a": [], "b": []}

    async def ar(r):
        calls["a"].append(r)
        return "local-a"

    async def br(r):
        calls["b"].append(r)
        return "local-b"

    a = Adapter(
        Config(
            "a",
            enabled=True,
            outbound={"rf": "b"},
            inbound={("b", "rf"): "rf"},
            **kwargs,
        ),
        ar,
        {"b": ab},
        lambda: now[0],
        monotonic_clock=lambda: now[0],
    )
    b = Adapter(
        Config(
            "b",
            enabled=True,
            outbound={"rf": "a"},
            inbound={("a", "rf"): "rf"},
            **kwargs,
        ),
        br,
        {"a": ba},
        lambda: now[0],
        monotonic_clock=lambda: now[0],
    )
    return a, b, ab, ba, calls, now


@pytest.mark.asyncio
@pytest.mark.parametrize("width,hops", [(2, 0), (2, 1), (3, 0), (3, 2)])
async def test_reciprocal_width_and_shared_planner(width, hops):
    a, b, ab, ba, calls, _ = pair()
    raw = wire(width, hops)
    assert await a.radio_received(raw, "rf") == ("local-a", "transport_accepted")
    assert await b.peer_received(ab.frames[0], ba) == "rf_planner_accepted"
    assert calls["b"][0].raw == raw and calls["b"][0].preserve_width
    assert calls["b"][0].origin == "peer"
    reply = wire(width, hops, b"reply")
    await b.radio_received(reply, "rf")
    await a.peer_received(ba.frames[0], ab)
    assert calls["a"][-1].raw == reply
    assert len(ab.frames) == len(ba.frames) == 1  # No peer-to-peer retransmission.


@pytest.mark.asyncio
async def test_single_byte_rf_and_disabled_link_never_suppressed():
    a, b, ab, ba, calls, _ = pair()
    raw = wire(1)
    for _ in range(2):
        assert (await a.radio_received(raw, "rf"))[1] == "ineligible"
    assert len(calls["a"]) == 2 and not ab.frames
    off = Adapter(Config("off"), a.rf_planner)
    assert (await off.radio_received(wire(), "rf"))[1] == "disabled"
    assert len(calls["a"]) == 3


@pytest.mark.asyncio
async def test_dedup_does_not_starve_local_rf_and_blocks_network_rf_loop():
    a, b, ab, ba, calls, _ = pair()
    raw = wire()
    await a.radio_received(raw, "rf")
    await b.peer_received(ab.frames[0], ba)
    assert (await b.radio_received(raw, "rf"))[1] == "duplicate_or_full"
    assert len(calls["b"]) == 2 and not ba.frames
    assert await b.peer_received(ab.frames[0], ba) == "duplicate_or_full"
    await a.radio_received(raw, "rf")
    assert len(calls["a"]) == 2 and len(ab.frames) == 1


@pytest.mark.asyncio
async def test_outage_has_no_backlog_and_local_rf_still_runs():
    a, b, ab, ba, calls, now = pair()
    ab.up = False
    raw = wire()
    assert (await a.radio_received(raw, "rf"))[1] == "disconnected"
    # A duplicate arriving from the peer must not trigger another RF dispatch.
    await b.radio_received(raw, "rf")
    ab.up = True
    assert await a.peer_received(ba.frames[0], ab) == "duplicate_or_full"
    assert len(calls["a"]) == 1 and not ab.frames
    assert (await a.radio_received(raw, "rf"))[1] == "duplicate_or_full"
    await a.radio_received(wire(text=b"fresh"), "rf")
    assert len(ab.frames) == 1
    now[0] += 20
    assert (await a.radio_received(raw, "rf"))[1] == "transport_accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("width", 1),
        ("width", 3),
        ("version", 2),
        ("hops", 0),
        ("hops", 2),
        ("origin", "impostor"),
        ("radio", "unknown"),
        ("packet_id", "wrong"),
        ("expires_ms", 99999),
        ("issued_ms", 104000),
        ("expires_ms", 200000),
        ("version", True),
    ],
)
async def test_reject_invalid_peer_envelope(field, value):
    a, b, ab, ba, calls, _ = pair()
    await a.radio_received(wire(), "rf")
    e = decode(ab.frames[0])
    e[field] = value
    with pytest.raises(ValueError):
        await b.peer_received(encode(e), ba)
    assert not calls["b"]


@pytest.mark.asyncio
async def test_peer_one_byte_and_unrecognized_session_rejected():
    a, b, ab, ba, calls, _ = pair()
    await a.radio_received(wire(), "rf")
    with pytest.raises(ValueError):
        await b.peer_received(ab.frames[0], TestOnlyTransport("a"))
    e = decode(ab.frames[0])
    e["raw"] = wire(1).hex()
    e["width"] = 1
    with pytest.raises(ValueError):
        await b.peer_received(encode(e), ba)
    assert not calls["b"]


@pytest.mark.parametrize(
    "frame", [b"", b"\0\0\0\x02{}extra", b"x" * 2053, b"\0\0\0\x02[]"]
)
def test_frame_bounds(frame):
    with pytest.raises(ValueError):
        decode(frame)


def test_duplicate_json_key_and_bad_encoded_width():
    data = b'{"version":1,"version":1}'
    with pytest.raises(ValueError):
        decode(len(data).to_bytes(4, "big") + data)
    from private_link.adapter import decode_packet

    with pytest.raises(ValueError):
        decode_packet(bytes([21, 192, 1]))


@pytest.mark.asyncio
async def test_bounded_seen_timeout_and_capacity_do_not_suppress_local_rf():
    a, b, ab, ba, calls, _ = pair(max_seen=1, send_timeout=0.01)
    ab.block = True
    assert (await a.radio_received(wire(), "rf"))[1] == "transport_failed_or_uncertain"
    assert not ab.frames
    assert (await a.radio_received(wire(text=b"next"), "rf"))[1] == "duplicate_or_full"
    assert len(calls["a"]) == 2


@pytest.mark.asyncio
async def test_concurrent_send_limit_and_cancellation_retains_dedup():
    a, b, ab, ba, calls, _ = pair(max_inflight=1)
    ab.block = True
    task = asyncio.create_task(a.radio_received(wire(), "rf"))
    await asyncio.sleep(0.01)
    assert (await a.radio_received(wire(text=b"next"), "rf"))[1] == "busy"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert a._outbound_inflight == a._inbound_inflight == 0
    assert (await a.radio_received(wire(), "rf"))[1] == "duplicate_or_full"
    assert len(calls["a"]) == 3


def test_no_default_unauthenticated_transport():
    with pytest.raises(TypeError):
        AuthenticatedPeerTransport()
    with pytest.raises(ValueError):
        Adapter(Config("a"), None, {"peer": object()})


@pytest.mark.asyncio
async def test_expiry_during_local_dispatch_and_peer_policy_rejection():
    a, b, ab, ba, calls, now = pair()

    async def slow_local(request):
        now[0] += 20
        return False

    a.rf_planner = slow_local
    assert await a.radio_received(wire(), "rf") == (False, "expired")
    assert not ab.frames
    a, b, ab, ba, calls, now = pair()

    async def reject(request):
        return False

    b.rf_planner = reject
    await a.radio_received(wire(), "rf")
    assert await b.peer_received(ab.frames[0], ba) == "rf_planner_rejected"
    assert await b.peer_received(ab.frames[0], ba) == "duplicate_or_full"


@pytest.mark.parametrize("node", ["space inside", "é", "", "x" * 65])
def test_embedded_safe_identifiers(node):
    with pytest.raises(ValueError):
        Config(node)


def test_empty_payload_not_forwardable():
    from private_link.adapter import decode_packet

    with pytest.raises(ValueError):
        decode_packet(bytes([21, 64]))


@pytest.mark.asyncio
async def test_restart_allows_reexport_without_suppressing_hardware_rf(
    tmp_path,
):
    from private_link.replay import MemoryReplayStore

    store = MemoryReplayStore()
    a, _, ab, _, calls, _ = pair()
    a.replay_store = store
    raw = wire()
    assert (await a.radio_received(raw, "rf"))[1] == "transport_accepted"
    await store.close()
    store = MemoryReplayStore()
    a2, _, ab2, _, calls2, _ = pair()
    a2.replay_store = store
    try:
        assert (await a2.radio_received(raw, "rf"))[1] == "transport_accepted"
        assert len(calls2["a"]) == len(ab2.frames) == 1
        assert len(calls["a"]) == len(ab.frames) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_restart_allows_fresh_peer_replay(tmp_path):
    from private_link.replay import MemoryReplayStore

    a, b, ab, ba, calls, _ = pair()
    await a.radio_received(wire(), "rf")
    store = MemoryReplayStore()
    b.replay_store = store
    assert await b.peer_received(ab.frames[0], ba) == "rf_planner_accepted"
    assert calls["b"][0].expires_ms == 115000
    await store.close()
    store = MemoryReplayStore()
    _, b2, _, ba2, calls2, _ = pair()
    b2.replay_store = store
    try:
        assert await b2.peer_received(ab.frames[0], ba2) == "rf_planner_accepted"
        assert len(calls2["b"]) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_storage_failure_preserves_local_rf_but_denies_crossings():
    class Broken:
        async def reserve(self, *args):
            raise OSError("test storage fault")

    a, b, ab, ba, calls, _ = pair()
    a.replay_store = Broken()
    assert (await a.radio_received(wire(), "rf"))[1] == "replay_store_denied"
    assert len(calls["a"]) == 1 and not ab.frames
    a.replay_store = None
    await a.radio_received(wire(text=b"new"), "rf")
    b.replay_store = Broken()
    assert await b.peer_received(ab.frames[0], ba) == "replay_store_denied"
    assert not calls["b"]


@pytest.mark.asyncio
async def test_expiry_during_reservation_does_not_reach_peer_rf():
    a, b, ab, ba, calls, now = pair()
    await a.radio_received(wire(), "rf")

    class Slow:
        async def reserve(self, *args):
            now[0] += 20
            return True

    b.replay_store = Slow()
    assert await b.peer_received(ab.frames[0], ba) == "expired"
    assert not calls["b"]


@pytest.mark.asyncio
async def test_near_expiry_peer_retains_echo_suppression_after_send_deadline():
    a, b, ab, ba, calls, now = pair()
    raw = wire()
    await a.radio_received(raw, "rf")
    e = decode(ab.frames[0])
    e["issued_ms"] = 85001
    e["expires_ms"] = 100001
    assert await b.peer_received(encode(e), ba) == "rf_planner_accepted"
    now[0] += 0.002
    assert (await b.radio_received(raw, "rf"))[1] == "duplicate_or_full"
    assert len(calls["b"]) == 2 and not ba.frames


@pytest.mark.asyncio
async def test_trace_never_crosses_but_still_reaches_normal_rf():
    from private_link.adapter import decode_packet

    a, b, ab, ba, calls, _ = pair()
    raw = wire()
    raw = bytes([(9 << 2) | 1]) + raw[1:]
    assert (await a.radio_received(raw, "rf"))[1] == "ineligible"
    assert len(calls["a"]) == 1 and not ab.frames
    _, width, digest = decode_packet(raw)
    e = dict(
        version=1,
        origin="a",
        radio="rf",
        packet_id=digest,
        issued_ms=100000,
        expires_ms=115000,
        hops=1,
        width=width,
        raw=raw.hex(),
    )
    with pytest.raises(ValueError, match="Unsupported private transit"):
        await b.peer_received(encode(e), ba)


@pytest.mark.asyncio
async def test_sends_to_one_peer_are_serialized():
    a, _, ab, _, _, _ = pair()
    active = 0
    peak = 0

    async def send(frame):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        ab.frames.append(frame)
        active -= 1

    ab.send = send
    results = await asyncio.gather(
        a.radio_received(wire(text=b"A"), "rf"), a.radio_received(wire(text=b"B"), "rf")
    )
    assert peak == 1 and len(ab.frames) == 2
    assert all(result[1] == "transport_accepted" for result in results)


@pytest.mark.asyncio
async def test_uncertain_send_aborts_session_and_changed_peer_binding_is_rejected():
    a, _, ab, _, _, _ = pair(send_timeout=0.01)
    ab.block = True
    assert (await a.radio_received(wire(), "rf"))[1] == "transport_failed_or_uncertain"
    assert not ab.connected
    ab.up = True
    ab.peer = "wrong"
    assert (await a.radio_received(wire(text=b"new"), "rf"))[
        1
    ] == "peer_binding_changed"
    assert not ab.connected


@pytest.mark.asyncio
async def test_inbound_capacity_does_not_consume_outbound_capacity():
    a, b, ab, ba, calls, _ = pair(max_inflight=1)
    await a.radio_received(wire(), "rf")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def planner(request):
        if request.origin == "peer":
            entered.set()
            await release.wait()
        calls["b"].append(request)
        return True

    b.rf_planner = planner
    incoming = asyncio.create_task(b.peer_received(ab.frames[0], ba))
    await entered.wait()
    try:
        assert (await b.radio_received(wire(text=b"local"), "rf"))[
            1
        ] == "transport_accepted"
    finally:
        release.set()
        await incoming


@pytest.mark.asyncio
async def test_unmapped_radio_does_not_fill_network_seen_capacity():
    a, _, ab, _, calls, _ = pair(max_seen=1)
    assert (await a.radio_received(wire(), "unmapped"))[1] == "ineligible"
    assert not a._seen
    assert (await a.radio_received(wire(text=b"mapped"), "rf"))[
        1
    ] == "transport_accepted"
    assert len(calls["a"]) == 2 and len(ab.frames) == 1


def test_utf16_and_escaped_fields_rejected():
    import json

    body = json.dumps(
        dict(
            version=1,
            origin="a",
            radio="rf",
            packet_id="00" * 32,
            issued_ms=100000,
            expires_ms=115000,
            hops=1,
            width=2,
            raw="154001",
        )
    )
    for raw in (
        body.encode("utf-16"),
        body.replace('"origin": "a"', '"origin": "\\u0061"').encode(),
    ):
        with pytest.raises(ValueError):
            decode(len(raw).to_bytes(4, "big") + raw)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "alter", [str.upper, lambda value: value[:2] + " " + value[2:]]
)
async def test_noncanonical_packet_hex_rejected(alter):
    a, b, ab, ba, _, _ = pair()
    await a.radio_received(wire(text=b"\xab"), "rf")
    e = decode(ab.frames[0])
    e["raw"] = alter(e["raw"])
    with pytest.raises(ValueError, match="Invalid payload fields"):
        await b.peer_received(encode(e), ba)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", range(16))
async def test_payload_policy_matches_standalone_native_allowlist(kind):
    a, _, ab, _, calls, _ = pair()
    size = 19 if kind in (5, 6) else 4 if kind == 3 else 20
    p = Packet()
    p.header = (kind << 2) | 1
    p.payload = bytearray(b"x" * size)
    p.payload_len = size
    p.set_path(b"", PathUtils.encode_path_len(2, 0))
    result = await a.radio_received(p.write_to(), "rf")
    allowed = kind in (2, 3, 5, 6, 8)
    assert result[1] == ("transport_accepted" if allowed else "ineligible")
    assert len(calls["a"]) == 1 and len(ab.frames) == int(allowed)
