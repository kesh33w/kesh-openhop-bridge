"""Automatic public enrollment on the existing TLS port; data still requires mTLS."""
import asyncio
from datetime import timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from host_enrollment import enroll, validate_request
from private_link.transport import TlsPeerService, PeerStream
from private_link.daemon import _configuration


class EnrollmentController:
    def __init__(self, config_path, ca_cert, ca_key, state_dir):
        self.config_path = Path(config_path)
        self.state_dir = Path(state_dir)
        self.ca = x509.load_pem_x509_certificate(Path(ca_cert).read_bytes())
        self.key = serialization.load_pem_private_key(Path(ca_key).read_bytes(), None)
        self.lock = asyncio.Lock()

    async def accept(self, request, service):
        _, origin = validate_request(request)
        async with self.lock:
            current = yaml.safe_load(self.config_path.read_text())
            approval, candidate, expires = enroll(request, current, self.ca, self.key,
                self.state_dir / origin, days=365, no_expiration=True)
            _, route_config, _ = _configuration(SimpleNamespace(config=candidate))
            adapter = service.receiver.__self__
            fingerprint = x509.load_pem_x509_certificate(
                approval['certificate_pem'].encode()).fingerprint(hashes.SHA256()).hex()
            existing = service.transports.get(origin)
            if existing is not None and existing._fingerprint != fingerprint:
                raise ValueError('Existing live identity conflict')
            # Publish disk state before acknowledging; restart or retry recovers it.
            if current != candidate:
                fd, name = tempfile.mkstemp(prefix='.enroll-', dir=self.config_path.parent)
                try:
                    with os.fdopen(fd, 'w') as stream:
                        yaml.safe_dump(candidate, stream, sort_keys=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.config_path)
                    fd = os.open(self.config_path.parent, os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            # No await between map changes: the event loop sees complete routing.
            if existing is None:
                existing = PeerStream(origin, fingerprint)
                service.transports[origin] = existing
            service._peers_by_pin[fingerprint] = origin
            service.settings['peer_fingerprints'] = dict(candidate['private_island']['transport']['peer_fingerprints'])
            adapter.transports[origin] = existing
            adapter._send_locks.setdefault(origin, asyncio.Lock())
            adapter.config = route_config
            # CLI/config saves must retain enrollments admitted since startup.
            adapter.rf_planner.__self__.daemon.config['private_island'] = candidate['private_island']
            return {**approval, 'expires_utc': expires.isoformat(),
                    'ca_expires_utc': self.ca.not_valid_after_utc.isoformat()}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate field')
        result[key] = value
    return result


class EnrollmentPeerService(TlsPeerService):
    def __init__(self, settings, controller):
        super().__init__(settings)
        self.enrollment = controller
        self.enrollment_active = 0

    def _context(self):
        context = super()._context()
        # Missing client cert may enter HTTP enrollment only. A supplied cert
        # still must verify; base _accept and PeerStream enforce data identity.
        context.verify_mode = ssl.CERT_OPTIONAL
        context.set_alpn_protocols(['http/1.1'])
        return context

    async def _accept(self, reader, writer):
        tls = writer.get_extra_info('ssl_object')
        if tls is None or tls.getpeercert(binary_form=True) or tls.selected_alpn_protocol() != 'http/1.1':
            return await super()._accept(reader, writer)
        task = asyncio.current_task()
        self._tasks.add(task)
        admitted = not self._closing and self.enrollment_active < 4
        if admitted:
            self.enrollment_active += 1
        try:
            if not admitted:
                return await self._reply(writer, 503, {'error': 'busy'})
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            if len(header) > 2048:
                raise ValueError('Headers too large')
            lines = header.decode('ascii').split('\r\n')
            if lines[0] != 'POST /enroll HTTP/1.1':
                return await self._reply(writer, 404, {'error': 'not_found'})
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(':', 1)
                name = name.lower()
                if name in headers:
                    raise ValueError('Duplicate header')
                headers[name] = value.strip()
            if 'transfer-encoding' in headers or headers.get('content-type') != 'application/json':
                raise ValueError('Unsupported request')
            size = int(headers.get('content-length', '0'))
            if not 1 <= size <= 8192:
                raise ValueError('Invalid request size')
            body = await asyncio.wait_for(reader.readexactly(size), 5)
            request = json.loads(body, object_pairs_hook=unique_object)
            approval = await self.enrollment.accept(request, self)
            await self._reply(writer, 200, approval)
        except (ValueError, KeyError, TypeError, UnicodeError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError):
            await self._reply(writer, 400, {'error': 'invalid_request'})
        except (OSError, TimeoutError):
            pass
        except Exception:
            # Never emit CSR, certificate/private-key values or exception text.
            await self._reply(writer, 503, {'error': 'enrollment_unavailable'})
        finally:
            if admitted:
                self.enrollment_active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, TimeoutError):
                pass
            self._tasks.discard(task)

    async def _reply(self, writer, status, data):
        body = json.dumps(data, separators=(',', ':')).encode()
        reasons = {200: 'OK', 400: 'Bad Request', 404: 'Not Found', 503: 'Service Unavailable'}
        writer.write((f'HTTP/1.1 {status} {reasons[status]}\r\nContent-Type: application/json\r\n'
                      f'Content-Length: {len(body)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n').encode() + body)
        await asyncio.wait_for(writer.drain(), 5)
