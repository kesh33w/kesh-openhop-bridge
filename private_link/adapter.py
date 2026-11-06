"""Optional RF-island boundary, deliberately without a network implementation.

The trusted integration must call radio_received only from actual hardware RX.
Both local and peer ingress use the SAME supplied RF planner callback. The callback
owns RF policy, path consumption, airtime and confirmed TX semantics; this adapter
never implements a second RF routing algorithm or decrypts packet payloads.
"""

import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from openhop_core.protocol import Packet


class PacketRejected(ValueError):
    """A complete authenticated envelope rejected by packet policy, not framing."""

    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


MAX_FRAME = 2048
MAX_PACKET = 254
FIELDS = {
    "version",
    "origin",
    "radio",
    "packet_id",
    "issued_ms",
    "expires_ms",
    "hops",
    "width",
    "raw",
}


def decode_packet(raw):
    if not isinstance(raw, bytes) or not 2 <= len(raw) <= MAX_PACKET:
        raise ValueError("Invalid radio packet length")
    packet = Packet()
    try:
        if not packet.read_from(raw) or not packet.payload or packet.write_to() != raw:
            raise ValueError("Noncanonical packet")
    except (ValueError, IndexError) as exc:
        raise ValueError("Malformed radio packet") from exc
    return packet, packet.get_path_hash_size(), packet.calculate_packet_hash().hex()


def supported_peer_packet(packet):
    """Initial transit set shared with standalone firmware; never decrypts payload."""
    kind, size = packet.get_payload_type(), len(packet.payload)
    return (
        (kind in (5, 6) and size >= 19 and (size - 3) % 16 == 0)
        or (kind in (2, 8) and size >= 20 and (size - 4) % 16 == 0)
        or (kind == 3 and size in (4, 6))
    )


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate envelope field")
        result[key] = value
    return result


def encode(envelope):
    payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
    if len(payload) > MAX_FRAME:
        raise ValueError("Envelope too large")
    return len(payload).to_bytes(4, "big") + payload


def decode(frame):
    if not isinstance(frame, bytes) or not 4 < len(frame) <= MAX_FRAME + 4:
        raise ValueError("Invalid frame size")
    if int.from_bytes(frame[:4], "big") != len(frame) - 4:
        raise ValueError("Invalid frame length")
    try:
        body = frame[4:].decode("utf-8")
        if "\\" in body:
            raise ValueError("Escaped fields are outside the private wire subset")
        value = json.loads(body, object_pairs_hook=_object)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Malformed envelope") from exc
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("Unexpected envelope fields")
    return value


class AuthenticatedPeerTransport(ABC):
    """Security boundary, not an authentication implementation.

    Implementations MUST authenticate and encrypt a session, pin peer identity,
    reject application data until authentication completes, bound framing before
    allocation, disable early data, and discard any unsent frames on disconnect.
    No production implementation is included. Test doubles belong only in tests.
    """

    @property
    @abstractmethod
    def verified_peer_id(self) -> str: ...

    @property
    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def abort(self) -> None:
        """Synchronously invalidate and close the current uncertain session."""

    def abort_after_send_failure(self) -> None:
        """Close an uncertain send; session-aware implementations may already do so."""
        self.abort()

    def inbound_frame_validated(self) -> None:
        """Optional synchronous activity observer, independent of RF admission."""

    @abstractmethod
    async def send(self, frame: bytes) -> None:
        """Acceptance by transport only, never evidence of RF or recipient ACK."""


@dataclass(frozen=True)
class Config:
    node_id: str
    enabled: bool = False
    # Local ingress radio -> approved peer(s). No network-to-network forwarding.
    outbound: dict[str, str | list[str] | tuple[str, ...]] = field(default_factory=dict)
    # (authenticated peer, remote ingress radio) -> local RF ingress context.
    inbound: dict[tuple[str, str], str] = field(default_factory=dict)
    ttl_ms: int = 15000
    max_seen: int = 1024
    max_inflight: int = 4
    send_timeout: float = 1.0

    def __post_init__(self):
        routes = {}
        for radio, value in self.outbound.items():
            peers = (value,) if isinstance(value, str) else value
            if not isinstance(peers, (list, tuple)) or not 1 <= len(peers) <= 16:
                raise ValueError(
                    "Outbound route requires one to sixteen explicit peers"
                )
            if any(not isinstance(peer, str) for peer in peers) or len(
                set(peers)
            ) != len(peers):
                raise ValueError("Outbound peers must be distinct names")
            routes[radio] = tuple(peers)
        object.__setattr__(self, "outbound", routes)
        all_peers = {peer for peers in routes.values() for peer in peers}
        if len(all_peers) > 16:
            raise ValueError("Too many outbound peers")
        names = [
            self.node_id,
            *self.outbound,
            *all_peers,
            *self.inbound.values(),
        ]
        for key in self.inbound:
            if not isinstance(key, tuple) or len(key) != 2:
                raise ValueError("Inbound route must name peer and radio")
            names.extend(key)
        if any(
            not isinstance(n, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) is None
            for n in names
        ):
            raise ValueError("Invalid explicit route identity")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for value, low, high in [
            (self.ttl_ms, 100, 60000),
            (self.max_seen, 1, 10000),
            (self.max_inflight, 1, 64),
        ]:
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid adapter bounds")
        if (
            not isinstance(self.send_timeout, (int, float))
            or not 0 < self.send_timeout <= 5
        ):
            raise ValueError("Invalid send timeout")
        if len(self.outbound) > 16 or len(self.inbound) > 32:
            raise ValueError("Too many routes")
        if self.node_id in all_peers or any(k[0] == self.node_id for k in self.inbound):
            raise ValueError("Self peer route forbidden")


@dataclass(frozen=True)
class RfIngress:
    raw: bytes
    radio_id: str
    origin: str  # 'hardware' or 'peer'; never reinterpret peer input as hardware RX.
    preserve_width: bool = True
    expires_ms: int | None = None


class Adapter:
    def __init__(
        self,
        config: Config,
        rf_planner: Callable[[RfIngress], Awaitable[object]],
        transports=None,
        clock=time.time,
        replay_store=None,
        monotonic_clock=time.monotonic,
    ):
        # Freeze caller-owned route maps, so policy cannot mutate during an await.
        self.config = Config(
            **{
                **vars(config),
                "outbound": dict(config.outbound),
                "inbound": dict(config.inbound),
            }
        )
        self.rf_planner = rf_planner
        self.transports = dict(transports or {})
        self.clock = clock
        self.monotonic_clock = monotonic_clock
        self.replay_store = replay_store
        self._seen = {}
        self._outbound_inflight = 0
        self._peer_inflight = {}
        self._inbound_inflight = 0
        self._send_locks = {peer: asyncio.Lock() for peer in self.transports}
        for peer, transport in self.transports.items():
            if (
                not isinstance(transport, AuthenticatedPeerTransport)
                or transport.verified_peer_id != peer
            ):
                raise ValueError("An authenticated peer-bound transport is required")

    def _remember(self, packet_id, expiry, admitted_ms):
        now = self.monotonic_clock()
        self._seen = {key: end for key, end in self._seen.items() if end > now}
        if packet_id in self._seen or len(self._seen) >= self.config.max_seen:
            return False
        # Use the admission sample that produced expiry, never a corrected wall clock.
        self._seen[packet_id] = now + max(0, expiry - admitted_ms) / 1000
        return True

    async def _reserve_replay(self, packet_id, expiry, admitted_ms):
        if self.replay_store is None:
            return True  # Adapter-only use still has the bounded monotonic _seen cache.
        try:
            accepted = (
                await self.replay_store.reserve(packet_id, expiry, admitted_ms) is True
            )
            if not accepted:
                logging.getLogger(__name__).warning("Private replay reservation denied")
            return accepted
        except Exception:
            logging.getLogger(__name__).error("Private replay reservation failed")
            return False  # Admission failure never suppresses ordinary local RF.

    async def radio_received(self, raw: bytes, radio_id: str):
        """Hardware-only integration hook. Local RF is independent of link state."""
        candidate = None
        fresh = False
        now = int(self.clock() * 1000)
        expiry = now + self.config.ttl_ms
        peers = self.config.outbound.get(radio_id)
        if self.config.enabled and peers is not None:
            try:
                packet, width, packet_id = decode_packet(raw)
                if width in (2, 3) and supported_peer_packet(packet):
                    candidate = (width, packet_id)
                    # Record actual RF even during outage, before awaiting the shared
                    # planner. Network dedup never decides whether local RF runs.
                    fresh = self._remember(packet_id, expiry, now)
            except ValueError:
                pass
        local = await self.rf_planner(RfIngress(raw, radio_id, "hardware"))
        if not self.config.enabled:
            return local, "disabled"
        if candidate is None or peers is None:
            return local, "ineligible"
        if not fresh:
            return local, "duplicate_or_full"
        width, packet_id = candidate
        if int(self.clock() * 1000) >= expiry:
            return local, "expired"
        if not await self._reserve_replay(packet_id, expiry, now):
            return local, "replay_store_denied"
        envelope = dict(
            version=1,
            origin=self.config.node_id,
            radio=radio_id,
            packet_id=packet_id,
            issued_ms=now,
            expires_ms=expiry,
            hops=1,
            width=width,
            raw=raw.hex(),
        )
        frame = encode(envelope)
        # Reserve packet identity once, then independently admit each destination.
        outcomes = await asyncio.gather(
            *(self._send_to_peer(peer, frame, expiry) for peer in peers)
        )
        # Preserve the original result for existing single-destination callers.
        return local, outcomes[0] if len(peers) == 1 else dict(zip(peers, outcomes))

    async def _send_to_peer(self, peer, frame, expiry):
        transport = self.transports.get(peer)
        if transport is None or not transport.connected:
            return "disconnected"
        if transport.verified_peer_id != peer:
            transport.abort()
            return "peer_binding_changed"
        if self._peer_inflight.get(peer, 0) >= self.config.max_inflight:
            return "busy"
        self._peer_inflight[peer] = self._peer_inflight.get(peer, 0) + 1
        self._outbound_inflight += 1
        sending = False
        try:
            async with self._send_locks[peer]:
                if int(self.clock() * 1000) >= expiry:
                    return "expired"
                if not transport.connected or transport.verified_peer_id != peer:
                    return "disconnected"
                sending = True
                await asyncio.wait_for(
                    transport.send(frame), timeout=self.config.send_timeout
                )
            return "transport_accepted"
        except (Exception, asyncio.CancelledError) as exc:
            # Never retry uncertain sends. Cancelling a queued copy must not abort
            # a different packet that already owns this peer's send lock.
            if sending:
                transport.abort_after_send_failure()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return "transport_failed_or_uncertain"
        finally:
            self._peer_inflight[peer] -= 1
            self._outbound_inflight -= 1

    async def peer_received(self, frame: bytes, transport: AuthenticatedPeerTransport):
        if not self.config.enabled:
            return "disabled"
        peer = transport.verified_peer_id
        if self.transports.get(peer) is not transport or not transport.connected:
            raise ValueError("Unrecognized authenticated session")
        e = decode(frame)
        if any(
            type(e[k]) is not int or e[k] < 0
            for k in ("version", "issued_ms", "expires_ms", "hops", "width")
        ):
            raise ValueError("Invalid envelope number")
        if e["version"] != 1 or e["origin"] != peer:
            raise ValueError("Unsupported version or origin")
        if e["hops"] != 1:
            raise PacketRejected("hop_policy", "Unsupported hop count")
        if (
            not isinstance(e["radio"], str)
            or not isinstance(e["raw"], str)
            or len(e["raw"]) > MAX_PACKET * 2
            or re.fullmatch(r"(?:[0-9a-f]{2})+", e["raw"]) is None
        ):
            raise ValueError("Invalid payload fields")
        radio = self.config.inbound.get((peer, e["radio"]))
        if radio is None:
            raise PacketRejected("route", "Unapproved peer route")
        now = int(self.clock() * 1000)
        if not 0 < e["expires_ms"] - e["issued_ms"] <= self.config.ttl_ms:
            raise PacketRejected("lifetime", "Invalid lifetime")
        if e["issued_ms"] > now + 2000 or e["expires_ms"] <= now:
            raise PacketRejected("time", "Expired or future envelope")
        try:
            raw = bytes.fromhex(e["raw"])
        except ValueError as exc:
            raise ValueError("Invalid packet encoding") from exc
        packet, width, packet_id = decode_packet(raw)
        if not supported_peer_packet(packet):
            raise PacketRejected(
                "payload_policy", "Unsupported private transit payload"
            )
        if e["width"] != width or e["packet_id"] != packet_id:
            raise ValueError("Invalid width metadata or packet ID")
        if width not in (2, 3):
            raise PacketRejected("width_policy", "Ineligible width")
        transport.inbound_frame_validated()
        if self._inbound_inflight >= self.config.max_inflight:
            return "busy"
        retention = max(e["expires_ms"], now + self.config.ttl_ms)
        if not self._remember(packet_id, retention, now):
            return "duplicate_or_full"
        self._inbound_inflight += 1
        try:
            if not await self._reserve_replay(packet_id, retention, now):
                return "replay_store_denied"
            if int(self.clock() * 1000) >= e["expires_ms"]:
                return "expired"
            if self.transports.get(peer) is not transport or not transport.connected:
                return "disconnected"
            result = await self.rf_planner(
                RfIngress(raw, radio, "peer", expires_ms=e["expires_ms"])
            )
        finally:
            self._inbound_inflight -= 1
        return "rf_planner_rejected" if result is False else "rf_planner_accepted"
        # Neither status independently proves modem TX or recipient delivery.
