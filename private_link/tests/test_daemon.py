"""Actual dispatcher/router/engine with fake radios; no listener or RF."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openhop_core.protocol import Packet, PathUtils
from private_link.adapter import AuthenticatedPeerTransport, decode_packet, encode
from private_link.daemon import install_from_config
from repeater.main import RepeaterDaemon
from repeater.packet_router import PacketRouter
from tests.test_fabric_fanout import _Rig


class TestTransport(AuthenticatedPeerTransport):
    __test__ = False
    verified_peer_id = "remote"
    connected = True

    def __init__(self):
        self.frames = []

    async def send(self, frame):
        self.frames.append(frame)

    def abort(self):
        self.connected = False


class TestStore:
    __test__ = False
    healthy = True

    def __init__(self):
        self.seen = set()

    async def reserve(self, packet_id, expiry, now):
        if packet_id in self.seen:
            return False
        self.seen.add(packet_id)
        return True


def wire(width=2, marker=1, kind=5, route=1, path=b""):
    packet = Packet()
    packet.header = (kind << 2) | route
    packet.payload = bytearray(bytes([marker, 0, 0]) + bytes([marker]) * 16)
    if kind == 3:
        packet.payload = bytearray(bytes([marker]) * 4)
    if kind in (2, 8):
        packet.payload.append(0)
    packet.payload_len = len(packet.payload)
    packet.set_path(path, PathUtils.encode_path_len(width, len(path) // width))
    return packet.write_to()


def frame(raw, lifetime=10000):
    _, width, digest = decode_packet(raw)
    now = int(time.time() * 1000)
    return encode(
        dict(
            version=1,
            origin="remote",
            radio="remote-rf",
            packet_id=digest,
            issued_ms=now,
            expires_ms=now + lifetime,
            hops=1,
            width=width,
            raw=raw.hex(),
        )
    )


async def setup():
    rig = _Rig({"repeat_on_ingress": True})
    rig.handler.local_hash_bytes = bytes([0xAB, 0x12, 0x34])
    config = rig.handler.config
    config["radios"] = [{"id": "local"}, {"radio_id": "link"}]
    config["private_island"] = {
        "enabled": True,
        "node_id": "host",
        "outbound": {"local": "remote"},
        "inbound": [{"peer": "remote", "radio": "remote-rf", "local_radio": "local"}],
    }
    daemon = RepeaterDaemon(config, radio=rig.radio)
    daemon.dispatcher = rig.dispatcher
    daemon.repeater_handler = rig.handler
    daemon.router = PacketRouter(daemon)
    await daemon.router.start()
    daemon.dispatcher.register_fallback_handler(daemon._router_callback)
    daemon.dispatcher._arm_rx()
    transport = TestTransport()
    daemon.private_island = await install_from_config(
        daemon, transports={"remote": transport}, replay_store=TestStore()
    )
    return rig, daemon, transport


async def settled(daemon):
    for _ in range(300):
        await asyncio.sleep(0.001)
        if (
            not daemon.private_island.tasks
            and daemon.router.queue.empty()
            and not daemon.router._route_tasks
        ):
            return
    raise AssertionError("Bounded fake-radio pipeline did not drain")


async def cleanup(daemon):
    if daemon.private_island is not None:
        await daemon.private_island.close()
    await daemon.dispatcher.stop()
    await daemon.router.stop()
    await daemon.repeater_handler.aclose_channel_bridge()


@pytest.mark.asyncio
async def test_hardware_tee_preserves_single_rf_dispatch_and_width_filter():
    rig, daemon, transport = await setup()
    try:
        for width in (1, 2, 3):
            rig.local.rx_callback(wire(width, marker=width), -82, 3.0)
            await settled(daemon)
        assert rig.handler.rx_count == 3
        assert len(transport.frames) == 2
        assert len(rig.air.frames) == 6  # Existing two-leg repeat, once per RX.
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_peer_transit_uses_native_queue_without_rf_metrics_or_local_helpers():
    rig, daemon, transport = await setup()
    daemon.text_helper = MagicMock()
    daemon.path_helper = MagicMock()
    try:
        for width in (2, 3):
            outcome = await daemon.private_island.adapter.peer_received(
                frame(wire(width, marker=width + 10)), transport
            )
            assert outcome == "rf_planner_accepted"
            await settled(daemon)
        assert rig.handler.rx_count == 0
        assert rig.handler.recv_flood_count == 0
        assert len(rig.air.frames) == 4
        for _, raw in rig.air.frames:
            packet, width, _ = decode_packet(raw)
            assert width in (2, 3) and packet.get_path_hash_count() == 1
        rig.handler.storage.record_packet.assert_not_called()
        assert rig.handler.private_peer_count == 2
        assert rig.handler.private_peer_last_outcome["transmitted"] is True
        assert not daemon.text_helper.mock_calls and not daemon.path_helper.mock_calls
        assert not transport.frames
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_direct_hop_ack_and_deadline_are_native_and_no_peer_reexport():
    rig, daemon, transport = await setup()
    try:
        raw = wire(2, marker=32, kind=3, route=2, path=b"\xab\x12\x22\x33")
        assert (
            await daemon.private_island.adapter.peer_received(frame(raw), transport)
            == "rf_planner_accepted"
        )
        await settled(daemon)
        assert len(rig.air.frames) == 2
        assert all(
            decode_packet(raw)[0].path == b"\x22\x33" for _, raw in rig.air.frames
        )
        before = len(rig.air.frames)
        rig.handler._calculate_tx_delay = lambda *args: 0.08
        raw = wire(2, marker=44)
        assert (
            await daemon.private_island.adapter.peer_received(frame(raw, 30), transport)
            == "rf_planner_accepted"
        )
        await settled(daemon)
        assert len(rig.air.frames) == before
        assert not transport.frames
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_unsupported_peer_payload_and_queue_saturation_fail_closed():
    rig, daemon, transport = await setup()
    try:
        raw = wire(2, marker=55, kind=9)
        with pytest.raises(ValueError):
            await daemon.private_island.adapter.peer_received(frame(raw), transport)
        daemon.private_island.peer_pending = daemon.private_island.limit
        assert (
            await daemon.private_island.adapter.peer_received(
                frame(wire(2, marker=56)), transport
            )
            == "rf_planner_rejected"
        )
        assert not rig.air.frames
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_disabled_binding_never_touches_network_or_store():
    daemon = SimpleNamespace(config={})
    assert await install_from_config(daemon) is None


@pytest.mark.asyncio
async def test_unknown_radio_or_peer_fails_before_activation():
    rig, daemon, transport = await setup()
    try:
        daemon.config["private_island"]["outbound"] = {"missing": "remote"}
        with pytest.raises(ValueError, match="unknown local radio"):
            await install_from_config(
                daemon, transports={"remote": transport}, replay_store=TestStore()
            )
        daemon.config["private_island"]["outbound"] = {"local": "unknown"}
        with pytest.raises(ValueError, match="allowlist"):
            await install_from_config(
                daemon, transports={"remote": transport}, replay_store=TestStore()
            )
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
@pytest.mark.parametrize("start_failure", [False, True])
async def test_actual_daemon_initialize_installs_binding_with_fake_io(
    monkeypatch, start_failure
):
    import private_link.daemon as module
    import repeater.main as main
    import repeater.service_utils

    rig = _Rig({"repeat_on_ingress": True})
    config = rig.handler.config
    config["radios"] = [{"id": "local"}, {"id": "link"}]
    # Existing upstream test fixture identity, never persisted by this test.
    config["repeater"].update(
        node_name="private-host-test", identity_key=b"k" * 32, allow_discovery=False
    )
    config["private_island"] = {
        "enabled": True,
        "node_id": "host",
        "outbound": {"local": "remote"},
        "inbound": [{"peer": "remote", "radio": "remote-rf", "local_radio": "local"}],
    }
    transport, store = TestTransport(), TestStore()
    install = module.install_from_config
    service = SimpleNamespace(
        receiver=None,
        start=AsyncMock(
            side_effect=OSError("test private listener startup failed")
            if start_failure
            else None
        ),
        close=AsyncMock(),
    )

    async def isolated_install(daemon):
        return await install(
            daemon,
            transports={"remote": transport},
            replay_store=store,
            service=service,
        )

    monkeypatch.setattr(module, "install_from_config", isolated_install)
    monkeypatch.setattr(
        repeater.service_utils,
        "ensure_plugin_manager_service",
        lambda: (True, "test only"),
    )
    monkeypatch.setattr(main.socket, "gethostbyname", lambda name: "192.0.2.1")
    for name in (
        "TraceHelper",
        "AdvertHelper",
        "LoginHelper",
        "ConfigManager",
        "SensorManager",
        "GPSService",
        "TextHelper",
        "PathHelper",
        "ProtocolRequestHelper",
        "NeighborScopeHelper",
    ):
        fake = MagicMock()
        fake.get_acl_dict.return_value = {}
        fake.get_summary.return_value = {}
        monkeypatch.setattr(main, name, MagicMock(return_value=fake))
    neighbors = MagicMock()
    neighbors.stop = AsyncMock()
    monkeypatch.setattr(main, "NeighborsPublisher", MagicMock(return_value=neighbors))
    glass = MagicMock()
    glass.start = AsyncMock()
    glass.stop = AsyncMock()
    monkeypatch.setattr(main, "GlassHandler", MagicMock(return_value=glass))
    daemon = RepeaterDaemon(config, radio=rig.radio)
    daemon._load_companion_identities = AsyncMock()
    try:
        with (
            patch("repeater.engine.StorageCollector"),
            patch("repeater.engine.RepeaterHandler._start_background_tasks"),
        ):
            await daemon.initialize()
        if start_failure:
            assert daemon.private_island is None
            service.close.assert_awaited_once()
            assert not any(
                getattr(callback, "__name__", "") == "on_hardware_rx"
                for callback in daemon.dispatcher._raw_rx_subscribers
            )
            daemon.repeater_handler._calculate_tx_delay = lambda *args: 0
            daemon.dispatcher._arm_rx()
            rig.local.rx_callback(wire(1, marker=81), -82, 3.0)
            for _ in range(300):
                await asyncio.sleep(0.001)
                if len(rig.air.frames) == 2:
                    break
            assert len(rig.air.frames) == 2
            assert daemon.repeater_handler.rx_count == 1
            return
        assert daemon.private_island is not None
        daemon.repeater_handler._calculate_tx_delay = lambda *args: 0
        result = await daemon.private_island.adapter.peer_received(
            frame(wire(2, marker=80)), transport
        )
        assert result == "rf_planner_accepted"
        await settled(daemon)
        assert len(rig.air.frames) == 2
        assert daemon.repeater_handler.rx_count == 0
        assert not daemon.text_helper.process_text_packet.called
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_slow_replay_store_and_tee_saturation_do_not_block_normal_rf():
    rig, daemon, transport = await setup()
    gate = asyncio.Event()

    class SlowStore(TestStore):
        async def reserve(self, *args):
            await gate.wait()
            return await super().reserve(*args)

    daemon.private_island.adapter.replay_store = SlowStore()
    daemon.private_island.limit = 1
    try:
        rig.local.rx_callback(wire(2, marker=90), -80, 1.0)
        rig.local.rx_callback(wire(2, marker=91), -80, 1.0)
        for _ in range(100):
            await asyncio.sleep(0.001)
            if len(rig.air.frames) == 4:
                break
        assert rig.handler.rx_count == 2 and len(rig.air.frames) == 4
        assert not transport.frames and daemon.private_island.dropped == 1
        gate.set()
        await settled(daemon)
        assert len(transport.frames) == 1
    finally:
        gate.set()
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_close_releases_owned_replay_even_if_transport_close_fails():
    rig, daemon, _ = await setup()
    service = SimpleNamespace(
        close=AsyncMock(side_effect=RuntimeError("test close failure"))
    )
    owned = SimpleNamespace(close=AsyncMock())
    daemon.private_island.service = service
    daemon.private_island.owned_replay = owned
    try:
        with pytest.raises(RuntimeError, match="test close failure"):
            await daemon.private_island.close()
        owned.close.assert_awaited_once()
        assert (
            daemon.private_island.on_hardware_rx
            not in daemon.dispatcher._raw_rx_subscribers
        )
    finally:
        daemon.private_island.service = None
        daemon.private_island.owned_replay = None
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_peer_channel_mapping_preserves_width_expiry_and_unknown_observation(
    tmp_path,
):
    from repeater.channel_bridge import BridgeSettings, AsyncEchoLedger
    from openhop_core.protocol.crypto import CryptoUtils
    from tests.test_channel_bridge import packet as chat_packet, C1, C2
    from tests.test_channel_bridge_engine import config as bridge_config

    rig, daemon, transport = await setup()
    daemon.config["channel_bridge"] = bridge_config(tmp_path)
    rig.handler.channel_bridge = BridgeSettings.from_config(daemon.config)
    settings = rig.handler.channel_bridge
    rig.handler.bridge_echoes = AsyncEchoLedger(
        settings.database, ttl=settings.ttl, capacity=settings.capacity
    )
    rig.handler.bridge_egress_callback = daemon.router.deliver_bridge_egress
    observer = SimpleNamespace(
        _receive_radio_ids=frozenset({"link"}),
        process_received_packet=AsyncMock(
            return_value=SimpleNamespace(authenticated=True)
        ),
    )
    daemon.companion_bridges = {7: observer}
    daemon.dispatcher.set_default_path_hash_mode(0)
    try:
        for width in (2, 3):
            source = chat_packet(C1, message=f"private mapping width {width}")
            source.set_path(b"", PathUtils.encode_path_len(width, 0))
            assert (
                await daemon.private_island.adapter.peer_received(
                    frame(source.write_to()), transport
                )
                == "rf_planner_accepted"
            )
            await settled(daemon)
            local, translated = rig.sent_packets[-2:]
            assert local[0] == "local" and translated[0] == "link"
            assert translated[1].get_path_hash_size() == width
            assert (
                translated[1]._private_peer
                and translated[1]._private_peer_deadline is not None
            )
            assert (
                translated[1]._path_hash_mode_applied
                and translated[1]._flood_scope_applied
            )
            assert CryptoUtils.mac_then_decrypt(
                C2.key, C2.key, bytes(translated[1].payload[1:])
            ) == CryptoUtils.mac_then_decrypt(C1.key, C1.key, bytes(source.payload[1:]))
        assert observer.process_received_packet.await_count == 2
        for call in observer.process_received_packet.await_args_list:
            seen = call.args[0]
            assert seen._rssi is None and seen._snr is None
        before = len(rig.air.frames)
        rig.handler._calculate_tx_delay = lambda *args: 0.08
        late = chat_packet(C1, message="expire before mapped TX")
        late.set_path(b"", PathUtils.encode_path_len(3, 0))
        assert (
            await daemon.private_island.adapter.peer_received(
                frame(late.write_to(), 30), transport
            )
            == "rf_planner_accepted"
        )
        await settled(daemon)
        assert len(rig.air.frames) == before
        assert observer.process_received_packet.await_count == 2
        assert not transport.frames and rig.handler.rx_count == 0
        rig.handler.storage.record_packet.assert_not_called()
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_actual_binding_restart_forgets_replay_but_same_run_suppresses(tmp_path):
    saved = frame(wire(2, marker=101))
    database = str(tmp_path / "private-replay.sqlite")
    for first in (True, False):
        rig, daemon, transport = await setup()
        await daemon.private_island.close()
        daemon.config["private_island"].update(
            replay_path=database, initialize_replay=first
        )
        daemon.private_island = await install_from_config(
            daemon, transports={"remote": transport}
        )
        try:
            result = await daemon.private_island.adapter.peer_received(saved, transport)
            await settled(daemon)
            assert result == "rf_planner_accepted" and len(rig.air.frames) == 2
            assert (
                await daemon.private_island.adapter.peer_received(saved, transport)
                == "duplicate_or_full"
            )
            assert not tmp_path.joinpath("private-replay.sqlite").exists()
        finally:
            await cleanup(daemon)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["router", "engine", "core"])
async def test_partial_integration_refuses_activation_before_io(monkeypatch, component):
    import private_link.transport as transport_module
    import private_link.replay as replay_module
    import openhop_core.protocol as protocol

    rig, daemon, _ = await setup()
    try:
        daemon.config["private_island"]["transport"] = {"enabled": True}
        create = MagicMock(
            side_effect=AssertionError("Transport must not be constructed")
        )
        opening = MagicMock(side_effect=AssertionError("Cache must not be constructed"))
        monkeypatch.setattr(transport_module, "create_service", create)
        monkeypatch.setattr(replay_module, "MemoryReplayStore", opening)
        if component == "core":
            monkeypatch.setattr(protocol, "Packet", lambda: SimpleNamespace())
        else:
            cls = type(
                daemon.router if component == "router" else daemon.repeater_handler
            )
            monkeypatch.setattr(cls, "PRIVATE_PEER_ROUTING_VERSION", 0)
        with pytest.raises(RuntimeError, match="matching Core and repeater"):
            await install_from_config(daemon)
        create.assert_not_called()
        opening.assert_not_called()
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
async def test_hardware_fanout_uses_native_daemon_once_per_rx():
    rig, daemon, first = await setup()
    try:
        await daemon.private_island.close()
        second = TestTransport()
        second.verified_peer_id = "other"
        daemon.config["private_island"]["outbound"] = {"local": ["remote", "other"]}
        daemon.private_island = await install_from_config(
            daemon,
            transports={"remote": first, "other": second},
            replay_store=TestStore(),
        )
        for width in (1, 2, 3):
            rig.local.rx_callback(wire(width, marker=width), -82, 3.0)
            await settled(daemon)
        assert first.frames == second.frames and len(first.frames) == 2
        assert rig.handler.rx_count == 3 and len(rig.air.frames) == 6
        daemon.config["private_island"]["outbound"] = {"local": ["remote", "unknown"]}
        with pytest.raises(ValueError, match="allowlist"):
            await install_from_config(
                daemon,
                transports={"remote": first, "other": second},
                replay_store=TestStore(),
            )
    finally:
        await cleanup(daemon)


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [2, 3])
@pytest.mark.parametrize("ack_length", [4, 6])
async def test_recipient_ack_bytes_survive_actual_daemon_route(width, ack_length):
    """Protocol/hardware mock only: native host consumes its hop, preserving ACK bytes."""
    rig, daemon, transport = await setup()
    try:
        local_hop = bytes([0xAB, 0x12, 0x34])[:width]
        packet = Packet()
        packet.header = (3 << 2) | 2
        packet.payload = bytearray(
            bytes([73, 29, width, ack_length, 7, 9])[:ack_length]
        )
        packet.payload_len = ack_length
        packet.set_path(local_hop, PathUtils.encode_path_len(width, 1))
        assert (
            await daemon.private_island.adapter.peer_received(
                frame(packet.write_to()), transport
            )
            == "rf_planner_accepted"
        )
        await settled(daemon)
        assert len(rig.air.frames) == 2
        for _, raw in rig.air.frames:
            forwarded, observed_width, _ = decode_packet(raw)
            assert observed_width == width and forwarded.get_path_hash_count() == 0
            assert bytes(forwarded.payload) == bytes(packet.payload)
        assert not transport.frames
    finally:
        await cleanup(daemon)
