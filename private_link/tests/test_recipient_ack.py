"""Real endpoint decrypt/ACK with in-process link transport; no RF or live TLS."""
import asyncio
import hashlib
import struct
from types import SimpleNamespace

import pytest
from openhop_core.node.handlers.text import TextMessageHandler
from openhop_core.protocol import Packet, PacketBuilder, PathUtils
from openhop_core.protocol.identity import Identity, LocalIdentity
from private_link.tests.test_adapter import pair


@pytest.mark.asyncio
@pytest.mark.parametrize('width', [2, 3])
async def test_decrypted_dm_recipient_ack_crosses_private_link(width):
    # Reuse the existing public disposable fixture seeds, not persistent identities.
    sender = LocalIdentity(bytes.fromhex('11' * 32))
    recipient = LocalIdentity(bytes.fromhex('22' * 32))
    text, timestamp = b'island recipient fixture', 1234
    secret = Identity(recipient.get_public_key()).calc_shared_secret(sender.get_private_key())
    dm = PacketBuilder.create_datagram(2, recipient, sender, secret,
        struct.pack('<IB', timestamp, 0) + text + b'\0')
    # At the recipient the direct route is already consumed. Routing itself is
    # covered by the native Dispatcher check, not simulated as a success here.
    dm.set_path(b'', PathUtils.encode_path_len(width, 0))
    responses = asyncio.Queue()
    async def send(packet, **kwargs):
        await responses.put(packet)
    path = bytes.fromhex('abcdef')[:width]
    contact = SimpleNamespace(public_key=sender.get_public_key(), name='fixture',
        out_path=path, out_path_len=PathUtils.encode_path_len(width, 1))
    handler = TextMessageHandler(recipient, SimpleNamespace(contacts=[contact]),
        lambda _: None, send)
    assert (await handler(dm)).authenticated
    assert dm.decrypted['text'] == text.decode()
    ack = await asyncio.wait_for(responses.get(), 2)
    assert ack.get_payload_type() == 3 and len(ack.payload) == 6
    assert bytes(ack.payload[:4]) == hashlib.sha256(
        struct.pack('<IB', timestamp, 0) + text + sender.get_public_key()).digest()[:4]
    assert ack.get_path_hash_size() == width
    a, b, ab, ba, calls, _ = pair()
    assert (await b.radio_received(ack.write_to(), 'rf'))[1] == 'transport_accepted'
    assert await a.peer_received(ba.frames[0], ab) == 'rf_planner_accepted'
    observed = Packet(); assert observed.read_from(calls['a'][-1].raw)
    assert bytes(observed.payload) == bytes(ack.payload)
    assert observed.get_path_hash_size() == width
