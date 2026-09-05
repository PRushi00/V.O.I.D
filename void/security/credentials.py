"""Multiple named Gemini credentials over the existing OS keyring.

:class:`CredentialPool` manages an ordered set of credential *references*
(names) plus per-credential availability/cooldown state. It never stores, logs,
or exposes secret values: a value is read from the keyring on demand (via
:mod:`void.security.secrets`) and handed straight to the caller, never retained
on a long-lived object.

Backward compatibility: with only the primary key (``gemini_api_key``) stored
and no manifest, the pool exposes exactly that one credential - identical to the
original single-key setup.

The manifest (keyring entry ``gemini_api_key_names``) holds ONLY credential
names, never values, because keyring offers no portable way to enumerate the
keys stored under a service.

This module deliberately does NOT talk to any Gemini API or decide rotation
policy; it only tracks references and availability. Provider integration
(429 rotation) is a later, separate phase.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from void.security import secrets

# Keyring entry holding the JSON list of credential names (names only).
MANIFEST_KEY = "gemini_api_key_names"


class CredentialsExhausted(RuntimeError):
    """Raised when every configured credential is currently unavailable."""


class CredentialMissing(RuntimeError):
    """Raised when a referenced credential has no value in the secret store."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Credential:
    """A reference to a named credential. Never holds the secret value.

    ``cooldown_until`` (timezone-aware UTC) marks the credential temporarily
    unavailable until that instant; ``None`` means available.
    """
    name: str
    cooldown_until: datetime | None = None

    def is_available(self, now: datetime | None = None) -> bool:
        if self.cooldown_until is None:
            return True
        now = now or _utcnow()
        return now >= self.cooldown_until

    # Explicit, value-free representations. A Credential never holds secret
    # material, so this is defensive belt-and-suspenders for non-disclosure.
    def __repr__(self) -> str:
        if self.cooldown_until is None:
            state = "available"
        else:
            state = f"cooldown_until={self.cooldown_until.isoformat()}"
        return f"Credential(name={self.name!r}, {state})"

    __str__ = __repr__


class CredentialPool:
    """Ordered pool of named Gemini credentials with cooldown tracking.

    ``get_secret`` is injectable purely for testing; production uses the real
    :func:`void.security.secrets.get_secret`. Only names/manifest are read at
    construction - never a secret value.
    """

    def __init__(self, primary_name: str = secrets.GEMINI_API_KEY,
                 manifest_key: str = MANIFEST_KEY,
                 get_secret=None):
        self._primary = primary_name
        self._manifest_key = manifest_key
        self._get_secret = get_secret or secrets.get_secret
        names = self._resolve_names()
        self._creds: list[Credential] = [Credential(n) for n in names]
        self._by_name: dict[str, Credential] = {c.name: c for c in self._creds}

    # --- name / manifest resolution ------------------------------------

    def _read_manifest(self) -> list | None:
        """Return the manifest list, or None if absent/invalid/unavailable."""
        try:
            raw = self._get_secret(self._manifest_key)
        except secrets.SecretStoreError:
            # The manifest is optional metadata; a backend hiccup must never
            # break the single-key path. Fall back to primary-only.
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, list) else None

    def _resolve_names(self) -> list[str]:
        manifest = self._read_manifest()
        candidate = manifest if manifest is not None else [self._primary]
        # Primary always first, then manifest order.
        ordered = [self._primary] + [n for n in candidate if n != self._primary]
        seen: set[str] = set()
        final: list[str] = []
        for n in ordered:
            if not isinstance(n, str):
                continue  # never interpret arbitrary values as credential names
            n = n.strip()
            if not n or n in seen:
                continue
            seen.add(n)
            final.append(n)
        # Guarantee the primary is present and first, whatever the manifest held.
        if self._primary and self._primary not in seen:
            final.insert(0, self._primary)
        return final

    # --- introspection -------------------------------------------------

    def names(self) -> list[str]:
        return [c.name for c in self._creds]

    def credentials(self) -> list[Credential]:
        return list(self._creds)

    def __len__(self) -> int:
        return len(self._creds)

    def __repr__(self) -> str:
        return f"CredentialPool(names={self.names()!r}, count={len(self._creds)})"

    __str__ = __repr__

    # --- availability --------------------------------------------------

    def get_next_available(self, now: datetime | None = None) -> Credential:
        """First credential (in order) that is not in cooldown.

        Raises :class:`CredentialsExhausted` if every credential is unavailable.
        Never silently returns an unavailable/arbitrary credential.
        """
        now = now or _utcnow()
        for cred in self._creds:
            if cred.is_available(now):
                return cred
        raise CredentialsExhausted(
            f"All {len(self._creds)} Gemini credential(s) are currently "
            f"unavailable (in cooldown)."
        )

    def mark_unavailable(self, alias: str, cooldown_until: datetime) -> None:
        """Mark ``alias`` unavailable until ``cooldown_until`` (UTC-aware)."""
        cred = self._by_name.get(alias)
        if cred is None:
            raise ValueError(f"Unknown credential: {alias!r}")
        if cooldown_until.tzinfo is None:
            raise ValueError("cooldown_until must be timezone-aware (UTC).")
        cred.cooldown_until = cooldown_until.astimezone(timezone.utc)

    def clear_cooldown(self, alias: str) -> None:
        """Make ``alias`` immediately available again."""
        cred = self._by_name.get(alias)
        if cred is None:
            raise ValueError(f"Unknown credential: {alias!r}")
        cred.cooldown_until = None

    # --- secret access (on demand; value never retained) ---------------

    def get_value(self, credential: "Credential | str") -> str:
        """Read a credential's value from the store, on demand.

        The value is returned to the caller and is NOT stored on the pool or
        the Credential. Raises :class:`CredentialMissing` if there is no value;
        the exception never contains the value.
        """
        name = credential.name if isinstance(credential, Credential) else credential
        value = self._get_secret(name)
        if not value:
            raise CredentialMissing(f"No stored value for credential {name!r}.")
        return value
