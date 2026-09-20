"""VoiceSession: the single authoritative owner of the voice-session lifecycle.

PTT edges / STT / dispatch / TTS results are turned into EVENTS and fed to one
pure reducer (:mod:`void.voice.state`), which returns the next state plus the
side-effect commands the session executes. Voice is an I/O ADAPTER: it never
interprets intent, calls tools/RiskGate, approves confirmations, inspects task
status, or creates a second agent. The existing Assistant/Agent/RiskGate/
KillSwitch/Task machinery remains authoritative.

Authoritative invariants (Phase 9B step 3):
  * ONE owner of (state, generation), guarded by one lock.
  * A monotonic generation token is bumped when a session begins and whenever
    the current session is invalidated (barge-in/restart, KillSwitch, shutdown).
    Every async result carries the generation it started with; a result whose
    generation no longer matches is dropped - it cannot change state, dispatch,
    speak, or resurrect IDLE/LISTENING/SPEAKING/STOPPED/CLOSED.
  * DISPATCHED is single-flight: a new PTT press is ignored (never queued), and
    only one Assistant.run() is ever in flight through the voice path.
  * STOPPED is latched (KillSwitch); only an explicit non-voice re-arm leaves it.
  * CLOSED is terminal; no later event starts a voice session.
  * Task state lives entirely outside this object; dispatch is a one-way handoff
    of a transcript string in and a finalized response string out.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Callable

from void import perf

from void.voice.adapters import STTError, TTSError
from void.voice.state import VoiceCommand, VoiceEvent, VoiceState, reduce_voice

# Lifecycle diagnostics -> local diagnostic log. Privacy: only stage names and
# sizes/flags; NEVER the audio, the transcript text, or the response text.
_log = logging.getLogger("void.voice.session")


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
        self._on_state = on_state or (lambda _s: None)
        self._on_transcript = on_transcript or (lambda _t: None)
        self._msg = on_message or (lambda _m: None)
        self._speak_response = speak_response
        self._lock = threading.RLock()
        self._state = VoiceState.IDLE
        self._generation = 0     # bumped on new session / invalidation
        self._last_transcript: str | None = None
        self._last_result = None
        self._pending_audio = None
        self._pending_response = ""
        # SPEAKING retirement (async TTS): only retire once we have observed the
        # backend speaking, or after a small grace window (guards the race where
        # async speak() has returned but the backend has not started yet).
        self._speak_grace = speak_grace_seconds
        self._now = now or time.monotonic
        self._speech_seen = False
        self._speaking_since = 0.0
        # Performance-telemetry correlation (V2.0 T0.7). One id per voice session
        # (minted with the generation at PTT_DOWN), bound in the worker thread around
        # STT / dispatch / speak so the agent's own events join the same chain.
        # Telemetry only: never consulted for any decision.
        self._interaction_id: str | None = None
        self._activation_source = "ptt"
        self._endpoint_reason = "ptt_release"
        self._capture_started: float | None = None

    # --- observability ------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def last_transcript(self) -> str | None:
        return self._last_transcript

    # --- public entry points (translate to lifecycle events) ----------
    def on_ptt_press(self, source: str = "ptt") -> None:
        """``source`` (ptt|wake) is telemetry only: it labels the activation event."""
        self._activation_source = source
        self._apply(VoiceEvent.PTT_DOWN)

    def note_endpoint_reason(self, reason: str) -> None:
        """Telemetry only: why an automatic (wake) capture ended."""
        if reason in ("silence", "no_speech", "max_duration"):
            self._endpoint_reason = reason

    def on_ptt_release(self) -> None:
        self._apply(VoiceEvent.PTT_UP)

    def notify_speech_finished(self) -> None:
        with self._lock:
            gen = self._generation
        self._apply(VoiceEvent.TTS_DONE, gen=gen)

    def stop(self, reason: str = "kill switch") -> None:
        """KillSwitch entry (authoritative from ANY state). Latches STOPPED."""
        self._apply(VoiceEvent.KILLSWITCH)
        self._msg(f"Voice stopped ({reason}).")

    def close(self) -> None:
        """Shutdown: release voice I/O resources and reach terminal CLOSED.
        Lifecycle only - no task authority."""
        self._apply(VoiceEvent.SHUTDOWN)

    def reset(self) -> None:
        """Explicit non-voice re-arm: STOPPED -> IDLE, only if the kill switch
        is clear. Never leaves CLOSED."""
        with self._lock:
            if getattr(self._ks, "engaged", False):
                self._msg("Cannot reset voice while the kill switch is engaged.")
                return
        self._apply(VoiceEvent.REARM)

    def warmup(self) -> None:
        """Best-effort, off-thread pre-warm so the FIRST interaction has no
        cold start: load the STT model (+ one tiny dummy decode). Never raises;
        touches no lifecycle state, opens no microphone, and speaks nothing."""
        warm = getattr(self._stt, "warmup", None)
        if callable(warm):
            try:
                warm()
            except Exception:
                pass

    def poll(self) -> None:
        """Live-loop tick (runtime monitor cadence). Two jobs, both idempotent:
        make the GLOBAL kill switch authoritative from any state, and retire
        async speech (SPEAKING -> IDLE) once the backend has finished."""
        with self._lock:
            st = self._state
            if st in (VoiceState.STOPPED, VoiceState.CLOSED):
                return
            engaged = bool(getattr(self._ks, "engaged", False))
        if engaged:
            self.stop("kill switch")
            return
        with self._lock:
            if self._state != VoiceState.SPEAKING:
                return
            try:
                speaking = bool(self._tts.is_speaking)
            except Exception:
                speaking = False
            if speaking:
                self._speech_seen = True
                return
            retire = (self._speech_seen
                      or (self._now() - self._speaking_since) >= self._speak_grace)
            gen = self._generation
        if retire:
            self._apply(VoiceEvent.TTS_DONE, gen=gen)

    # --- the one authoritative event application ----------------------
    def _apply(self, event: str, *, gen: int | None = None,
               text: str | None = None) -> None:
        after: list[Callable[[], None]] = []
        with self._lock:
            # KillSwitch is authoritative from any active state, for any event:
            # coerce so a late async result arriving after an engaged switch
            # cannot slip through before the monitor's next tick.
            if (event not in (VoiceEvent.SHUTDOWN, VoiceEvent.REARM)
                    and self._state not in (VoiceState.STOPPED, VoiceState.CLOSED)
                    and getattr(self._ks, "engaged", False)):
                event = VoiceEvent.KILLSWITCH
                gen = None

            # Stale async result: its generation no longer matches -> drop it
            # entirely (no state change, no side effects).
            if gen is not None and gen != self._generation:
                return

            prev = self._state
            new_state, commands = reduce_voice(prev, event)
            if new_state == prev and not commands:
                return                                  # deterministic ignore

            # transcript is stored + surfaced before dispatch begins
            if event == VoiceEvent.STT_OK and text is not None:
                self._last_transcript = text
                after.append(lambda t=text: self._on_transcript(t))

            for c in commands:
                if c == VoiceCommand.NEW_GENERATION:
                    self._generation += 1
                    if event == VoiceEvent.PTT_DOWN:          # a new session (not an invalidation)
                        self._interaction_id = perf.new_interaction_id()
                        self._endpoint_reason = "ptt_release"
                        self._perf("activation", source=self._activation_source)
                elif c == VoiceCommand.MIC_OPEN:
                    self._capture_started = self._now()
                    if not self._safe_open_mic():
                        g = self._generation
                        after.append(lambda g=g: self._apply(
                            VoiceEvent.INTERNAL_ERROR, gen=g))
                elif c == VoiceCommand.CAPTURE_FINALIZE:
                    if self._capture_started is not None:
                        self._perf("endpoint", reason=self._endpoint_reason,
                                   capture_s=round(self._now() - self._capture_started, 3))
                        self._capture_started = None
                    ok, audio = self._safe_finalize_capture()
                    g = self._generation
                    if ok:
                        self._pending_audio = audio
                        after.append(lambda g=g: self._apply(
                            VoiceEvent.BEGIN_STT, gen=g))
                    else:
                        after.append(lambda g=g: self._apply(
                            VoiceEvent.CAPTURE_FAILED, gen=g))
                elif c == VoiceCommand.MIC_CLOSE:
                    self._safe_close_mic()
                elif c == VoiceCommand.TTS_STOP:
                    self._safe_tts_stop()
                elif c == VoiceCommand.TTS_CLOSE:
                    self._safe_tts_close()
                elif c == VoiceCommand.RUN_STT:
                    g = self._generation
                    after.append(lambda g=g: self._in_interaction(self._run_stt, g))
                elif c == VoiceCommand.RUN_DISPATCH:
                    g = self._generation
                    after.append(lambda g=g: self._in_interaction(self._run_dispatch, g))
                elif c == VoiceCommand.SPEAK:
                    self._speech_seen = False
                    self._speaking_since = self._now()
                    g = self._generation
                    after.append(lambda g=g: self._in_interaction(self._run_speak, g))
                elif c == VoiceCommand.WARN_EMPTY:
                    self._msg("No speech detected.")

            if event == VoiceEvent.TTS_DONE and prev == VoiceState.SPEAKING:
                self._perf("speak", dur_s=round(self._now() - self._speaking_since, 3))
            self._set_state(new_state)
            # ERROR is transient: clean up (above) then recover to IDLE, unless a
            # higher-priority transition (KillSwitch/shutdown) supersedes it.
            if new_state == VoiceState.ERROR:
                g = self._generation
                after.append(lambda g=g: self._apply(VoiceEvent.RECOVER, gen=g))

        # Blocking / follow-up work runs OUTSIDE the lock, in order.
        for cont in after:
            cont()

    # --- telemetry helpers (no decision ever depends on these) --------
    def _perf(self, event: str, **fields) -> None:
        if self._interaction_id:
            fields.setdefault("interaction_id", self._interaction_id)
        perf.emit(event, **fields)

    def _in_interaction(self, fn, *args) -> None:
        """Run a blocking step with this session's interaction id bound, so the
        agent/provider/tool events emitted underneath join the same chain."""
        cm = (perf.interaction(self._interaction_id) if self._interaction_id
              else contextlib.nullcontext())
        with cm:
            fn(*args)

    # --- blocking pipeline steps (run outside the lock) ---------------
    def _run_stt(self, gen: int) -> None:
        audio = self._pending_audio
        try:
            n = len(audio)
        except Exception:
            n = -1
        _log.info("STT_STARTED (audio_samples=%s)", n)
        t0 = time.monotonic()
        try:
            transcript = self._stt.transcribe(audio)
        except STTError as exc:
            _log.warning("STT_FAILED: %s", type(exc).__name__)
            self._apply(VoiceEvent.STT_FAILED, gen=gen)
            return
        except Exception as exc:
            _log.warning("STT_FAILED: %s", type(exc).__name__)
            self._apply(VoiceEvent.STT_FAILED, gen=gen)
            return
        transcript = (transcript or "").strip()
        stt_fields = {"decode_s": round(time.monotonic() - t0, 3), "empty": not transcript,
                      "backend": type(self._stt).__name__}
        if n >= 0:
            stt_fields["audio_s"] = round(n / 16000.0, 3)
        self._perf("stt", **stt_fields)
        if not transcript:
            _log.info("STT_DONE empty=True (no command recognized)")
            self._apply(VoiceEvent.STT_EMPTY, gen=gen)
        else:
            _log.info("STT_DONE empty=False (len=%d chars)", len(transcript))
            self._apply(VoiceEvent.STT_OK, gen=gen, text=transcript)

    def _run_dispatch(self, gen: int) -> None:
        transcript = self._last_transcript or ""
        # One-way handoff to the EXISTING execution pipeline. The response is
        # treated as opaque finalized output; task/RiskGate/confirmation state
        # is NOT inspected here.
        _log.info("DISPATCH_STARTED (transcript_len=%d)", len(transcript))
        try:
            result = self._assistant.run(transcript)
        except Exception as exc:
            _log.warning("DISPATCH_FAILED: %s: %s", type(exc).__name__, exc)
            self._msg(f"(voice) dispatch failed: {exc}")
            self._apply(VoiceEvent.DISPATCH_FAILED, gen=gen)
            return
        self._last_result = result
        text = getattr(result, "result", None) or ""
        self._pending_response = text
        self._perf("respond", kind="llm_text" if text else "silent")
        if self._speak_response and text:
            _log.info("DISPATCH_OK -> SPEAK (response_len=%d)", len(text))
            self._apply(VoiceEvent.DISPATCH_OK_SPEAK, gen=gen)
        else:
            _log.info("DISPATCH_OK -> silent (has_text=%s speak=%s)",
                      bool(text), self._speak_response)
            self._apply(VoiceEvent.DISPATCH_OK_SILENT, gen=gen)

    def _run_speak(self, gen: int) -> None:
        _log.info("SPEAK_STARTED")
        self._perf("speak", chars=len(self._pending_response or ""))
        try:
            self._tts.speak(self._pending_response)
        except TTSError as exc:
            _log.warning("SPEAK_FAILED: %s", type(exc).__name__)
            self._msg(f"(voice) could not speak the response: {exc}")
            self._apply(VoiceEvent.SPEAK_FAILED, gen=gen)

    # --- guarded side effects -----------------------------------------
    def _set_state(self, new_state: str) -> None:
        if new_state != self._state:
            self._state = new_state
            self._on_state(new_state)

    def _safe_open_mic(self) -> bool:
        try:
            self._capture.open()
            return True
        except Exception as exc:
            self._safe_close_mic()
            self._msg(f"(voice) microphone unavailable: {exc}")
            return False

    def _safe_finalize_capture(self):
        try:
            return True, self._capture.stop()      # finalize + close mic
        except Exception as exc:
            self._safe_close_mic()
            self._msg(f"(voice) capture failed: {exc}")
            return False, None

    def _safe_close_mic(self) -> None:
        try:
            self._capture.close()
        except Exception:
            pass

    def _safe_tts_stop(self) -> None:
        try:
            self._tts.stop()
        except Exception:
            pass

    def _safe_tts_close(self) -> None:
        try:
            self._tts.close()
        except Exception:
            pass
