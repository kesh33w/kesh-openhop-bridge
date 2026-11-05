"""Optional mutual-TLS host service. Importing or constructing never opens sockets."""

import asyncio
import hashlib
import logging
import re
import ssl
from collections import Counter
from pathlib import Path

from .adapter import MAX_FRAME, AuthenticatedPeerTransport, PacketRejected, decode


class PeerStream(AuthenticatedPeerTransport):
    """Stable adapter identity across sessions; no offline buffer or retransmission."""

    def __init__(self, peer_id, fingerprint):
        self.verified_peer_id = peer_id
        self._writer = None
        self._fingerprint = fingerprint
        self._lock = asyncio.Lock()

    @property
    def verified_peer_id(self):
        return self._peer_id

    @verified_peer_id.setter
    def verified_peer_id(self, value):
        self._peer_id = value

    @property
    def connected(self):
        return self._writer is not None and not self._writer.is_closing()

    def attach(self, writer):
        tls = writer.get_extra_info("ssl_object")
        certificate = tls.getpeercert(binary_form=True) if tls is not None else None
        if (
            tls is None
            or not certificate
            or tls.version() not in ("TLSv1.2", "TLSv1.3")
            or hashlib.sha256(certificate).hexdigest() != self._fingerprint
        ):
            return False
        previous = self._writer
        self._writer = writer
        if previous is not None and previous is not writer:
            previous.close()
        return True

    def owns(self, writer):
        return self._writer is writer and self.connected

    def detach(self, writer):
        if self._writer is writer:
            self._writer = None
        writer.close()

    def abort(self):
        if self._writer is not None:
            self.detach(self._writer)

    def abort_after_send_failure(self):
        # send() closes its captured writer on every failure/cancellation.
        # The current writer may already be an authenticated replacement.
        pass

    async def send(self, frame):
        decode(frame)  # Bounds and complete framing before writing any bytes.
        async with self._lock:
            writer = self._writer
            if not self.owns(writer):
                raise ConnectionError("Authenticated peer is disconnected")
            try:
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), timeout=2)
                if not self.owns(writer):
                    raise ConnectionError("Peer session changed during send")
            except BaseException:
                # Partial/uncertain writes must not be replayed on a new session.
                self.detach(writer)
                raise


class TlsPeerService:
    def __init__(self, settings):
        allowed = {
            "enabled",
            "bind_host",
            "port",
            "ca_file",
            "certificate_file",
            "private_key_file",
            "peer_fingerprints",
            "frame_timeout",
            "idle_timeout",
        }
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("Unknown private TLS service setting")
        self.settings = dict(settings)
        enabled = settings.get("enabled", False)
        if type(enabled) is not bool:
            raise ValueError("TLS enabled must be boolean")
        self.enabled = enabled
        pins = settings.get("peer_fingerprints", {})
        if not isinstance(pins, dict) or len(pins) > 16:
            raise ValueError(
                "TLS peers must be an explicit bounded fingerprint mapping"
            )
        self._peers_by_pin = {}
        self.transports = {}
        for peer, pin in pins.items():
            if (
                not isinstance(peer, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", peer) is None
            ):
                raise ValueError("Invalid TLS peer identity")
            if (
                not isinstance(pin, str)
                or re.fullmatch(r"[0-9a-fA-F]{64}", pin) is None
            ):
                raise ValueError("Invalid peer certificate fingerprint")
            pin = pin.lower()
            if pin in self._peers_by_pin:
                raise ValueError("A certificate cannot identify multiple peers")
            self._peers_by_pin[pin] = peer
            self.transports[peer] = PeerStream(peer, pin)
        self.bind_host = settings.get("bind_host", "127.0.0.1")
        self.port = settings.get("port")
        self.frame_timeout = settings.get("frame_timeout", 5)
        self.idle_timeout = settings.get("idle_timeout", 300)
        for value, low, high in (
            (self.frame_timeout, 0.1, 30),
            (self.idle_timeout, 1, 3600),
        ):
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError("Invalid TLS frame or idle timeout")
        if enabled:
            if (
                not isinstance(self.bind_host, str)
                or not self.bind_host
                or len(self.bind_host) > 253
            ):
                raise ValueError("Explicit TLS bind address required")
            if type(self.port) is not int or not 1 <= self.port <= 65535 or not pins:
                raise ValueError("TLS port and approved peer fingerprints required")
            for field in ("ca_file", "certificate_file", "private_key_file"):
                value = settings.get(field)
                if not isinstance(value, str) or not Path(value).is_absolute():
                    raise ValueError(
                        "TLS credential files must be explicit absolute paths"
                    )
        self.receiver = None
        self._server = None
        self._tasks = set()
        self._closing = False
        self.failures = 0
        self.rejections = Counter()

    def _context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.options |= ssl.OP_NO_COMPRESSION | ssl.OP_NO_TICKET
        try:
            context.load_verify_locations(cafile=self.settings["ca_file"])
            context.load_cert_chain(
                self.settings["certificate_file"], self.settings["private_key_file"]
            )
        except (OSError, ssl.SSLError):
            raise ValueError("Private TLS credentials could not be loaded") from None
        return context

    async def start(self):
        if not self.enabled:
            return False
        if self._server is not None:
            raise RuntimeError("Private TLS service already started")
        if not callable(self.receiver):
            raise ValueError(
                "Validated peer receiver must be installed before listening"
            )
        context = self._context()  # All validation precedes socket creation.
        self._closing = False
        self._server = await asyncio.start_server(
            self._accept,
            self.bind_host,
            self.port,
            ssl=context,
            ssl_handshake_timeout=5,
            ssl_shutdown_timeout=2,
            limit=MAX_FRAME + 4,
            backlog=16,
        )
        return True

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self._tasks.add(task)
        transport = None
        try:
            tls = writer.get_extra_info("ssl_object")
            if (
                self._closing
                or tls is None
                or tls.version() not in ("TLSv1.2", "TLSv1.3")
            ):
                return
            certificate = tls.getpeercert(binary_form=True)
            if not certificate:
                return
            peer = self._peers_by_pin.get(hashlib.sha256(certificate).hexdigest())
            if peer is None:
                return
            transport = self.transports[peer]
            if not transport.attach(writer):
                return  # Only the same approved certificate can replace this peer.
            logging.getLogger(__name__).info("Private peer connected: %s", peer)
            while transport.owns(writer) and not self._closing:
                first = await asyncio.wait_for(reader.readexactly(1), self.idle_timeout)
                rest = await asyncio.wait_for(reader.readexactly(3), self.frame_timeout)
                header = first + rest
                size = int.from_bytes(header, "big")
                if not 0 < size <= MAX_FRAME:
                    return
                body = await asyncio.wait_for(
                    reader.readexactly(size), self.frame_timeout
                )
                if not transport.owns(writer):
                    return
                try:
                    result = await self.receiver(header + body, transport)
                except PacketRejected as exc:
                    # Only explicit packet-policy failures leave the stream usable.
                    result = exc.reason
                if result not in (None, "rf_planner_accepted"):
                    # Fixed reasons only. Never emit wire data or exception text.
                    reason = (
                        result
                        if result
                        in {
                            "route",
                            "lifetime",
                            "time",
                            "payload_policy",
                            "width_policy",
                            "hop_policy",
                            "busy",
                            "duplicate_or_full",
                            "replay_store_denied",
                            "expired",
                            "disconnected",
                            "disabled",
                            "rf_planner_rejected",
                        }
                        else "rejected"
                    )
                    self.rejections[reason] += 1
                    logging.getLogger(__name__).info(
                        "Private packet rejected: %s", reason
                    )
        except Exception:
            self.failures += 1
            logging.getLogger(__name__).warning("Private peer session failed")
        finally:
            if transport is not None:
                transport.detach(writer)
                logging.getLogger(__name__).info(
                    "Private peer disconnected: %s", transport.verified_peer_id
                )
            else:
                writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except (OSError, ConnectionError, TimeoutError):
                pass
            finally:
                self._tasks.discard(task)

    async def close(self):
        self._closing = True
        if self._server is not None:
            self._server.close()
        for transport in self.transports.values():
            if transport._writer is not None:
                transport.detach(transport._writer)
        tasks = tuple(
            task for task in self._tasks if task is not asyncio.current_task()
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None


def create_service(settings):
    return TlsPeerService(settings)
