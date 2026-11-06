"""Peer activity uses validated frames and monotonic age, never RF outcomes."""

import asyncio

import pytest
from test_adapter import wire
from test_transport import MockTls, PIN, Writer, frame, service

from private_link.adapter import Adapter, Config, decode_packet, encode
from private_link.transport import PeerStream


def timed_service(tick):
    instance = service()
    instance.transports["peer"] = PeerStream("peer", PIN, clock=lambda: tick[0])
    return instance, instance.transports["peer"]


def envelope(text=b"opaque"):
    raw = wire(text=text)
    return dict(version=1, origin="peer", radio="rf", packet_id=decode_packet(raw)[2],
                issued_ms=100000, expires_ms=115000, hops=1, width=2, raw=raw.hex())


def use_adapter(instance, tick, planner):
    adapter = Adapter(
        Config("local", enabled=True, inbound={("peer", "rf"): "rf"}),
        planner, instance.transports, clock=lambda: 100,
        monotonic_clock=lambda: tick[0],
    )
    instance.receiver = adapter.peer_received
    return adapter


def test_age_unknown_disconnect_and_reconnect_without_new_inbound_activity():
    tick = [10.0]
    _, transport = timed_service(tick)
    old, replacement = Writer(MockTls()), Writer(MockTls())
    assert not transport.connected
    assert transport.last_valid_inbound_frame_age_seconds is None
    assert transport.attach(old)
    assert transport.last_valid_inbound_frame_age_seconds is None
    transport._record_valid_inbound_frame(old, tick[0])
    tick[0] = 15.25
    assert transport.last_valid_inbound_frame_age_seconds == 5.25
    # A failed authentication must not reset the live session's activity.
    assert not transport.attach(Writer(MockTls(b"unapproved")))
    assert transport.last_valid_inbound_frame_age_seconds == 5.25
    transport.detach(old)
    tick[0] = 16.0
    assert not transport.connected
    assert transport.last_valid_inbound_frame_age_seconds == 6.0
    assert transport.attach(replacement)
    assert transport.last_valid_inbound_frame_age_seconds == 6.0
    transport._record_valid_inbound_frame(old, tick[0])
    assert transport.last_valid_inbound_frame_age_seconds == 6.0
    transport._record_valid_inbound_frame(replacement, tick[0])
    transport.detach(old)
    assert transport.connected
    assert transport.last_valid_inbound_frame_age_seconds == 0
    transport.detach(replacement)


@pytest.mark.asyncio
async def test_outbound_send_does_not_report_inbound_activity():
    tick = [10.0]
    instance, transport = timed_service(tick)
    writer = Writer(MockTls())
    assert transport.attach(writer)
    await transport.send(frame())
    assert writer.frames == [frame()]
    assert transport.last_valid_inbound_frame_age_seconds is None
    await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, "rf_planner_accepted", "disabled", "disconnected", "unexpected"])
async def test_unvalidated_receiver_outcome_does_not_report_activity(outcome):
    tick = [10.0]
    instance, transport = timed_service(tick)

    async def receive(data, peer):
        return outcome

    instance.receiver = receive
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    reader.feed_data(frame())
    reader.feed_eof()
    await instance._accept(reader, writer)
    assert transport.last_valid_inbound_frame_age_seconds is None
    await instance.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [None, False])
async def test_valid_frame_is_visible_while_real_rf_planner_is_blocked(outcome):
    tick = [10.0]
    instance, transport = timed_service(tick)
    entered, release = asyncio.Event(), asyncio.Event()

    async def planner(request):
        entered.set()
        await release.wait()
        return outcome

    use_adapter(instance, tick, planner)
    data = encode(envelope())
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    task = asyncio.create_task(instance._accept(reader, writer))
    try:
        # Partial bytes are not a valid frame.
        reader.feed_data(data[:3])
        await asyncio.sleep(0)
        assert transport.last_valid_inbound_frame_age_seconds is None
        reader.feed_data(data[3:])
        await asyncio.wait_for(entered.wait(), 1)
        assert transport.connected
        assert transport.last_valid_inbound_frame_age_seconds == 0
        tick[0] = 15.0
        assert transport.last_valid_inbound_frame_age_seconds == 5.0
        assert not task.done()
        release.set()
        reader.feed_eof()
        await task
        assert transport.last_valid_inbound_frame_age_seconds == 5.0
        assert not transport.connected
    finally:
        release.set()
        await instance.close()


@pytest.mark.asyncio
async def test_raising_rf_planner_does_not_hide_valid_frame_activity():
    tick = [10.0]
    instance, transport = timed_service(tick)

    async def planner(request):
        raise RuntimeError("RF planner unavailable")

    use_adapter(instance, tick, planner)
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    reader.feed_data(encode(envelope()))
    await asyncio.wait_for(instance._accept(reader, writer), 1)
    assert not transport.connected
    assert instance.failures == 1
    tick[0] = 15.0
    assert transport.last_valid_inbound_frame_age_seconds == 5.0
    await instance.close()


@pytest.mark.asyncio
async def test_valid_frame_is_visible_while_replay_admission_is_blocked():
    tick = [10.0]
    instance, transport = timed_service(tick)
    entered, release = asyncio.Event(), asyncio.Event()
    planned = []

    async def planner(request):
        planned.append(request)

    class Replay:
        async def reserve(self, *args):
            entered.set()
            await release.wait()
            return False

    adapter = use_adapter(instance, tick, planner)
    adapter.replay_store = Replay()
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    task = asyncio.create_task(instance._accept(reader, writer))
    try:
        reader.feed_data(encode(envelope()))
        await asyncio.wait_for(entered.wait(), 1)
        tick[0] = 15.0
        assert transport.last_valid_inbound_frame_age_seconds == 5.0
        assert not planned
        release.set()
        reader.feed_eof()
        await task
        assert not planned
    finally:
        release.set()
        await instance.close()


@pytest.mark.asyncio
async def test_real_validation_rejections_do_not_refresh_but_duplicates_do():
    tick = [10.0]
    instance, transport = timed_service(tick)
    processed = asyncio.Event()
    planned = []

    async def planner(request):
        planned.append(request)
        return False  # A valid frame is not proof of even RF queue admission.

    adapter = use_adapter(instance, tick, planner)

    async def receive(data, peer):
        try:
            return await adapter.peer_received(data, peer)
        finally:
            processed.set()

    instance.receiver = receive
    good = envelope()
    reader, writer = asyncio.StreamReader(), Writer(MockTls())
    task = asyncio.create_task(instance._accept(reader, writer))

    async def feed(envelope):
        processed.clear()
        reader.feed_data(encode(envelope))
        await asyncio.wait_for(processed.wait(), 1)

    try:
        await feed(dict(good, radio="unapproved"))
        assert transport.last_valid_inbound_frame_age_seconds is None
        tick[0] = 12.0
        await feed(good)
        assert transport.last_valid_inbound_frame_age_seconds == 0
        assert len(planned) == 1
        tick[0] = 16.0
        await feed(good)
        assert transport.last_valid_inbound_frame_age_seconds == 0
        assert len(planned) == 1  # Duplicate activity did not schedule more RF.
        tick[0] = 20.0
        await feed(dict(good, expires_ms=90000, issued_ms=80000))
        assert transport.last_valid_inbound_frame_age_seconds == 4.0
        tick[0] = 25.0
        reader.feed_data(b"\x00\x00\x00\x02{}")
        await task
        assert not transport.connected
        assert transport.last_valid_inbound_frame_age_seconds == 9.0
        assert instance.failures == 1
    finally:
        reader.feed_eof()
        await instance.close()


@pytest.mark.asyncio
async def test_old_receiver_cannot_refresh_replacement_session():
    tick = [10.0]
    instance, transport = timed_service(tick)
    old_entered, old_release = asyncio.Event(), asyncio.Event()
    new_entered, new_release = asyncio.Event(), asyncio.Event()
    old_frame, new_frame = encode(envelope(b"old")), encode(envelope(b"new"))

    async def planner(request):
        return None

    adapter = use_adapter(instance, tick, planner)

    async def receive(data, peer):
        entered, release = ((old_entered, old_release) if data == old_frame
                            else (new_entered, new_release))
        entered.set()
        await release.wait()
        return await adapter.peer_received(data, peer)

    instance.receiver = receive
    reader, old = asyncio.StreamReader(), Writer(MockTls())
    task = asyncio.create_task(instance._accept(reader, old))
    new_reader, replacement = asyncio.StreamReader(), Writer(MockTls())
    new_task = None
    try:
        reader.feed_data(old_frame)
        await asyncio.wait_for(old_entered.wait(), 1)
        tick[0] = 20.0
        new_task = asyncio.create_task(instance._accept(new_reader, replacement))
        new_reader.feed_data(new_frame)
        await asyncio.wait_for(new_entered.wait(), 1)
        tick[0] = 25.0
        old_release.set()
        await task
        assert transport.owns(replacement)
        assert transport.last_valid_inbound_frame_age_seconds is None
        tick[0] = 30.0
        new_release.set()
        new_reader.feed_eof()
        await new_task
        assert transport.last_valid_inbound_frame_age_seconds == 10.0
    finally:
        old_release.set()
        new_release.set()
        reader.feed_eof()
        new_reader.feed_eof()
        await instance.close()
