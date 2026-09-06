"""VoiceSession (Phase 9A): the single serialized owner of the voice pipeline.

PTT press/release -> mic capture -> local STT -> Assistant.run() -> local TTS,
driven by a deterministic state machine. Voice is an I/O ADAPTER: it never
interprets intent, calls tools/RiskGate, approves confirmations, or creates a
second agent. The existing Assistant/Agent/RiskGate/KillSwitch/Task machinery
remains authoritative.

Key safety properties:
  * Single-flight: only one voice command in flight; new activations are
    rejected (never queued) unless interrupting TTS.
  * Mic is closed except during LISTENING (context-managed cleanup).
  * KillSwitch works from every state and does not depend on STT/TTS
    responsiveness: it bumps a generation token and forces STOPPED immediately;
    a late STT result carrying a stale token is discarded before dispatch.
  * HIGH-risk confirmation stays with the existing non-voice mechanism; a spoken
    "yes" cannot authorize anything.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from void.core.task import Status
from void.voice.adapters import STTError, TTSError
from void.voice.state import VoiceState, VoiceStateMachine


class VoiceSession:
    def __init__(self, assistant, kill_switch, *, capture, stt, tts,
                 on_state: Callable[[str], None] | None = None,
                 on_transcript: Callable[[str], None] | None = None,
                 on_message: Callable[[str], None] | None = None,
                 speak_response: bool = True,
                 speak_grace_seconds: float = 0.5,
                 now: Callable[[], float] | None = None):
        self._assistant = assistant
        self._ks = kill_switch
        self._capture = capture
        self._stt = stt
        self._tts = tts
        self._sm = VoiceStateMachine(on_change=on_state or (lambda _s: None))
        self._on_transcript = on_transcript or (lambda _t: None)
        self._msg = on_message or (lambda _m: None)
        self._speak_response = speak_response
        self._lock = threading.RLock()
        self._generation = 0     # bumped by stop()/reset(); guards late results
        self._last_transcript: str | None = None
        self._last_result = None
        # Live-loop retirement of ASYNC TTS (see poll()). We only retire SPEAKING
        # once we have observed the backend actually speaking, or after a small
        # grace window (guards the race where async speak() has returned but the
        # backend has not started yet, so it must not be retired prematurely).
        self._speak_grace = speak_grace_seconds
        self._now = now or time.monotonic
        self._speech_seen = False
        self._speaking_since = 0.0

    # --- observability ------------------------------------------------
    @property
    def state(self) -> str:
        return self._sm.state

    @property
    def last_transcript(self) -> str | None:
        return self._last_transcript

    # --- kill switch (authoritative, from ANY state) ------------------
    def stop(self, reason: str = "kill switch") -> None:
        with self._lock:
            self._generation += 1        # invalidate any in-flight/late result
            self._safe_close_mic()
            try:
                self._tts.stop()
            except Exception:
                pass
            self._sm.force_stopped()
            self._msg(f"Voice stopped ({reason}).")

    def close(self) -> None:
        """Release voice I/O resources (TTS worker/COM, mic). Lifecycle only -
        no task/authority semantics. Safe to call after stop()."""
        try:
            self._tts.close()
        except Exception:
            pass
        self._safe_close_mic()

    def reset(self) -> None:
        """Return a STOPPED session to IDLE - only if the kill switch is clear."""
        with self._lock:
            if getattr(self._ks, "engaged", False):
                self._msg("Cannot reset voice while the kill switch is engaged.")
                return
            self._generation += 1
            self._sm.reset()

    # --- PTT edges ----------------------------------------------------
    def on_ptt_press(self) -> None:
        with self._lock:
            if self._blocked_by_killswitch():
                return
            st = self._sm.state
            if st == VoiceState.SPEAKING:
                # PTT during TTS interrupts speech and starts a new capture.
                try:
                    self._tts.stop()
                except Exception:
                    pass
                self._begin_listening()
                return
            if st == VoiceState.IDLE:
                self._begin_listening()
                return
            # LISTENING/CAPTURED/TRANSCRIBING/DISPATCHED/AWAITING_CONFIRMATION:
            # single-flight - reject, never queue.
            self._msg("Voice is busy; ignoring activation.")

    def on_ptt_release(self) -> None:
        with self._lock:
            if self._sm.state != VoiceState.LISTENING:
                return
            gen = self._generation
            self._sm.to(VoiceState.CAPTURED)
            try:
                audio = self._capture.stop()      # finalize + close mic
            except Exception as exc:
                self._safe_close_mic()
                self._to_error(f"capture failed: {exc}")
                return
        # STT + dispatch outside the press/release critical section.
        self._transcribe_and_dispatch(audio, gen)

    # --- TTS completion (from the real async voice-done event) --------
    def notify_speech_finished(self) -> None:
        with self._lock:
            if self._sm.state == VoiceState.SPEAKING:
                self._sm.to(VoiceState.IDLE)

    # --- live-loop tick (driven by the runtime monitor) ----------------
    def poll(self) -> None:
        """One deterministic live-loop step, safe to call repeatedly.

        Two responsibilities, both authoritative in a running session:
          1. Make the GLOBAL kill switch stop voice from ANY state - even if it
             was engaged elsewhere (UI STOP, `void stop`, another process) while
             voice is mid-utterance and no PTT edge is arriving.
          2. Retire async speech: return SPEAKING -> IDLE once the TTS backend
             reports it has finished (with a grace window for the async start).

        No sleeps; the monitor thread supplies the cadence. Idempotent.
        """
        with self._lock:
            st = self._sm.state
            if st == VoiceState.STOPPED:
                return
            engaged = bool(getattr(self._ks, "engaged", False))
        if engaged:
            self.stop("kill switch")
            return
        with self._lock:
            if self._sm.state != VoiceState.SPEAKING:
                return
            try:
                speaking = bool(self._tts.is_speaking)
            except Exception:
                speaking = False
            if speaking:
                self._speech_seen = True
                return
            # Not (yet) speaking: retire only once we have actually observed
            # speech, or after the grace window (degenerate: utterance finished
            # between polls, or backend never reported speaking).
            if self._speech_seen or (self._now() - self._speaking_since) >= self._speak_grace:
                self._sm.to(VoiceState.IDLE)

    # --- pipeline -----------------------------------------------------
    def _transcribe_and_dispatch(self, audio, gen: int) -> None:
        with self._lock:
            if self._stale(gen):
                return
            self._sm.to(VoiceState.TRANSCRIBING)
        try:
            transcript = self._stt.transcribe(audio)
        except STTError as exc:
            self._to_error(f"transcription failed: {exc}")
            return
        transcript = (transcript or "").strip()
        with self._lock:
            # Authoritative guard IMMEDIATELY before dispatch: a late result
            # after a kill switch / new generation must never reach the Agent.
            if self._stale(gen):
                return
            if not transcript:
                self._msg("No speech detected.")
                self._sm.to(VoiceState.IDLE)   # invalid transcript: do not dispatch
                return
            self._last_transcript = transcript
            self._on_transcript(transcript)     # UI shows transcript before dispatch
            self._sm.to(VoiceState.DISPATCHED)

        # Hand the transcript to the EXISTING execution pipeline. Treated as
        # ordinary untrusted user input; the Agent/RiskGate remain authoritative.
        result = self._assistant.run(transcript)
        self._last_result = result

        with self._lock:
            if self._stale(gen):
                return
            status = getattr(result, "status", None)
            if status == Status.AWAITING_CONFIRMATION:
                self._sm.to(VoiceState.AWAITING_CONFIRMATION)
                self._msg("Confirmation required (approve or deny outside voice).")
                # Voice never authorizes: announce only, then release to IDLE.
                if self._speak_response:
                    self._safe_speak("A confirmation is required.")
                self._sm.to(VoiceState.IDLE)
                return
            text = getattr(result, "result", None) or ""
            if self._speak_response and text:
                self._sm.to(VoiceState.SPEAKING)
                self._speech_seen = False
                self._speaking_since = self._now()
                self._safe_speak(text)
                # Real TTS is async; the runtime monitor calls poll() to retire
                # SPEAKING -> IDLE when speech finishes. notify_speech_finished()
                # remains available for callers that have a backend done-event.
            else:
                self._sm.to(VoiceState.IDLE)

    # --- helpers ------------------------------------------------------
    def _begin_listening(self) -> None:
        self._sm.to(VoiceState.LISTENING)
        try:
            self._capture.open()
        except Exception as exc:
            self._safe_close_mic()
            self._to_error(f"microphone unavailable: {exc}")

    def _safe_speak(self, text: str) -> None:
        try:
            self._tts.speak(text)
        except TTSError as exc:
            # TTS failure must NOT corrupt the Agent result or task; just report.
            self._msg(f"(voice) could not speak the response: {exc}")
            if self._sm.state == VoiceState.SPEAKING:
                self._sm.to(VoiceState.IDLE)

    def _safe_close_mic(self) -> None:
        try:
            self._capture.close()
        except Exception:
            pass

    def _blocked_by_killswitch(self) -> bool:
        if getattr(self._ks, "engaged", False) or self._sm.state == VoiceState.STOPPED:
            self._msg("Voice is stopped (kill switch).")
            return True
        return False

    def _stale(self, gen: int) -> bool:
        """True if this in-flight operation has been invalidated (kill switch /
        new generation). Late STT/dispatch must abort when stale."""
        if gen != self._generation or getattr(self._ks, "engaged", False):
            if getattr(self._ks, "engaged", False):
                self._sm.force_stopped()
            return True
        return False

    def _to_error(self, message: str) -> None:
        with self._lock:
            self._msg(f"Voice error: {message}")
            if self._sm.state not in (VoiceState.STOPPED,):
                if self._sm.can(VoiceState.ERROR):
                    self._sm.to(VoiceState.ERROR)
                self._sm.to(VoiceState.IDLE)
