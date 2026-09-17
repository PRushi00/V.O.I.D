"""Pairing: the one deliberate, narrow, time-boxed exception to "unknown
devices get nothing".

The owner starts a pairing window from the laptop (``python -m void device
pair-start``), which prints a short-lived, single-use token plus the
gateway's address and certificate fingerprint. The companion device is told
that same information out-of-band (read off the laptop's screen), connects
once, presents the token over the pairing endpoint, and - only if the token
is valid and unused - receives a fresh device_id and shared secret. The
token itself is never a long-term credential: it is consumed on first
success and expires on its own shortly after, so a token that leaked (a
shoulder-surfed screen, a stray screenshot) stops being useful within
minutes even if never used.

File-backed (``<state_dir>/pairing_window.json``), the same cross-process
pattern :class:`~void.core.kill_switch.KillSwitch` already uses for its stop
file: ``pair-start`` and ``device serve`` are ordinarily two separate
process invocations (a short CLI command vs. a long-running server), so the
pairing window has to live somewhere both can see it, not in either
process's memory.

A newly paired device starts with only the default read-only capability;
the owner grants anything further afterwards
(void.device.identity.DeviceRegistry.grant / ``python -m void device
grant``), mirroring how void.roots authorizes filesystem access explicitly
rather than by default.
"""
from __future__ import annotations

import hmac
import json
import secrets as _pysecrets
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WINDOW_SECONDS = 300  # 5 minutes
_FILENAME = "pairing_window.json"


@dataclass
class PairingToken:
    token: str
    name: str
    expires_at: float


class PairingError(Exception):
    """Raised when a presented pairing token is missing, wrong, expired, or
    already used."""


class PairingManager:
    """At most one active (unused, unexpired) pairing token at a time - a
    fresh ``begin()`` call replaces any prior one, so a forgotten open
    pairing window can't linger indefinitely alongside a new one."""

    def __init__(self, state_dir: Path, window_seconds: float = DEFAULT_WINDOW_SECONDS):
        self._path = Path(state_dir) / _FILENAME
        self._window = window_seconds

    def _read(self) -> PairingToken | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return PairingToken(token=str(data["token"]), name=str(data["name"]),
                               expires_at=float(data["expires_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, token: PairingToken | None) -> None:
        if token is None:
            self._path.unlink(missing_ok=True)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({
            "token": token.token, "name": token.name, "expires_at": token.expires_at,
        }), encoding="utf-8")

    def begin(self, name: str, now: float | None = None) -> PairingToken:
        now = time.time() if now is None else now
        token = PairingToken(token=_pysecrets.token_urlsafe(9),  # ~12 chars
                            name=name, expires_at=now + self._window)
        self._write(token)
        return token

    def cancel(self) -> None:
        self._write(None)

    def redeem(self, token: str, now: float | None = None) -> str:
        """Consume the active token if it matches and hasn't expired.
        Returns the device name it was created with. Raises
        :class:`PairingError` otherwise - a wrong guess leaves the active
        token alone (rather than burning it), but it IS cleared on success or
        expiry, so it is single-use."""
        now = time.time() if now is None else now
        active = self._read()
        if active is None:
            raise PairingError("No pairing window is open.")
        if now > active.expires_at:
            self._write(None)
            raise PairingError("Pairing window has expired.")
        if not token or not hmac.compare_digest(token, active.token):
            raise PairingError("Incorrect pairing token.")
        self._write(None)  # single-use
        return active.name
