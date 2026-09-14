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

import array
import queue
import threading
from dataclasses import dataclass
from typing import Callable

from void.voice.adapters import (
    BrokerCapture, FasterWhisperSTT, PTTActivation,
)
from void.voice.capture_broker import AudioCaptureBroker, create_audio_broker
from void.voice.session import VoiceSession
from void.voice.state import VoiceState
from void.voice.tts import create_tts_provider
from void.voice.wake import WAKE_DETECTED, create_wake_detector


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


# --- wake-initiated capture: deterministic endpointing --------------------

@dataclass(frozen=True)
class _WakePolicy:
    """Deterministic wake-capture endpointing parameters.

    Seconds, except ``rearm_delay_ms`` (ms) and ``energy_threshold`` (int16
    RMS). Wake has no physical PTT release, so a wake-initiated capture MUST end
    on one of three bounded conditions - never "listen forever".
    """
    no_speech_s: float = 4.0
    silence_s: float = 0.8          # trailing silence that ends an utterance (snappier endpoint)
    max_capture_s: float = 15.0
    rearm_delay_ms: int = 500
    energy_threshold: float = 500.0

    @classmethod
    def from_config(cls, config) -> "_WakePolicy":
        def _num(key, default, low):
            try:
                v = float(config.get(key, default))
            except (TypeError, ValueError, AttributeError):
                v = float(default)
            return v if v >= low else float(low)
        return cls(
            no_speech_s=_num("voice.wake_no_speech_timeout", 4.0, 0.5),
            silence_s=_num("voice.wake_silence_timeout", 0.8, 0.2),
            max_capture_s=_num("voice.wake_max_capture_seconds", 15.0, 1.0),
            rearm_delay_ms=int(_num("voice.wake_rearm_delay_ms", 500.0, 0.0)),
            energy_threshold=_num("voice.wake_energy_threshold", 500.0, 0.0),
        )


def _rms_int16(frame: bytes) -> float:
    """RMS amplitude of a 16-bit little-endian PCM frame (pure stdlib)."""
    a = array.array("h")
    try:
        a.frombytes(frame if len(frame) % 2 == 0 else frame[:-1])
    except ValueError:
        return 0.0
    if not a:
        return 0.0
    return (sum(v * v for v in a) / len(a)) ** 0.5


class _WakeEndpointer:
    """Energy-based endpointer for ONE wake-initiated capture.

    A broker consumer (16 kHz mono int16 PCM ``bytes``). It NEVER buffers audio
    for STT and never persists it - it only decides WHEN the capture ends, then
    calls ``on_finalize(reason)`` exactly once. Elapsed time is derived from
    frame durations (deterministic; no wall clock). Bounded exits:
      * no voiced frame within ``no_speech_s``            -> 'no_speech'
      * >= ``silence_s`` trailing silence after speech    -> 'silence'
      * >= ``max_capture_s`` total                        -> 'max_duration'
    "Voiced" is a plain RMS-energy gate (``energy_threshold``), not speaker or
    language classification; a richer local VAD can replace it later without
    touching the broker or the session.
    """

    def __init__(self, policy: _WakePolicy, on_finalize: Callable[[str], None],
                 *, sample_rate: int = 16000):
        self._p = policy
        self._on_finalize = on_finalize
        self._sr = sample_rate
        self._elapsed = 0.0
        self._trailing_silence = 0.0
        self._speech = False
        self._done = False

    def __call__(self, frame: bytes) -> None:
        if self._done:
            return
        n = len(frame) // 2
        if n <= 0:
            return
        secs = n / self._sr
        self._elapsed += secs
        if _rms_int16(frame) >= self._p.energy_threshold:
            self._speech = True
            self._trailing_silence = 0.0
        elif self._speech:
            self._trailing_silence += secs

        if not self._speech and self._elapsed >= self._p.no_speech_s:
            self._fire("no_speech")
        elif self._speech and self._trailing_silence >= self._p.silence_s:
            self._fire("silence")
        elif self._elapsed >= self._p.max_capture_s:
            self._fire("max_duration")

    def _fire(self, reason: str) -> None:
        self._done = True
        self._on_finalize(reason)


class VoiceController:
    def __init__(self, session: VoiceSession, activation, *,
                 poll_interval: float = 0.1, worker=None,
                 broker: AudioCaptureBroker | None = None,
                 wake=None, wake_policy: _WakePolicy | None = None,
                 on_message: Callable[[str], None] | None = None):
        self._session = session
        self._activation = activation
        self._poll_interval = poll_interval
        self._monitor: threading.Thread | None = None
        self._stop_evt = threading.Event()
        # Optional serial worker for the blocking release-chain. When absent
        # (e.g. unit tests), on_ptt_release runs inline exactly as before.
        self._worker = worker
        self._msg = on_message or (lambda _m: None)

        # --- wake + shared-broker integration (Phase 9C-3) ----------------
        # ALL of this is inert when broker or wake is None: the PTT path is
        # then byte-for-byte the pre-9C-3 behavior (unit tests pass unchanged).
        self._broker = broker
        self._wake = wake
        self._wake_policy = wake_policy or _WakePolicy()
        self._wake_consumer = wake.feed_audio if wake is not None else None
        if wake is not None:
            wake.on_wake = self._on_wake_detected     # the ONLY wake output path
        self._wake_lock = threading.RLock()
        self._wake_armed = False
        self._wake_broken = False           # start() failed once -> PTT-only
        self._wake_capture_active = False
        self._wake_gen: int | None = None
        self._endpointer: _WakeEndpointer | None = None
        self._idle_ticks = 0
        self._rearm_ticks = max(1, int(round(
            self._wake_policy.rearm_delay_ms
            / max(1.0, poll_interval * 1000.0))))

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
        # THE single physical microphone owner for the integrated runtime. Both
        # the wake detector and command capture consume from this one broker;
        # nothing else opens a sounddevice stream.
        broker = create_audio_broker(config)
        capture = BrokerCapture(broker)
        stt = FasterWhisperSTT(
            model_name=config.get("voice.stt_model", "small"),
            device=config.get("voice.stt_device", "cpu"),
            language=config.get("voice.stt_language", "en"),
            beam_size=int(config.get("voice.stt_beam_size", 1)),
            vad_filter=bool(config.get("voice.stt_vad_filter", True)),
        )
        tts = create_tts_provider(config)   # provider-agnostic; null-safe fallback
        session = VoiceSession(
            assistant, assistant.kill_switch,
            capture=capture, stt=stt, tts=tts,
            on_state=on_state, on_transcript=on_transcript,
            on_message=on_message,
            speak_response=config.get("voice.speak_responses", True),
        )
        wake = cls._build_wake(config)
        worker = _SerialVoiceWorker()
        controller = cls(session, None, poll_interval=poll_interval,
                         worker=worker, broker=broker, wake=wake,
                         wake_policy=_WakePolicy.from_config(config),
                         on_message=on_message)
        # PTT edges go through the controller: press is quick (mic open / barge-
        # in) and runs inline on the hook thread; release runs the blocking
        # STT/Assistant/TTS chain on the serial worker so the hook thread stays
        # responsive (real barge-in / new PTT remain observable during a reply).
        controller._activation = PTTActivation(
            controller.on_ptt_press, controller.on_ptt_release,
            hotkey=config.get("voice.ptt_hotkey", "ctrl+space"),
        )
        return controller

    @staticmethod
    def _build_wake(config):
        """Build a wake detector only when it can plausibly work: the 'null'
        provider (harmless), or 'openwakeword' WITH a custom model path. The
        default (openwakeword, no "Hey V.O.I.D." model) yields None - wake is
        simply inactive and push-to-talk is unaffected, with no noise. A
        detector whose start() later fails is disabled after one message."""
        try:
            provider = str(config.get("voice.wake_provider", "openwakeword")
                           ).strip().lower()
            model = config.get("voice.wake_model_path", "") or ""
        except Exception:
            return None
        if provider == "null" or (provider in ("openwakeword", "whisper_gen3") and model):
            try:
                return create_wake_detector(config)
            except Exception:
                return None
        return None

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
        if self._broker is not None:
            self._broker.start()          # the ONE physical microphone owner
        self._activation.start()
        if monitor and self._monitor is None:
            self._monitor = threading.Thread(
                target=self._run_monitor, name="void-voice-monitor",
                daemon=True)
            self._monitor.start()
        self._prewarm_stt()

    def _prewarm_stt(self) -> None:
        """Warm the STT model off-thread at start() so the first command isn't a
        cold start. Best-effort and daemonized; a failure never blocks start()."""
        warm = getattr(self._session, "warmup", None)
        if not callable(warm):
            return
        threading.Thread(target=warm, name="void-stt-warmup", daemon=True).start()

    def poll_once(self) -> None:
        """One deterministic monitor tick: kill-switch enforcement + speech
        retirement (session.poll), then wake arm/disarm reconciliation."""
        self._session.poll()
        self._reconcile_wake()

    def _run_monitor(self) -> None:
        # Cadence only; all decisions live in session.poll() / _reconcile_wake().
        # A failing tick must never kill the monitor (the kill switch must keep
        # being checked).
        while not self._stop_evt.wait(self._poll_interval):
            try:
                self._session.poll()
                self._reconcile_wake()
            except Exception:  # pragma: no cover - defensive
                pass

    def shutdown(self, reason: str = "voice shutdown") -> None:
        """Stop activation, disarm wake, join the monitor, drive the session to
        terminal CLOSED (releases the TTS worker/COM and detaches capture), then
        close the broker (releases the physical microphone backend)."""
        self._stop_evt.set()
        try:
            self._activation.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        with self._wake_lock:
            self._teardown_wake_locked()
        if self._monitor is not None:
            self._monitor.join(timeout=1.0)
            self._monitor = None
        # SHUTDOWN: halt mic/STT/TTS, invalidate generation, release resources.
        # Done before the worker stops so any late chain result is stale-dropped.
        self._session.close()
        if self._broker is not None:
            try:
                self._broker.close()     # closes the physical capture backend
            except Exception:  # pragma: no cover - defensive
                pass
        if self._worker is not None:
            self._worker.stop()

    # --- wake activation (Phase 9C-3) --------------------------------------
    #
    # Wake is an UNTRUSTED activation signal, equivalent to a PTT press: it
    # begins a capture only from IDLE and authorizes nothing. It drives the SAME
    # VoiceSession reducer + generation token; there is no second lifecycle.

    def _reconcile_wake(self) -> None:
        """Arm the detector only while the session is IDLE (and settled for
        ``rearm_delay_ms`` since the last non-IDLE state); disarm otherwise.
        Runs every monitor tick, after session.poll()."""
        if self._wake is None or self._broker is None:
            return
        with self._wake_lock:
            # A wake capture is only ever concluded by its own endpointer via
            # _wake_finalize. If the session has already left LISTENING by any
            # OTHER path (PTT_UP, KillSwitch, shutdown, error), that capture is
            # superseded: reconcile the orphaned flag + endpointer here so wake
            # can re-arm (and a late _wake_finalize sees _wake_capture_active
            # False and no-ops). _wake_finalize's generation guard is unchanged.
            if (self._wake_capture_active
                    and self._session.state != VoiceState.LISTENING):
                self._wake_capture_active = False
                if self._endpointer is not None:
                    self._safe_unsub(self._endpointer)
                    self._endpointer = None
            if self._wake_capture_active:
                self._idle_ticks = 0
                return
            idle = (self._session.state == VoiceState.IDLE
                    and not self._wake_broken)
            if idle:
                self._idle_ticks += 1
                if (not self._wake_armed
                        and self._idle_ticks >= self._rearm_ticks):
                    self._arm_wake_locked()
            else:
                self._idle_ticks = 0
                if self._wake_armed:
                    self._disarm_wake_locked()
                if self._endpointer is not None:      # defensive
                    self._safe_unsub(self._endpointer)
                    self._endpointer = None

    def _arm_wake_locked(self) -> None:
        # drain() first so a freshly-armed detector never sees residual audio
        # (e.g. this run's own TTS output) that predates arming.
        try:
            self._broker.drain(timeout=0.1)
            self._wake.start()               # may raise config/dep/backend errors
        except Exception as exc:
            self._wake_broken = True
            self._msg(f"(voice) wake word unavailable: {type(exc).__name__}. "
                      f"Push-to-talk still works.")
            return
        self._broker.subscribe(self._wake_consumer)
        self._wake_armed = True

    def _disarm_wake_locked(self) -> None:
        self._safe_unsub(self._wake_consumer)
        try:
            self._wake.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        self._wake_armed = False

    def _teardown_wake_locked(self) -> None:
        if self._wake is None:
            return
        if self._wake_armed:
            self._disarm_wake_locked()
        if self._endpointer is not None:
            self._safe_unsub(self._endpointer)
            self._endpointer = None
        self._wake_capture_active = False
        try:
            self._wake.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def _safe_unsub(self, consumer) -> None:
        try:
            self._broker.unsubscribe(consumer)
        except Exception:  # pragma: no cover - defensive
            pass

    def _on_wake_detected(self, event: str) -> None:
        """The detector's ONLY output path. Runs on the broker pump thread.
        Begins a capture only from IDLE; hands the blocking work to the worker."""
        if event != WAKE_DETECTED:
            return
        with self._wake_lock:
            if (self._wake_capture_active
                    or self._session.state != VoiceState.IDLE):
                return                       # stale / overlapping wake -> ignore
            self._wake_capture_active = True
            self._wake_gen = self._session.generation   # provisional; re-bound post-press
        job = self._begin_wake_capture
        if self._worker is not None:
            self._worker.submit(job)         # off the pump thread
        else:
            job()                            # tests without a worker: inline

    def _begin_wake_capture(self) -> None:
        """Off the pump thread. Disarm the detector, then begin capture exactly
        like a PTT press (same reducer, same NEW_GENERATION), then attach the
        endpointer for THIS capture."""
        with self._wake_lock:
            if not self._wake_capture_active:
                return
            if self._wake_armed:
                self._disarm_wake_locked()
        self._session.on_ptt_press()         # IDLE -> LISTENING: NEW_GENERATION + MIC_OPEN
        if self._session.state != VoiceState.LISTENING:
            with self._wake_lock:            # e.g. KillSwitch coerced the press
                self._wake_capture_active = False
            return
        endpointer = _WakeEndpointer(self._wake_policy, self._wake_finalize)
        with self._wake_lock:
            # bind to the generation THIS capture created (post NEW_GENERATION),
            # so a later barge-in / killswitch / shutdown marks the finalize stale
            self._wake_gen = self._session.generation
            self._endpointer = endpointer
        self._broker.subscribe(endpointer)

    def _wake_finalize(self, reason: str) -> None:
        """Endpointer callback (broker pump thread). End the wake capture the
        same way a PTT release does - via the serial worker - unless the session
        generation has already moved on."""
        with self._wake_lock:
            if not self._wake_capture_active:
                return
            self._wake_capture_active = False
            endpointer, self._endpointer = self._endpointer, None
            stale = self._session.generation != self._wake_gen
        if endpointer is not None:
            self._safe_unsub(endpointer)
        if stale:
            return                           # a newer session owns the mic now
        self.on_ptt_release()                # -> worker: finalize + STT + dispatch + speak

    @property
    def stopped(self) -> bool:
        """True once the session has reached the KillSwitch-latched STOPPED."""
        return self._session.state == VoiceState.STOPPED

    @property
    def closed(self) -> bool:
        """True once the session has reached the terminal CLOSED state."""
        return self._session.state == VoiceState.CLOSED
