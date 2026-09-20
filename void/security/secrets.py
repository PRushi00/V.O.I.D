"""Secure secret storage for V.O.I.D.

Wraps the OS keyring so API keys, PINs and tokens are never written to the
repo or to plaintext config. On Windows the default backend is the Windows
Credential Manager (DPAPI-encrypted, per-user). On other platforms keyring
selects an appropriate backend (Keychain, Secret Service, etc.).

All secrets are stored under a single service name so they are easy to audit
and revoke.
"""
from __future__ import annotations

SERVICE = "void"


class SecretStoreError(RuntimeError):
    """Raised when the secret backend is unavailable or a write fails."""


def _keyring():
    try:
        import keyring  # imported lazily so the package imports without it
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SecretStoreError(
            "The 'keyring' package is required for secret storage. "
            "Install it with: pip install keyring"
        ) from exc
    return keyring


def set_secret(key: str, value: str) -> None:
    """Store (or overwrite) a secret under service 'void'."""
    kr = _keyring()
    try:
        kr.set_password(SERVICE, key, value)
    except Exception as exc:  # keyring raises backend-specific errors
        raise SecretStoreError(f"Failed to store secret '{key}': {exc}") from exc


def get_secret(key: str) -> str | None:
    """Return a stored secret, or None if it is not set."""
    kr = _keyring()
    try:
        return kr.get_password(SERVICE, key)
    except Exception as exc:  # pragma: no cover - backend dependent
        raise SecretStoreError(f"Failed to read secret '{key}': {exc}") from exc


def delete_secret(key: str) -> bool:
    """Delete a secret. Returns True if it existed, False otherwise."""
    kr = _keyring()
    try:
        kr.delete_password(SERVICE, key)
        return True
    except kr.errors.PasswordDeleteError:
        return False
    except Exception as exc:  # pragma: no cover - backend dependent
        raise SecretStoreError(f"Failed to delete secret '{key}': {exc}") from exc


# Well-known secret keys used across the app.
GEMINI_API_KEY = "gemini_api_key"
STOP_PIN = "stop_pin"
