"""Self-signed TLS identity for the Device Gateway.

There is no certificate authority for a laptop's LAN/hotspot IP, so trust is
established the same way SSH host keys are: the client PINS the server's
certificate fingerprint, learned out-of-band (typed/scanned in during
pairing - see void.device.pairing) instead of validated against a CA chain.
This is a deliberate, narrow, documented substitution for CA validation, not
a disabled check: the fingerprint the client pins is exactly what protects
against a man-in-the-middle both during and after pairing.

The private key never leaves this machine and is never logged. It uses
the ``cryptography`` package (already a transitive dependency of this
project's stack; declared directly in requirements.txt because this module
uses it directly), never a home-rolled crypto primitive.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import stat
from pathlib import Path

CERT_FILENAME = "device_cert.pem"
KEY_FILENAME = "device_key.pem"
_VALIDITY_DAYS = 3650  # ten years - a personal, non-rotated, pinned identity


def _generate(key_path: Path, cert_path: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "void-device-gateway"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path.write_bytes(key_bytes)
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # best-effort on Windows
    except OSError:
        pass
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_cert(state_dir: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a persistent self-signed
    identity on first use. Idempotent - reuses the existing key/cert on every
    later call so the fingerprint a device pinned at pairing time keeps
    matching."""
    key_path = state_dir / KEY_FILENAME
    cert_path = state_dir / CERT_FILENAME
    if not key_path.exists() or not cert_path.exists():
        _generate(key_path, cert_path)
    return cert_path, key_path


def fingerprint(cert_path: Path) -> str:
    """Colon-separated uppercase hex SHA-256 fingerprint of the DER-encoded
    certificate - the value a device pins during pairing and on every later
    connection. Same format as familiar TLS fingerprint displays."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    pem = cert_path.read_bytes()
    cert = x509.load_pem_x509_certificate(pem)
    digest = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in digest)


def fingerprint_from_der(der_bytes: bytes) -> str:
    """Fingerprint of a raw DER certificate blob (used by a client verifying
    the server's presented certificate against a pinned value)."""
    digest = hashlib.sha256(der_bytes).digest()
    return ":".join(f"{b:02X}" for b in digest)
