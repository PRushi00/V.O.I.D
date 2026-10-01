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
import logging
import math
import os
import secrets as _pysecrets
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("void.device.pairing")

DEFAULT_WINDOW_SECONDS = 300  # 5 minutes
_FILENAME = "pairing_window.json"
_CLOCK_SLACK_S = 30       # tolerated clock adjustment between begin() and redeem()

# Distinguishes WHY a token was rejected, for diagnostics only - never
# changes the response the caller gets (still always "invalid_pairing_token"
# to a network caller; see void.device.gateway). Before this, every
# rejection reason collapsed into one generic log line, so a real field
# failure (e.g. the gateway silently failing to READ the file at all - a
# permissions problem, a corrupt write, two processes resolving different
# state directories) was indistinguishable in the log from an ordinary wrong
# guess or an expired window. That made exactly this class of bug
# undiagnosable after the fact.
NO_WINDOW = "no_window"
EXPIRED = "expired"
WRONG_TOKEN = "wrong_token"


@dataclass
class PairingToken:
    token: str
    name: str
    expires_at: float


class PairingError(Exception):
    """Raised when a presented pairing token is missing, wrong, expired, or
    already used. ``reason`` is one of NO_WINDOW/EXPIRED/WRONG_TOKEN -
    diagnostic only, never exposed to the network caller as-is."""

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


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
            expires_at = float(data["expires_at"])
            if not math.isfinite(expires_at):
                raise ValueError("non-finite expiry")      # NaN/inf would make the window never expire
            return PairingToken(token=str(data["token"]), name=str(data["name"]), expires_at=expires_at)
        except (ValueError, KeyError, TypeError):
            # The file exists but its CONTENT is unusable (corrupt/partial
            # JSON, missing field) - genuinely equivalent to "no window", but
            # worth a diagnostic trace since it means something wrote a bad
            # file rather than there simply being no pairing in progress.
            # Never logs file content - only that this happened.
            _log.warning("PAIRING_FILE_UNREADABLE path=%s (corrupt/partial content)",
                        self._path)
            return None
        except OSError as exc:
            # Distinct from "the file doesn't exist" (handled above): this is
            # something like a permissions error stopping an EXISTING file
            # from being read at all. Silently treating this the same as "no
            # window" is exactly what made this class of bug undiagnosable in
            # the field - a real read failure and a genuine absence produced
            # an IDENTICAL, generic "no pairing window is open" with no trace
            # of which one actually happened.
            _log.warning("PAIRING_FILE_READ_FAILED path=%s error=%s",
                        self._path, type(exc).__name__)
            return None

    def _write(self, token: PairingToken | None) -> None:
        if token is None:
            self._path.unlink(missing_ok=True)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "token": token.token, "name": token.name, "expires_at": token.expires_at,
        })
        # Atomic write (temp file + os.replace): a plain write_text() leaves
        # a window where a concurrent _read() on another process/thread can
        # observe a truncated/partial file (and silently treat it as "no
        # window" per the except clause above). os.replace is atomic on both
        # Windows and POSIX, so a reader only ever sees the old complete
        # file or the new complete file, never a partial one.
        tmp_path = self._path.parent / f"{self._path.name}.tmp{os.getpid()}"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, self._path)

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
            raise PairingError("No pairing window is open.", reason=NO_WINDOW)
        # ``begin()`` never issues a window longer than ``self._window``; one that claims to be longer was not written
        # by this code (a planted or corrupted file), so it is treated as expired and discarded rather than honoured.
        if now > active.expires_at or active.expires_at - now > self._window + _CLOCK_SLACK_S:
            self._write(None)
            raise PairingError("Pairing window has expired.", reason=EXPIRED)
        if not token or not hmac.compare_digest(token, active.token):
            raise PairingError("Incorrect pairing token.", reason=WRONG_TOKEN)
        self._write(None)  # single-use
        return active.name
