"""Stage one explicitly approved client enrollment. Never modifies a live config.

Signing creates a credential. Run only after approval for the specific request.
Deployment of candidate.yaml remains a separate host-owner operation.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def spki(key):
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def validate_request(request):
    if set(request) != {'version', 'origin', 'csr_pem'} or request['version'] != 1:
        raise ValueError('Invalid enrollment request schema')
    if not isinstance(request['csr_pem'], str) or len(request['csr_pem']) > 4096:
        raise ValueError('Invalid CSR size')
    csr = x509.load_pem_x509_csr(request['csr_pem'].encode())
    key = csr.public_key()
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('Expected P-256 key')
    if not csr.is_signature_valid:
        raise ValueError('Invalid CSR signature')
    origin = 'device-' + hashlib.sha256(spki(key)).hexdigest()[:24]
    expected = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, origin)])
    if request['origin'] != origin or csr.subject != expected:
        raise ValueError('CSR identity does not match public key')
    return csr, origin


def stage_routes(config, origin, fingerprint):
    result = copy.deepcopy(config)
    island = result['private_island']
    if not island.get('enabled') or island.get('node_id') == origin:
        raise ValueError('Private island must be enabled with a distinct host identity')
    if 'host-rf' not in {r['id'] for r in result.get('radios', [])}:
        raise ValueError('Required host-rf radio is missing')
    peers = island['transport']['peer_fingerprints']
    previous = peers.get(origin)
    if previous is not None and previous != fingerprint:
        raise ValueError('Existing identity has a different certificate')
    if origin not in peers and len(peers) >= 16:
        raise ValueError('Peer capacity of 16 reached')
    if any(p != origin and fp == fingerprint for p, fp in peers.items()):
        raise ValueError('Certificate already belongs to another peer')
    expected = {'peer': origin, 'radio': 'remote-rf', 'local_radio': 'host-rf'}
    inbound = island.setdefault('inbound', [])
    existing = [r for r in inbound if r.get('peer') == origin]
    if existing and existing != [expected]:
        raise ValueError('Existing routes conflict with enrollment template')
    if not existing:
        if len(inbound) >= 32:
            raise ValueError('Inbound route capacity reached')
        inbound.append(expected)
    outbound = island.setdefault('outbound', {})
    if any(r != 'host-rf' and origin in ids for r, ids in outbound.items()):
        raise ValueError('Existing outbound route conflicts with enrollment template')
    destinations = outbound.setdefault('host-rf', [])
    if origin not in destinations:
        if len(destinations) >= 16:
            raise ValueError('Outbound peer capacity reached')
        destinations.append(origin)
    peers[origin] = fingerprint
    return result


def issue(csr, ca, ca_key, days, now, *, no_expiration=False):
    if spki(ca_key.public_key()) != spki(ca.public_key()):
        raise ValueError('CA key does not match certificate')
    if not ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError('Issuer is not a CA')
    if not ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
        raise ValueError('Issuer cannot sign certificates')
    if not ca.not_valid_before_utc <= now < ca.not_valid_after_utc:
        raise ValueError('CA is not currently valid')
    if days < 1 or days > 365:
        raise ValueError('Lifetime must be 1 to 365 days')
    expires = ca.not_valid_after_utc if no_expiration else min(now + timedelta(days=days), ca.not_valid_after_utc)
    if expires < now + timedelta(days=1):
        raise ValueError('CA expires in less than one day; renew trust first')
    return (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(ca.subject)
        .public_key(csr.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(max(now-timedelta(minutes=5), ca.not_valid_before_utc)).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256()))


def enroll(request, config, ca, ca_key, output, days=30, now=None, *, no_expiration=False):
    """Persist certificate before candidate config so interrupted retries reuse it."""
    now = now or datetime.now(timezone.utc)
    csr, origin = validate_request(request)
    output = Path(output)
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.is_symlink() or output.stat().st_mode & 0o077:
        raise ValueError('Output directory must be private, mode 0700')
    approval_path = output / 'approval.json'
    if approval_path.exists():
        approval = json.loads(approval_path.read_text())
        cert = x509.load_pem_x509_certificate(approval['certificate_pem'].encode())
        if approval['origin'] != origin or spki(cert.public_key()) != spki(csr.public_key()):
            raise ValueError('Output directory belongs to a different enrollment')
        cert.verify_directly_issued_by(ca)
        if cert.subject != csr.subject or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc:
            raise ValueError('Saved certificate is invalid or expired')
    else:
        # Validate capacity/conflicts before signing a credential.
        if origin in config['private_island']['transport']['peer_fingerprints']:
            raise ValueError('Already enrolled; recover original approval instead of issuing another certificate')
        stage_routes(config, origin, '0' * 64)
        cert = issue(csr, ca, ca_key, days, now, no_expiration=no_expiration)
        approval = {'version': 1, 'origin': origin,
                    'certificate_pem': cert.public_bytes(serialization.Encoding.PEM).decode()}
        # Publish a complete file without replacing a concurrent enrollment.
        fd, temporary = tempfile.mkstemp(prefix='.approval-', dir=output)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(approval, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, approval_path)
        finally:
            os.unlink(temporary)
    candidate = stage_routes(config, origin, cert.fingerprint(hashes.SHA256()).hex())
    fd, temp = tempfile.mkstemp(prefix='.candidate-', dir=output)
    try:
        with os.fdopen(fd, 'w') as stream:
            yaml.safe_dump(candidate, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, output / 'candidate.yaml')
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return approval, candidate, cert.not_valid_after_utc


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for option in ('request', 'config', 'ca-cert', 'ca-key', 'output'):
        p.add_argument('--' + option, type=Path, required=True)
    p.add_argument('--days', type=int, default=30)
    args = p.parse_args()
    os.umask(0o077)
    ca = x509.load_pem_x509_certificate(args.ca_cert.read_bytes())
    key = serialization.load_pem_private_key(args.ca_key.read_bytes(), password=None)
    approval, _, expires = enroll(json.loads(args.request.read_text()),
        yaml.safe_load(args.config.read_text()), ca, key, args.output, args.days)
    print('Staged', approval['origin'], 'expires', expires.isoformat())
    print('No live configuration changed. Host owner must review and deploy candidate.yaml.')


if __name__ == '__main__':
    main()
