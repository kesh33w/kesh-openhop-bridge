"""Mock TLS stream decisions only; no certificates, keys, listeners or network."""

import asyncio
import hashlib
import ssl

import pytest
from private_link.adapter import encode
from private_link.transport import TlsPeerService

CERT = b"TEST_ONLY_NON_CERTIFICATE_BYTES"
PIN = hashlib.sha256(CERT).hexdigest()


class MockTls:
    def __init__(self, certificate=CERT, version="TLSv1.2"):
        self.certificate, self.protocol = certificate, version

    def getpeercert(self, binary_form):
        assert binary_form
        return self.certificate

    def version(self):
        return self.protocol


class Writer:
    def __init__(self, tls=None):
        self.tls = tls
        self.closed = False
        self.frames = []
        self.block = False

    def get_extra_info(self, name):
        assert name == "ssl_object"
        return self.tls

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return

    def write(self, frame):
        self.frames.append(frame)

    async def drain(self):
        if self.block:
            await asyncio.Future()


def service(**settings):
    return TlsPeerService({"peer_fingerprints": {"peer": PIN}, **settings})


def frame():
    return encode(
        dict(
            version=1,
            origin="peer",
            radio="rf",
            packet_id="00" * 32,
            issued_ms=1000,
            expires_ms=2000,
            hops=1,
            width=2,
            raw="154001",
        )
    )


@pytest.mark.asyncio
async def test_disabled_service_never_creates_socket(monkeypatch):
    async def forbidden(*a, **kw):
        raise AssertionError("Socket creation forbidden")

    monkeypatch.setattr(asyncio, "start_server", forbidden)
    s = service()
    assert await s.start() is False
    await s.close()


@pytest.mark.asyncio
async def test_credential_failure_precedes_socket_creation(monkeypatch):
    async def forbidden(*a, **kw):
        raise AssertionError("Socket creation forbidden")

    monkeypatch.setattr(asyncio, "start_server", forbidden)
    s = service(
        enabled=True,
        port=7443,
        ca_file="/missing/ca",
        certificate_file="/missing/cert",
        private_key_file="/missing/key",
    )
    s.receiver = lambda *_: None
    with pytest.raises(ValueError, match="credentials could not be loaded"):
        await s.start()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tls", [None, MockTls(None), MockTls(b"wrong"), MockTls(version="TLSv1.1")]
)
async def test_unverified_unknown_or_obsolete_session_never_attaches(tls):
    s = service()
    reader = asyncio.StreamReader()
    writer = Writer(tls)
    await s._accept(reader, writer)
    assert writer.closed and not s.transports["peer"].connected
    assert not s._tasks


@pytest.mark.asyncio
async def test_fragmented_frames_stable_identity_and_wrong_pin_rejection():
    s = service()
    received = []
    event = asyncio.Event()

    async def receive(data, transport):
        received.append((data, transport))
        event.set()

    s.receiver = receive
    reader = asyncio.StreamReader()
    writer = Writer(MockTls())
    task = asyncio.create_task(s._accept(reader, writer))
    await asyncio.sleep(0)
    other = Writer(MockTls(b"wrong"))
    await s._accept(asyncio.StreamReader(), other)
    assert other.closed and not writer.closed
    wire = frame()
    for part in (wire[:1], wire[1:3], wire[3:8], wire[8:]):
        reader.feed_data(part)
        await asyncio.sleep(0)
    await asyncio.wait_for(event.wait(), 1)
    assert received == [(wire, s.transports["peer"])]
    await s.transports["peer"].send(wire)
    assert writer.frames == [wire]
    reader.feed_eof()
    await task
    assert not s.transports["peer"].connected
    await s.close()


@pytest.mark.asyncio
async def test_oversize_header_and_partial_timeout_close_without_body_allocation():
    for data in ((2049).to_bytes(4, "big"), b"\x00"):
        s = service(frame_timeout=0.1)
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        writer = Writer(MockTls())
        await asyncio.wait_for(s._accept(reader, writer), 1)
        assert writer.closed and not s.transports["peer"].connected


@pytest.mark.asyncio
async def test_cancelled_uncertain_send_disconnects_without_replay():
    s = service()
    t = s.transports["peer"]
    writer = Writer(MockTls())
    writer.block = True
    assert t.attach(writer)
    task = asyncio.create_task(t.send(frame()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed and not t.connected and len(writer.frames) == 1
    replacement = Writer(MockTls())
    assert t.attach(replacement)
    assert not replacement.frames
    await s.close()


@pytest.mark.parametrize(
    "settings",
    [
        {"enabled": "yes"},
        {"peer_fingerprints": {"peer": "bad"}},
        {"peer_fingerprints": {"a": PIN, "b": PIN}},
        {"frame_timeout": True},
        {"enabled": True, "port": 0},
        {"unknown": 1},
    ],
)
def test_invalid_configuration_rejected(settings):
    with pytest.raises(ValueError):
        service(**settings)


def test_context_requires_client_certificates_and_tls12(monkeypatch):
    class Context:
        options = 0

        def __init__(self, protocol):
            assert protocol == ssl.PROTOCOL_TLS_SERVER

        def load_verify_locations(self, *, cafile):
            self.ca = cafile

        def load_cert_chain(self, certificate, key):
            self.chain = (certificate, key)

    monkeypatch.setattr(ssl, "SSLContext", Context)
    s = service(
        enabled=True,
        port=7443,
        ca_file="/test-only/ca",
        certificate_file="/test-only/cert",
        private_key_file="/test-only/key",
    )
    context = s._context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.options & ssl.OP_NO_TICKET
    assert context.ca == "/test-only/ca"


@pytest.mark.asyncio
async def test_authenticated_reconnect_replaces_half_open_writer():
    s = service()
    t = s.transports['peer']
    old = Writer(MockTls())
    replacement = Writer(MockTls())
    assert t.attach(old)
    assert t.attach(replacement)
    assert old.closed and t.owns(replacement)
    # The old read task's eventual cleanup must not detach its replacement.
    t.detach(old)
    assert t.owns(replacement)
    await s.close()


@pytest.mark.asyncio
async def test_service_closes_clients_before_waiting_for_server():
    s = service()
    writer = Writer(MockTls())
    assert s.transports['peer'].attach(writer)

    class Server:
        def close(self):
            pass

        async def wait_closed(self):
            # Python 3.12+ waits for active connections to close here.
            assert writer.closed, 'Server wait entered with a live client'

    s._server = Server()
    await s.close()
    assert not s.transports['peer'].connected


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_old_send_does_not_abort_authenticated_replacement(cancel):
    from private_link.adapter import Adapter, Config
    from test_adapter import wire

    s = service()
    t = s.transports['peer']
    old = Writer(MockTls())
    started = asyncio.Event()
    release = asyncio.Event()

    async def drain():
        started.set()
        await release.wait()
        raise ConnectionError('Old session disconnected')

    old.drain = drain
    assert t.attach(old)

    async def local(_):
        return 'local'

    adapter = Adapter(Config('local', enabled=True, outbound={'rf': 'peer'}, inbound={('peer', 'rf'): 'rf'}),
                      local, {'peer': t}, clock=lambda: 100)
    sending = asyncio.create_task(adapter.radio_received(wire(), 'rf'))
    await asyncio.wait_for(started.wait(), 1)
    replacement = Writer(MockTls())
    assert t.attach(replacement)
    if cancel:
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    else:
        release.set()
        assert (await sending)[1] == 'transport_failed_or_uncertain'
    assert t.owns(replacement) and not replacement.frames
    assert len(old.frames) == 1
    await s.close()
