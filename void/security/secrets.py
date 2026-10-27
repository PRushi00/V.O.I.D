"""Secure secret storage for V.O.I.D.

Wraps the OS keyring so API keys, PINs and tokens are never written to the
repo or to plaintext config. On Windows the default backend is the Windows
Credential Manager (DPAPI-encrypted, per-user). On other platforms keyring
selects an appropriate backend (Keychain, Secret Service, etc.).

All secrets are stored under a single service name so they are easy to audit
and revoke.

**The environment is a fallback for some secrets, and deliberately not for most.**
A headless machine - a container, a CI runner, a server - has no keyring at all, and on one of
those the choice is not "keyring or environment", it is "environment or no credential". So
:func:`get_secret` falls back to an environment variable, but only for the names in
:data:`ENV_FALLBACKS`, which is a closed list holding third-party API credentials and nothing else.

What is NOT on that list matters more than what is. The memory key decrypts the owner's private
memory store, and the stop PIN releases the kill switch; neither may ever be supplied by the
environment, because an environment variable is readable by every child process this program
starts and by anything that can read /proc. A revocable key for somebody else's API is a different
class of secret from the key to the owner's own data, and only the first kind is accepted here.

The keyring is always tried FIRST, so an installed desktop behaves exactly as it did before: the
environment is consulted only when the keyring has nothing, or is not there at all.
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
    try:
        _keyring().set_password(SERVICE, key, value)
    except Exception as exc:  # keyring raises backend-specific errors
        # Never let ``value`` reach the message: this text ends up in logs and on screens.
        hint = ""
        names = ENV_FALLBACKS.get(key, ())
        if names:
            hint = (f" This machine has no usable keyring; set {names[0]} in the environment "
                    f"instead, which V.O.I.D reads when the keyring has nothing.")
        raise SecretStoreError(f"Failed to store secret '{key}': {exc}.{hint}") from exc


def get_secret(key: str) -> str | None:
    """Return a stored secret, or None if it is not set.

    The keyring is asked first and the environment only afterwards, so nothing about an installed
    desktop changes. A missing or broken backend is not an error when the environment carries the
    secret - that is the whole point of the fallback - but it is still an error when it does not,
    so a genuinely broken keyring still reports itself rather than looking like an absent key.
    """
    try:
        value = _keyring().get_password(SERVICE, key)
    except Exception as exc:
        # No keyring, or one that will not answer. Normal for a container or a CI runner, and it
        # must not hide a credential the environment really is carrying.
        fallback = _from_environment(key)
        if fallback is not None:
            return fallback
        if isinstance(exc, SecretStoreError):
            raise
        raise SecretStoreError(f"Failed to read secret '{key}': {exc}") from exc
    if value:
        return value
    return _from_environment(key)


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
MEMORY_KEY = "memory_key"     # 32-byte AES key for void.memory (base64); never leaves the OS store

#: Secrets that may ALSO be supplied through the environment, and the variable names tried in
#: order. A closed list, and short on purpose - see the module docstring for why ``MEMORY_KEY``
#: and ``STOP_PIN`` are absent from it and must stay absent.
ENV_FALLBACKS: dict[str, tuple[str, ...]] = {
    GEMINI_API_KEY: ("VOID_GEMINI_API_KEY", "GEMINI_API_KEY"),
}

#: Where :func:`get_secret` found a secret, for reports that should not imply a key is in the
#: keyring when it is in the environment.
KEYRING = "keyring"
ENVIRONMENT = "environment"
ABSENT = "absent"


def _from_environment(key: str) -> str | None:
    """A secret supplied through the environment, or None when there is none to find."""
    import os
    for name in ENV_FALLBACKS.get(key, ()):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def secret_source(key: str) -> str:
    """Where this secret would come from: :data:`KEYRING`, :data:`ENVIRONMENT` or :data:`ABSENT`.

    For telling the owner the truth about their own installation. "A key is configured" and "a key
    is present" are different claims, and a report that blurs them is the kind of comfortable
    inaccuracy this project exists to avoid.
    """
    try:
        if _keyring().get_password(SERVICE, key):
            return KEYRING
    except Exception:                       # noqa: BLE001 - absent backend is a normal answer here
        pass
    return ENVIRONMENT if _from_environment(key) is not None else ABSENT
