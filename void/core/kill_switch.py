"""Emergency stop - the highest-priority control in V.O.I.D.

'VOID, STOP EVERYTHING' must halt task execution promptly. The switch combines
two signals so a stop can arrive from anywhere:

1. An in-process, thread-safe flag (fast path for the running agent/UI).
2. An optional stop-file on disk, so *another process* - a second terminal,
   the UI, and later another device - can trigger a stop that the running
   agent will observe.

The agent loop checks the switch before every step and every tool call.
Triggering can require an authenticated PIN (from the OS secret store) so a
stray phrase can't halt real work without authorization.

Design note: this is cooperative cancellation. It cannot kill a syscall
already mid-flight, but it prevents any *new* step or tool call from starting -
the right guarantee for V1's action set.
"""
from __future__ import annotations

import hmac
import threading
from pathlib import Path

from void.security import secrets


class StopRequested(Exception):
    """Raised inside the agent loop when the kill switch is engaged."""


class KillSwitch:
    def __init__(self, phrase: str = "VOID, STOP EVERYTHING",
                 require_pin: bool = False, stop_file: str | Path | None = None):
        self.phrase = phrase
        self.require_pin = require_pin
        self.stop_file = Path(stop_file) if stop_file else None
        self._event = threading.Event()
        self._reason: str | None = None

    # --- state ---------------------------------------------------------

    @property
    def engaged(self) -> bool:
        if self._event.is_set():
            return True
        if self.stop_file and self.stop_file.exists():
            if self._reason is None:
                try:
                    self._reason = self.stop_file.read_text(
                        encoding="utf-8").strip() or "stop file present"
                except OSError:
                    self._reason = "stop file present"
            return True
        return False

    @property
    def reason(self) -> str | None:
        return self._reason

    def reset(self) -> None:
        """Clear the stop state (owner-initiated resume)."""
        self._event.clear()
        self._reason = None
        if self.stop_file and self.stop_file.exists():
            try:
                self.stop_file.unlink()
            except OSError:
                pass

    # --- triggering ----------------------------------------------------

    def _authenticate(self, pin: str | None) -> bool:
        if not self.require_pin:
            return True
        stored = secrets.get_secret(secrets.STOP_PIN)
        if not stored:
            # Fail safe: if a PIN is required but none is configured, allow the
            # stop. Halting is always safer than refusing to halt.
            return True
        if not pin:
            return False
        # Constant-time comparison (D-09): '==' leaks the matching prefix length
        # through timing. compare_digest needs bytes for non-ASCII str.
        return hmac.compare_digest(str(pin).encode("utf-8"), str(stored).encode("utf-8"))

    def engage(self, reason: str = "manual stop", pin: str | None = None) -> bool:
        """Engage the stop. Returns True on success, False if auth failed."""
        if not self._authenticate(pin):
            return False
        self._reason = reason
        self._event.set()
        if self.stop_file:
            try:
                self.stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.stop_file.write_text(reason, encoding="utf-8")
            except OSError:
                pass  # in-process flag still holds
        return True

    def handle_command(self, text: str, pin: str | None = None) -> bool:
        """Engage if ``text`` matches the stop phrase (case-insensitive)."""
        if not text:
            return False
        if text.strip().lower() == self.phrase.strip().lower():
            return self.engage(reason="stop phrase", pin=pin)
        return False

    def raise_if_engaged(self) -> None:
        if self.engaged:
            raise StopRequested(self._reason or "stop requested")
