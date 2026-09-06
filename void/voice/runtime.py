"""VoiceController (Phase 9A): the runtime that makes the voice pipeline live.

The VoiceSession is pure logic (PTT edges -> STT -> Assistant.run -> TTS). This
controller supplies the two things a *running* session needs and nothing more:

  * an ActivationAdapter (push-to-talk hotkey) whose press/release edges are
    wired to the session, and
  * a lightweight monitor loop that ticks ``session.poll()`` so the GLOBAL kill
    switch interrupts voice from any state and async speech retires to IDLE.

It is an I/O runtime, NOT a second agent: it never touches tools, RiskGate, the
task store, or confirmations. Heavy/Windows-only libraries stay lazy (they are
imported only when the real adapters open the mic / load the model / speak), so
constructing a controller never requires the optional voice stack; the
dependency error surfaces at start()/first use with an actionable message.
"""
from __future__ import annotations

import threading
from typing import Callable

from void.voice.adapters import (
    FasterWhisperSTT, MicAudioCapture, PTTActivation,
)
from void.voice.session import VoiceSession
from void.voice.state import VoiceState
from void.voice.tts import create_tts_provider


class VoiceController:
    def __init__(self, session: VoiceSession, activation, *,
                 poll_interval: float = 0.1):
        self._session = session
        self._activation = activation
        self._poll_interval = poll_interval
        self._monitor: threading.Thread | None = None
        self._stop_evt = threading.Event()

    # --- construction from config -------------------------------------
    @classmethod
    def from_assistant(cls, assistant, config=None, *,
                       on_state: Callable[[str], None] | None = None,
                       on_transcript: Callable[[str], None] | None = None,
                       on_message: Callable[[str], None] | None = None,
                       poll_interval: float = 0.1) -> "VoiceController":
        """Build the real (mic + faster-whisper + local TTS) controller from
        config. The TTS backend is chosen via the provider-agnostic factory
        (voice.tts_provider), so the controller never depends on SAPI directly.

        Adapters are constructed but NOT activated here: no library import, no
        model download, no mic access happens until start()/first use.
        """
        config = config or assistant.config
        capture = MicAudioCapture()
        stt = FasterWhisperSTT(
            model_name=config.get("voice.stt_model", "small"),
            device=config.get("voice.stt_device", "cpu"),
            language=config.get("voice.stt_language", "en"),
        )
        tts = create_tts_provider(config)   # provider-agnostic; null-safe fallback
        session = VoiceSession(
            assistant, assistant.kill_switch,
            capture=capture, stt=stt, tts=tts,
            on_state=on_state, on_transcript=on_transcript,
            on_message=on_message,
            speak_response=config.get("voice.speak_responses", True),
        )
        activation = PTTActivation(
            session.on_ptt_press, session.on_ptt_release,
            hotkey=config.get("voice.ptt_hotkey", "ctrl+space"),
        )
        return cls(session, activation, poll_interval=poll_interval)

    # --- observability ------------------------------------------------
    @property
    def session(self) -> VoiceSession:
        return self._session

    @property
    def state(self) -> str:
        return self._session.state

    # --- lifecycle ----------------------------------------------------
    def start(self, *, monitor: bool = True) -> None:
        """Arm the activation adapter and (optionally) start the monitor loop.

        ``monitor=False`` skips the background thread so callers/tests can drive
        the live loop deterministically via poll_once().
        """
        self._stop_evt.clear()
        self._activation.start()
        if monitor and self._monitor is None:
            self._monitor = threading.Thread(
                target=self._run_monitor, name="void-voice-monitor",
                daemon=True)
            self._monitor.start()

    def poll_once(self) -> None:
        """One deterministic monitor tick (kill-switch enforcement + speech
        retirement). The background monitor calls this on a timer."""
        self._session.poll()

    def _run_monitor(self) -> None:
        # Cadence only; all decisions live in session.poll(). A failing tick
        # must never kill the monitor (the kill switch must keep being checked).
        while not self._stop_evt.wait(self._poll_interval):
            try:
                self._session.poll()
            except Exception:  # pragma: no cover - defensive
                pass

    def shutdown(self, reason: str = "voice shutdown") -> None:
        """Stop activation, join the monitor, and drive the session to the
        terminal CLOSED state (which also releases the TTS worker/COM and mic)."""
        self._stop_evt.set()
        try:
            self._activation.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        if self._monitor is not None:
            self._monitor.join(timeout=1.0)
            self._monitor = None
        # SHUTDOWN: halt mic/STT/TTS, invalidate generation, release resources.
        self._session.close()

    @property
    def stopped(self) -> bool:
        """True once the session has reached the KillSwitch-latched STOPPED."""
        return self._session.state == VoiceState.STOPPED

    @property
    def closed(self) -> bool:
        """True once the session has reached the terminal CLOSED state."""
        return self._session.state == VoiceState.CLOSED
