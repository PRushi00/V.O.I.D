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

import queue
import threading
from typing import Callable

from void.voice.adapters import (
    FasterWhisperSTT, MicAudioCapture, PTTActivation,
)
from void.voice.session import VoiceSession
from void.voice.state import VoiceState
from void.voice.tts import create_tts_provider


class _SerialVoiceWorker:
    """ONE dedicated thread that runs the blocking release-chain (capture
    finalize -> STT -> Assistant.run -> speak) off the PTT/keyboard-hook and
    monitor threads. Strictly serial (not a pool), so at most one chain runs at
    a time - single-flight is still enforced by the VoiceSession itself. Voice
    lifecycle safety is unchanged: results carry their generation, so anything
    that finishes late is dropped by the session's existing stale guard."""

    def __init__(self, name: str = "void-voice-worker"):
        self._q: "queue.Queue" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._name = name
        self._alive = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._alive = True
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True)
        self._thread.start()

    def submit(self, fn: Callable[[], None]) -> None:
        if self._alive:
            self._q.put(fn)

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            if fn is None:
                break
            try:
                fn()
            except Exception:  # pragma: no cover - defensive; never kill worker
                pass

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        self._alive = False
        self._q.put(None)
        self._thread.join(timeout=timeout)
        self._thread = None


class VoiceController:
    def __init__(self, session: VoiceSession, activation, *,
                 poll_interval: float = 0.1, worker=None):
        self._session = session
        self._activation = activation
        self._poll_interval = poll_interval
        self._monitor: threading.Thread | None = None
        self._stop_evt = threading.Event()
        # Optional serial worker for the blocking release-chain. When absent
        # (e.g. unit tests), on_ptt_release runs inline exactly as before.
        self._worker = worker

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
        worker = _SerialVoiceWorker()
        controller = cls(session, None, poll_interval=poll_interval,
                         worker=worker)
        # PTT edges go through the controller: press is quick (mic open / barge-
        # in) and runs inline on the hook thread; release runs the blocking
        # STT/Assistant/TTS chain on the serial worker so the hook thread stays
        # responsive (real barge-in / new PTT remain observable during a reply).
        controller._activation = PTTActivation(
            controller.on_ptt_press, controller.on_ptt_release,
            hotkey=config.get("voice.ptt_hotkey", "ctrl+space"),
        )
        return controller

    # --- PTT edges (input thread -> session; blocking work -> worker) --
    def on_ptt_press(self) -> None:
        # Quick: IDLE->LISTENING (mic open) or SPEAKING barge-in (TTS stop).
        self._session.on_ptt_press()

    def on_ptt_release(self) -> None:
        # Finalize capture + STT + Assistant.run + speak is blocking; run it off
        # the input/hook thread when a worker is present.
        if self._worker is not None:
            self._worker.submit(self._session.on_ptt_release)
        else:
            self._session.on_ptt_release()

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
        if self._worker is not None:
            self._worker.start()
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
        # Done before the worker stops so any late chain result is stale-dropped.
        self._session.close()
        if self._worker is not None:
            self._worker.stop()

    @property
    def stopped(self) -> bool:
        """True once the session has reached the KillSwitch-latched STOPPED."""
        return self._session.state == VoiceState.STOPPED

    @property
    def closed(self) -> bool:
        """True once the session has reached the terminal CLOSED state."""
        return self._session.state == VoiceState.CLOSED
