"""VoiceStateBridge: the ONLY safe way voice-runtime callbacks reach Qt.

VoiceSession's on_state/on_transcript/on_message callbacks fire synchronously
on whichever thread triggered them - the voice monitor thread, the serial
voice worker thread, the PTT keyboard-hook thread, or the wake-detector pump
thread. None of those are the Qt UI thread, so nothing on the receiving end
may touch a widget directly from inside them.

This bridge is a QObject with three Qt signals; the callbacks passed into
VoiceController.from_assistant(...) are simply the signals' own .emit bound
methods. Qt queues delivery of a signal to a slot living on a different
thread than the one that called .emit() (PySide6's default "auto connection"
becomes a queued connection across threads), so a UI-thread slot connected to
one of these signals is invoked safely on the UI thread, in order, once the
Qt event loop is running.

This module holds no reference to any widget and constructs nothing beyond
itself - it is pure plumbing, never a decision point. It never inspects task
state, never calls Assistant/RiskGate/KillSwitch, and never mutates anything;
it only relays three strings.
"""
from __future__ import annotations

try:
    from PySide6.QtCore import QObject, Signal
except ImportError as exc:  # pragma: no cover - UI optional
    raise ImportError(
        "PySide6 is required for the UI. Install it with: pip install PySide6"
    ) from exc


class VoiceStateBridge(QObject):
    """Relays VoiceSession's on_state/on_transcript/on_message callbacks
    (called from non-Qt threads) into the Qt UI thread as signals."""

    stateChanged = Signal(str)
    transcriptReceived = Signal(str)
    messageReceived = Signal(str)
