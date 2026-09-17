"""Tests for the self-signed TLS identity used by the Device Gateway."""
from void.device import cert


def test_ensure_cert_creates_key_and_cert_files(tmp_path):
    cert_path, key_path = cert.ensure_cert(tmp_path)
    assert cert_path.exists()
    assert key_path.exists()
    assert cert_path.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" in key_path.read_bytes()


def test_ensure_cert_is_idempotent_same_fingerprint(tmp_path):
    cert_path1, _ = cert.ensure_cert(tmp_path)
    fp1 = cert.fingerprint(cert_path1)
    cert_path2, _ = cert.ensure_cert(tmp_path)   # second call, same dir
    fp2 = cert.fingerprint(cert_path2)
    assert fp1 == fp2
    assert cert_path1 == cert_path2


def test_two_different_dirs_get_different_identities(tmp_path):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    c1, _ = cert.ensure_cert(d1)
    c2, _ = cert.ensure_cert(d2)
    assert cert.fingerprint(c1) != cert.fingerprint(c2)


def test_fingerprint_format_is_colon_separated_hex(tmp_path):
    cert_path, _ = cert.ensure_cert(tmp_path)
    fp = cert.fingerprint(cert_path)
    parts = fp.split(":")
    assert len(parts) == 32   # SHA-256 = 32 bytes
    assert all(len(p) == 2 for p in parts)
    assert all(c in "0123456789ABCDEF" for p in parts for c in p)


def test_fingerprint_from_der_matches_fingerprint_from_pem(tmp_path):
    from cryptography.hazmat.primitives.serialization import Encoding
    from cryptography import x509

    cert_path, _ = cert.ensure_cert(tmp_path)
    pem = cert_path.read_bytes()
    der = x509.load_pem_x509_certificate(pem).public_bytes(Encoding.DER)
    assert cert.fingerprint_from_der(der) == cert.fingerprint(cert_path)
