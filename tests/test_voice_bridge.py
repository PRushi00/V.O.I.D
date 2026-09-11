"""Deterministic tests for VoiceStateBridge (void.ui.voice_bridge).

Same-thread signal wiring needs no QApplication at all (proven directly).
Cross-thread delivery is proven with a QCoreApplication event-loop pump -
QCoreApplication has no GUI/display dependency (unlike QApplication, it never
touches a platform plugin), so this stays deterministic and display-
independent even though it exercises real Qt thread-safety.
"""
from __future__ import annotations

import sys
import threading
import time

from void.ui.voice_bridge import VoiceStateBridge


def test_same_thread_state_signal_delivers_synchronously():
    bridge = VoiceStateBridge()
    received = []
    bridge.stateChanged.connect(received.append)
    bridge.stateChanged.emit("listening")
    assert received == ["listening"]


def test_same_thread_transcript_and_message_signals():
    bridge = VoiceStateBridge()
    transcripts, messages = [], []
    bridge.transcriptReceived.connect(transcripts.append)
    bridge.messageReceived.connect(messages.append)
    bridge.transcriptReceived.emit("open my notes")
    bridge.messageReceived.emit("(voice) microphone unavailable")
    assert transcripts == ["open my notes"]
    assert messages == ["(voice) microphone unavailable"]


def test_bridge_holds_no_widget_or_backend_reference():
    # Pure plumbing: it relays strings, nothing else - no widget/Assistant/
    # VoiceController/security-object attribute of any kind.
    bridge = VoiceStateBridge()
    for attr in ("widget", "assistant", "voice_controller", "tools",
                 "risk_gate", "kill_switch"):
        assert not hasattr(bridge, attr)


def test_cross_thread_emit_is_safely_queued_to_the_owning_thread():
    # VoiceSession callbacks fire on non-Qt threads (monitor/worker/hook/
    # wake-pump threads). This proves a signal emitted from another thread is
    # delivered on the thread that owns the bridge, not synchronously in
    # place on the emitting thread, once the event loop pumps - the exact
    # safety property this bridge exists for.
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    bridge = VoiceStateBridge()
    received = []
    owning_thread = threading.current_thread()
    calling_threads = []

    def on_state(state):
        received.append(state)
        calling_threads.append(threading.current_thread())

    bridge.stateChanged.connect(on_state)

    def worker():
        bridge.stateChanged.emit("speaking")

    t = threading.Thread(target=worker, name="voice-test-worker")
    t.start()
    t.join()

    deadline = time.time() + 2.0
    while not received and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)

    assert received == ["speaking"]
    # Delivered on the thread that owns the bridge (this test's thread),
    # never on the emitting worker thread - this is what makes it safe to
    # connect a widget-mutating slot to it.
    assert calling_threads == [owning_thread]
