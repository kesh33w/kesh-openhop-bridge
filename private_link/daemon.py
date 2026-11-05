"""Private opt-in daemon binding. No network, database or hardware starts on import."""

import asyncio
import logging
import time

from .adapter import Adapter, Config, decode_packet, supported_peer_packet


def _configuration(daemon):
    from repeater.radio_ids import configured_radio_ids

    settings = daemon.config.get("private_island", {})
    if (
        not isinstance(settings, dict)
        or type(settings.get("enabled", False)) is not bool
    ):
        raise ValueError("private_island.enabled must be a boolean")
    if not settings.get("enabled", False):
        return None
    radios = set(configured_radio_ids(daemon.config))
    if not radios:
        raise ValueError("private_island requires an explicit named radios list")
    inbound = {}
    rows = settings.get("inbound", [])
    if not isinstance(rows, list):
        raise ValueError("private_island.inbound must be a route list")
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"peer", "radio", "local_radio"}:
            raise ValueError("Each inbound route requires peer, radio and local_radio")
        key = (row["peer"], row["radio"])
        if not all(isinstance(value, str) for value in (*key, row["local_radio"])):
            raise ValueError("Inbound route names must be strings")
        if key in inbound:
            raise ValueError("Duplicate private inbound route")
        inbound[key] = row["local_radio"]
    outbound = settings.get("outbound", {})
    if not isinstance(outbound, dict):
        raise ValueError(
            "private_island.outbound must map local radio to a peer or peer list"
        )
    if not set(outbound).issubset(radios) or not set(inbound.values()).issubset(radios):
        raise ValueError("Private routes reference an unknown local radio")
    bounds = {
        key: settings[key]
        for key in ("ttl_ms", "max_seen", "max_inflight", "send_timeout")
        if key in settings
    }
    config = Config(
        settings.get("node_id"),
        enabled=True,
        outbound=outbound,
        inbound=inbound,
        **bounds,
    )
    limit = settings.get("max_rx_tasks", 16)
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("private_island.max_rx_tasks must be 1..64")
    return settings, config, limit


class DaemonBinding:
    def __init__(self, daemon, config, transports, *, replay_store=None, limit=16):
        self.daemon = daemon
        self.limit = limit
        self.tasks = set()
        self.peer_pending = 0
        self.dropped = 0
        self.closed = False
        self.service = None
        self.owned_replay = None
        self.adapter = Adapter(
            config, self._route, transports, replay_store=replay_store
        )

    def on_hardware_rx(self, raw, rssi, snr, *, rx_radio_id=None):
        # The original dispatcher continues processing exactly once. This tee does
        # not await database, TLS, or a second RF planner invocation.
        if self.closed or rx_radio_id is None:
            return
        if len(self.tasks) >= self.limit:
            self.dropped += 1
            return
        task = asyncio.create_task(self.adapter.radio_received(bytes(raw), rx_radio_id))
        self.tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.dropped += 1

    async def _route(self, request):
        if request.origin == "hardware":
            return None  # Original dispatcher owns normal RF processing.
        if self.closed or request.origin != "peer" or self.peer_pending >= self.limit:
            return False
        packet, _, _ = decode_packet(request.raw)
        if not supported_peer_packet(packet):
            return False
        expires = getattr(request, "expires_ms", None)
        if type(expires) is not int:
            return False
        remaining = (expires / 1000.0) - time.time()
        if remaining <= 0 or remaining > 62:
            return False
        packet._private_peer = True
        packet._private_peer_deadline = time.monotonic() + remaining
        packet._rx_radio_id = (
            request.radio_id
        )  # Routing context, never an RF observation.
        packet._rssi = None
        packet._snr = None
        packet._path_hash_mode_applied = True
        packet._flood_scope_applied = True
        try:
            self.daemon.router.queue.put_nowait(packet)
        except asyncio.QueueFull:
            return False
        self.peer_pending += 1
        return True  # Queue admission, not modem TX or recipient ACK.

    def peer_finished(self):
        self.peer_pending = max(0, self.peer_pending - 1)

    async def close(self):
        self.closed = True
        self.daemon.dispatcher.remove_raw_rx_subscriber(self.on_hardware_rx)
        try:
            if self.service is not None:
                await self.service.close()
        finally:
            tasks = tuple(self.tasks)
            for task in tasks:
                task.cancel()
            try:
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                if self.owned_replay is not None:
                    await self.owned_replay.close()


async def install_from_config(
    daemon, *, transports=None, replay_store=None, service=None
):
    parsed = _configuration(daemon)
    if parsed is None:
        return None
    settings, config, limit = parsed
    # Refuse a partial installation before any database, certificate or socket I/O.
    from openhop_core.protocol import Packet

    probe = Packet()
    versions = [
        getattr(type(component), "PRIVATE_PEER_ROUTING_VERSION", None)
        for component in (daemon.router, daemon.repeater_handler)
    ]
    if (
        any(type(version) is not int or version != 1 for version in versions)
        or getattr(probe, "_private_peer", None) is not False
        or not hasattr(probe, "_private_peer_deadline")
        or probe._private_peer_deadline is not None
    ):
        raise RuntimeError(
            "Private island requires matching Core and repeater integration patches"
        )
    owned_replay = None
    if transports is None:
        from .transport import create_service

        transport_settings = settings.get("transport", {})
        if (
            not isinstance(transport_settings, dict)
            or transport_settings.get("enabled") is not True
        ):
            raise ValueError(
                "Enabled private island requires an explicitly enabled TLS transport"
            )
        service = service or create_service(transport_settings)
        transports = service.transports
    required_peers = {peer for peers in config.outbound.values() for peer in peers} | {
        key[0] for key in config.inbound
    }
    if not required_peers.issubset(transports):
        raise ValueError(
            "Private routes reference a peer absent from the authenticated transport allowlist"
        )
    if replay_store is None:
        from .replay import MemoryReplayStore

        replay_store = owned_replay = MemoryReplayStore(capacity=config.max_seen)
        logging.getLogger(__name__).info(
            "Private replay cache active; restart forgets reservations"
        )
        if "replay_path" in settings or "initialize_replay" in settings:
            logging.getLogger(__name__).warning(
                "Legacy replay_path/initialize_replay settings ignored; no database opened"
            )
    if not replay_store.healthy:
        if owned_replay is not None:
            await owned_replay.close()
        raise ValueError(
            "Private replay store is unavailable; network activation refused"
        )
    try:
        binding = DaemonBinding(
            daemon, config, transports, replay_store=replay_store, limit=limit
        )
        daemon.private_island = binding
        binding.service = service
        binding.owned_replay = owned_replay
        if service is not None:
            service.receiver = binding.adapter.peer_received
            await service.start()
        daemon.dispatcher.add_raw_rx_subscriber(binding.on_hardware_rx)
        return binding
    except BaseException:
        daemon.private_island = None
        try:
            if service is not None:
                await service.close()
        finally:
            if owned_replay is not None:
                await owned_replay.close()
        raise
