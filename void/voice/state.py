"""Deterministic voice state machine (Phase 9A).

Voice state is SEPARATE from Agent/Task status and is never controlled by the
LLM. Illegal transitions are rejected deterministically. KillSwitch may force
STOPPED from any state, and STOPPED is terminal (no reactivation) until an
explicit ``reset`` once the kill switch is cleared.
"""
from __future__ import annotations


class VoiceState:
    IDLE = "idle"
    LISTENING = "listening"                 # mic open, capturing
    CAPTURED = "captured"                    # mic closed, audio finalized
    TRANSCRIBING = "transcribing"           # STT running
    DISPATCHED = "dispatched"               # transcript handed to Assistant.run
    AWAITING_CONFIRMATION = "awaiting_confirmation"  # HIGH-risk; non-voice OK
    SPEAKING = "speaking"                    # TTS playing
    ERROR = "error"
    STOPPED = "stopped"                      # kill switch; terminal


# Allowed transitions (excluding the universal kill-switch -> STOPPED, and
# ERROR -> IDLE recovery, both handled explicitly below).
_ALLOWED: dict[str, set[str]] = {
    VoiceState.IDLE: {VoiceState.LISTENING},
    VoiceState.LISTENING: {VoiceState.CAPTURED, VoiceState.ERROR},
    VoiceState.CAPTURED: {VoiceState.TRANSCRIBING, VoiceState.ERROR},
    # invalid/empty transcript returns to IDLE; valid -> DISPATCHED
    VoiceState.TRANSCRIBING: {VoiceState.DISPATCHED, VoiceState.IDLE,
                              VoiceState.ERROR},
    VoiceState.DISPATCHED: {VoiceState.SPEAKING,
                            VoiceState.AWAITING_CONFIRMATION,
                            VoiceState.IDLE, VoiceState.ERROR},
    VoiceState.AWAITING_CONFIRMATION: {VoiceState.IDLE, VoiceState.ERROR},
    # SPEAKING may be interrupted by PTT (-> LISTENING) or finish (-> IDLE)
    VoiceState.SPEAKING: {VoiceState.IDLE, VoiceState.LISTENING,
                          VoiceState.ERROR},
    VoiceState.ERROR: {VoiceState.IDLE},
    VoiceState.STOPPED: set(),              # terminal
}


class IllegalVoiceTransition(RuntimeError):
    """Raised when an unallowed voice-state transition is attempted."""


class VoiceStateMachine:
    def __init__(self, on_change=None):
        self._state = VoiceState.IDLE
        self._on_change = on_change or (lambda _s: None)

    @property
    def state(self) -> str:
        return self._state

    def can(self, target: str) -> bool:
        if target == self._state:
            return True
        return target in _ALLOWED.get(self._state, set())

    def to(self, target: str) -> None:
        """Perform a normal (non-kill-switch) transition; reject if illegal."""
        if target == self._state:
            return
        if target not in _ALLOWED.get(self._state, set()):
            raise IllegalVoiceTransition(
                f"illegal voice transition {self._state} -> {target}")
        self._state = target
        self._on_change(target)

    def force_stopped(self) -> None:
        """Kill switch: STOPPED is reachable from ANY state, always allowed."""
        if self._state != VoiceState.STOPPED:
            self._state = VoiceState.STOPPED
            self._on_change(VoiceState.STOPPED)

    def reset(self) -> None:
        """Return a STOPPED machine to IDLE (owner-cleared kill switch only)."""
        self._state = VoiceState.IDLE
        self._on_change(VoiceState.IDLE)
