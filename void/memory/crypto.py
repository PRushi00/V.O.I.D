"""Encryption for persistent memory (V2.0 T3.2).

No custom cryptography: AES-256-GCM from the ``cryptography`` package, which V.O.I.D
already depends on. The 32-byte key is generated once with ``os.urandom`` and kept in the
OS credential store (Windows Credential Manager) under ``memory_key`` - the same trust
root as every other V.O.I.D secret. It is never written to the repo, config, database or
logs.

Every ciphertext is bound (as AEAD associated data) to the row's identity and to the
security-relevant metadata (kind, origin, status, sensitivity, cloud flag). So swapping
ciphertexts between rows, or editing a row's ``status``/``origin`` in the database file to
promote a quarantined item to trusted, makes decryption fail instead of silently working.

Failure is explicit and never falls back to plaintext:
  * key missing while ciphertext exists -> ``MemoryUnavailable("key_missing")``
  * key present but wrong               -> ``MemoryUnavailable("key_wrong")``
A missing key is NEVER regenerated over existing ciphertext.
"""
from __future__ import annotations

import base64
import binascii
import os

KEY_BYTES = 32
NONCE_BYTES = 12
SCHEMA_VERSION = 1


class MemoryUnavailable(RuntimeError):
    """Memory cannot be used safely. ``code`` is a stable, content-free reason:
    key_missing | key_wrong | key_invalid | keystore_unavailable | db_corrupt |
    schema_newer | disabled."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DecryptionError(Exception):
    """One ciphertext failed authentication (tampered, corrupt, wrong row, wrong key)."""


def new_key() -> bytes:
    return os.urandom(KEY_BYTES)


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii")


def decode_key(text: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(text.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise MemoryUnavailable("key_invalid", "The stored memory key is not valid.") from exc
    if len(key) != KEY_BYTES:
        raise MemoryUnavailable("key_invalid", "The stored memory key has the wrong length.")
    return key


def item_aad(item_id: str, kind: str, origin: str, status: str, sensitivity: str,
             cloud_ok: int) -> bytes:
    return f"void-memory|v{SCHEMA_VERSION}|{item_id}|{kind}|{origin}|{status}|{sensitivity}|{int(cloud_ok)}".encode()


KEY_CHECK_AAD = b"void-memory|key-check"
KEY_CHECK_PLAINTEXT = "void-memory-key-check"


class MemoryCipher:
    def __init__(self, key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if len(key) != KEY_BYTES:
            raise MemoryUnavailable("key_invalid", "The memory key has the wrong length.")
        self._aead = AESGCM(key)

    def encrypt(self, text: str, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, text.encode("utf-8"), aad)

    def decrypt(self, blob: bytes, aad: bytes) -> str:
        from cryptography.exceptions import InvalidTag

        if not isinstance(blob, (bytes, bytearray)) or len(blob) < NONCE_BYTES + 16:
            raise DecryptionError("ciphertext too short")
        try:
            return self._aead.decrypt(bytes(blob[:NONCE_BYTES]), bytes(blob[NONCE_BYTES:]), aad).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise DecryptionError("authentication failed") from exc


class KeyringKeyProvider:
    """The production key source: the OS credential store via ``void.security.secrets``."""

    def __init__(self, name: str | None = None):
        from void.security import secrets

        self._secrets = secrets
        self._name = name or secrets.MEMORY_KEY

    def get(self) -> bytes | None:
        try:
            raw = self._secrets.get_secret(self._name)
        except self._secrets.SecretStoreError as exc:
            raise MemoryUnavailable("keystore_unavailable", "The credential store is unavailable.") from exc
        return decode_key(raw) if raw else None

    def create(self) -> bytes:
        key = new_key()
        try:
            self._secrets.set_secret(self._name, encode_key(key))
        except self._secrets.SecretStoreError as exc:
            raise MemoryUnavailable("keystore_unavailable", "The credential store is unavailable.") from exc
        return key
