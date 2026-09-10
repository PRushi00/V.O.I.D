"""Voice I/O adapters (Phase 9A): activation, audio capture, STT, TTS.

Base classes define the contracts the VoiceSession depends on; the real
implementations import their heavy/Windows-only libraries LAZILY (inside
methods), so importing this module never requires the optional voice stack.
Tests drive the session with fakes; the real adapters are exercised only by the
Windows live smoke test (verify/voice_smoke.py).
"""
from __future__ import annotations

import threading
from typing import Callable


class VoiceDependencyError(RuntimeError):
    """A required optional voice dependency is not installed."""


class CaptureError(RuntimeError):
    pass


class STTError(RuntimeError):
    pass


class TTSError(RuntimeError):
    pass


# --- activation boundary ------------------------------------------------
#
#   ActivationAdapter
#       |-- PTTActivation            (this phase)
#       `-- WakeWordActivation       (future - same callbacks, same session)

class ActivationAdapter:
    """Feeds press/release edges to the VoiceSession. A future wake-word
    adapter implements the SAME interface and drives the SAME session - no
    change to the Agent/execution layer."""

    def __init__(self, on_press: Callable[[], None],
                 on_release: Callable[[], None]):
        self._on_press = on_press
        self._on_release = on_release

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class PTTActivation(ActivationAdapter):
    """Global push-to-talk hotkey (press = begin, release = finalize).

    The OS delivers a stream of raw key events, not clean press/release edges:
    holding a key produces auto-REPEAT key-down events (and, on some Windows
    setups, synthesized key-up events between repeats). Forwarding those raw
    events one-to-one would start a new activation on every repeat and finalize
    capture mid-hold. This adapter therefore tracks the PHYSICAL hold state and
    emits exactly one on_press per physical press and one on_release per
    physical release:

      * key-down while already held  -> ignored (auto-repeat), silently.
      * key-up while the key is still physically down -> ignored (repeat
        artifact); capture keeps running until the real release.

    A single physical press/hold => exactly one capture session.
    """

    def __init__(self, on_press, on_release, hotkey: str = "ctrl+space",
                 key_is_down: Callable[[], bool] | None = None):
        super().__init__(on_press, on_release)
        self._hotkey = hotkey
        self._kb = None
        self._held = False              # our own edge tracking (physical hold)
        self._lock = threading.Lock()   # keyboard callbacks fire off-thread
        # Predicate answering "is the PTT key still physically down?", used to
        # reject spurious key-up events during auto-repeat. Injected in tests;
        # bound to keyboard.is_pressed at start().
        self._key_is_down = key_is_down

    def _key_token(self) -> str:
        # The library hooks a single key; use the final chord token (e.g.
        # "ctrl+space" -> "space"). Chord/modifier matching is out of scope.
        return self._hotkey.split("+")[-1].strip().lower()

    # --- event normalization (edge-triggered, deterministic) ----------
    def _handle_key_down(self) -> None:
        with self._lock:
            if self._held:
                return                  # auto-repeat: ignore, no on_press, no noise
            self._held = True
        self._on_press()

    def _handle_key_up(self) -> None:
        with self._lock:
            if not self._held:
                return                  # release with no active capture: no-op
            # A key-up while the key is still physically down is a repeat
            # artifact - keep capturing until the genuine release.
            if self._key_is_down is not None and self._key_is_down():
                return
            self._held = False
        self._on_release()

    def start(self) -> None:
        try:
            import keyboard  # optional dep
        except ImportError as exc:
            raise VoiceDependencyError(
                "push-to-talk needs the 'keyboard' package "
                "(pip install -r requirements-voice.txt)") from exc
        self._kb = keyboard
        token = self._key_token()
        if self._key_is_down is None:
            self._key_is_down = lambda: bool(self._kb and self._kb.is_pressed(token))
        keyboard.on_press_key(token, lambda _e: self._handle_key_down())
        keyboard.on_release_key(token, lambda _e: self._handle_key_up())

    def stop(self) -> None:
        if self._kb is not None:
            try:
                self._kb.unhook_all()
            finally:
                self._kb = None
        with self._lock:
            self._held = False


# --- audio capture ------------------------------------------------------

class AudioCapture:
    """Owns the microphone for exactly one capture. Closed unless capturing."""

    @property
    def is_open(self) -> bool:
        raise NotImplementedError

    def open(self) -> None:
        raise NotImplementedError

    def stop(self):
        """Finalize and return captured audio (implementation-defined type)."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # Context-manager guarantees the mic is released even on exceptions.
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class MicAudioCapture(AudioCapture):
    """Real microphone capture via sounddevice (lazy)."""

    def __init__(self, samplerate: int = 16000, channels: int = 1):
        self._samplerate = samplerate
        self._channels = channels
        self._stream = None
        self._frames: list = []
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        try:
            import numpy  # noqa: F401  (used by the callback / STT)
            import sounddevice as sd
        except ImportError as exc:
            raise VoiceDependencyError(
                "microphone capture needs 'sounddevice' and 'numpy' "
                "(pip install -r requirements-voice.txt)") from exc
        self._frames = []

        def _cb(indata, _frames, _time, _status):
            self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self._samplerate, channels=self._channels,
            dtype="float32", callback=_cb)
        self._stream.start()
        self._open = True

    def stop(self):
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover
            raise VoiceDependencyError("numpy required") from exc
        self.close()
        if not self._frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self._frames, axis=0).reshape(-1)

    def close(self) -> None:
        # Robust cleanup - never leave the mic open on error paths.
        try:
            if self._stream is not None:
                try:
                    self._stream.stop()
                finally:
                    self._stream.close()
        finally:
            self._stream = None
            self._open = False


class BrokerCapture(AudioCapture):
    """AudioCapture backed by the shared AudioCaptureBroker - no device of its own.

    The broker (:mod:`void.voice.capture_broker`) is the SINGLE physical
    microphone owner for the integrated voice runtime (Phase 9C-3). This adapter
    lets the unchanged VoiceSession drive command capture through that one
    broker: ``open()`` subscribes a frame collector, ``stop()`` detaches it and
    returns the buffered command audio, ``close()`` detaches with nothing
    returned. It NEVER opens a ``sounddevice`` stream.

    Capture begins at ``open()``: a short, bounded ``broker.drain()`` first
    flushes any already-queued frames to the current subscribers (there are none
    at that instant), so the collector only ever receives frames produced AFTER
    ``open()`` - no pre-wake / pre-press audio can enter the command buffer, and
    the broker's own bounded drop-oldest intake means nothing older survives
    anyway. Frames are the broker contract (16 kHz mono int16 LE PCM ``bytes``);
    ``stop()`` converts them to the float32 mono array faster-whisper expects AT
    THIS STT-INPUT BOUNDARY (the broker format itself is unchanged). Nothing is
    persisted or logged.
    """

    _OPEN_DRAIN_TIMEOUT = 0.1     # bounded; only ever runs off the broker pump thread

    def __init__(self, broker):
        self._broker = broker
        self._frames: list[bytes] = []
        self._open = False
        # One stable consumer identity for the broker's identity-keyed (un)subscribe.
        self._consumer = self._collect

    @property
    def is_open(self) -> bool:
        return self._open

    def _collect(self, frame: bytes) -> None:
        if self._open:
            self._frames.append(frame)

    def open(self) -> None:
        try:
            import numpy  # noqa: F401  (int16 bytes -> float32 for STT, in stop())
        except ImportError as exc:
            raise VoiceDependencyError(
                "voice capture needs 'numpy' "
                "(pip install -r requirements-voice.txt)") from exc
        self._frames = []
        try:
            self._broker.drain(timeout=self._OPEN_DRAIN_TIMEOUT)
        except Exception:
            pass                      # best-effort flush; open() must not block
        self._broker.subscribe(self._consumer)
        self._open = True

    def stop(self):
        import numpy as np
        frames, self._frames = self._frames, []
        self._open = False
        try:
            self._broker.unsubscribe(self._consumer)
        except Exception:
            pass
        if not frames:
            return np.zeros(0, dtype="float32")
        pcm = np.frombuffer(b"".join(frames), dtype="<i2")
        return pcm.astype(np.float32) / 32768.0

    def close(self) -> None:
        try:
            self._broker.unsubscribe(self._consumer)
        finally:
            self._open = False
            self._frames = []


# --- speech to text -----------------------------------------------------

class STT:
    def transcribe(self, audio) -> str:
        raise NotImplementedError


class FasterWhisperSTT(STT):
    """Local faster-whisper STT (lazy init, configurable model, default small)."""

    def __init__(self, model_name: str = "small", device: str = "cpu",
                 compute_type: str = "int8", language: str = "en"):
        self._model_name = model_name
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._model = None

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceDependencyError(
                "local STT needs 'faster-whisper' "
                "(pip install -r requirements-voice.txt)") from exc
        try:
            self._model = WhisperModel(
                self._model_name, device=self._device,
                compute_type=self._compute_type)
        except Exception as exc:   # model download / init failure
            raise STTError(f"could not initialize STT model "
                           f"'{self._model_name}': {exc}") from exc
        return self._model

    def transcribe(self, audio) -> str:
        model = self._load()
        try:
            segments, _info = model.transcribe(audio, language=self._language)
            return "".join(seg.text for seg in segments).strip()
        except Exception as exc:
            raise STTError(f"transcription failed: {exc}") from exc


# --- text to speech -----------------------------------------------------

class TTS:
    """Provider-agnostic text-to-speech contract.

    Minimal by design: ``speak`` starts speech asynchronously (replacing any
    current utterance), ``stop`` is a responsive, idempotent interrupt, and
    ``is_speaking`` converges to False after completion/stop/failure/shutdown.
    ``close`` releases provider resources (default: nothing). No queue, pause,
    resume, volume, or provider-specific surface is exposed.
    """

    @property
    def is_speaking(self) -> bool:
        raise NotImplementedError

    def speak(self, text: str) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        """Release any provider resources. Default: nothing to release."""
        return None


class SapiTTS(TTS):
    """Interruptible local Windows SAPI TTS.

    ALL SAPI/COM work happens on ONE dedicated owner thread that CoInitializes
    COM and owns the ``SpVoice`` for its whole life; no COM object ever crosses
    a thread boundary. The public ``speak``/``stop``/``is_speaking``/``close``
    are provider-agnostic and thread-safe: callers only record intent into a
    single latest-command slot (guarded by a lock) and wake the worker - they
    never call COM. A monotonic command sequence makes the semantics
    deterministic:

      * speak while idle       -> start speech asynchronously.
      * speak while speaking    -> REPLACE (purge current, start new); no queue.
      * stop while speaking     -> purge current; no further utterance starts.
      * stop while idle / repeat-> safe no-op.
      * stop arriving before the worker starts audio -> no audio starts at all
        (the pre-start supersede check honours the latest-command slot).

    The heavy imports (pywin32 / pythoncom) are lazy; a missing dependency is
    surfaced as VoiceDependencyError on first speak(), which the resilient TTS
    wrapper turns into a non-fatal TTSError.

    The ``_voice_factory`` / ``_com_setup`` / ``_com_teardown`` / ``_poll_ms`` /
    ``_before_start`` parameters are internal seams for deterministic tests
    (inject a fake SpVoice, skip real COM, force a start/stop race); production
    code constructs ``SapiTTS()`` with no arguments.
    """

    _SVSF_ASYNC = 1
    _SVSF_PURGE = 2
    _POLL_MS = 50            # worker responsiveness to stop/replace/shutdown

    def __init__(self, *, _voice_factory=None, _com_setup=None,
                 _com_teardown=None, _poll_ms=None, _before_start=None):
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._ready = threading.Event()      # worker finished init (ok or error)
        self._idle = threading.Event()       # set whenever speech is not running
        self._idle.set()
        self._latest = None                  # (kind, text, seq): newest request
        self._seq = 0
        self._speaking = False
        self._shutdown = False
        self._closed = False
        self._started = False
        self._worker: threading.Thread | None = None
        self._start_error: Exception | None = None
        # test seams (None in production)
        self._voice_factory = _voice_factory
        self._com_setup = _com_setup
        self._com_teardown = _com_teardown
        self._poll_ms = _poll_ms or self._POLL_MS
        self._before_start = _before_start

    # --- public, provider-agnostic API --------------------------------
    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def speak(self, text: str) -> None:
        with self._lock:
            if self._closed:
                return                       # terminal: never speak after close
        self._ensure_worker()                # may raise (missing dep / init fail)
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            self._latest = ("speak", text, self._seq)
            self._speaking = True            # optimistic: intent to speak
            self._idle.clear()
            self._wake.set()

    def stop(self) -> None:
        # Idempotent and non-blocking: only records intent; never joins/blocks.
        with self._lock:
            if self._closed or not self._started:
                self._speaking = False
                return
            self._seq += 1
            self._latest = ("stop", None, self._seq)
            self._speaking = False           # converge immediately on request
            self._wake.set()

    def close(self) -> None:
        with self._lock:
            worker = self._worker
            self._closed = True
            self._shutdown = True
            self._speaking = False
            self._wake.set()
        if worker is not None:
            worker.join(timeout=2.0)         # bounded: worker ticks every poll
        with self._lock:
            self._worker = None
            self._started = False
            self._idle.set()

    # --- worker lifecycle ---------------------------------------------
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._started:
                if self._start_error is not None:
                    raise self._start_error
                return
            if self._voice_factory is None:
                # Fail fast with a clean error if the dependency is absent.
                try:
                    import pythoncom  # noqa: F401
                    import win32com.client  # noqa: F401
                except ImportError as exc:
                    self._started = True
                    self._start_error = VoiceDependencyError(
                        "Windows TTS needs pywin32 "
                        "(pip install -r requirements.txt)")
                    raise self._start_error from exc
            self._started = True
            self._ready.clear()
            self._worker = threading.Thread(
                target=self._run, name="void-tts-sapi", daemon=True)
            self._worker.start()
        # Wait (bounded) for init so a Dispatch failure surfaces synchronously.
        self._ready.wait(timeout=5.0)
        with self._lock:
            if self._start_error is not None:
                raise self._start_error

    def _make_voice(self):
        if self._voice_factory is not None:
            return self._voice_factory()
        import win32com.client
        return win32com.client.Dispatch("SAPI.SpVoice")

    def _run(self) -> None:
        setup = self._com_setup
        teardown = self._com_teardown
        if setup is None or teardown is None:
            import pythoncom
            setup = setup or pythoncom.CoInitialize
            teardown = teardown or pythoncom.CoUninitialize
        try:
            setup()
        except Exception as exc:              # COM init failure
            with self._lock:
                self._start_error = TTSError(f"COM init failed: {exc}")
            self._ready.set()
            return
        voice = None
        try:
            voice = self._make_voice()
        except Exception as exc:
            with self._lock:
                self._start_error = TTSError(
                    f"could not initialize SAPI voice: {exc}")
            self._ready.set()
            try:
                teardown()
            except Exception:
                pass
            return
        self._ready.set()
        try:
            self._loop(voice)
        finally:
            try:
                teardown()
            except Exception:
                pass

    def _purge(self, voice) -> None:
        try:
            voice.Speak("", self._SVSF_ASYNC | self._SVSF_PURGE)
        except Exception:
            pass

    def _loop(self, voice) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            with self._lock:
                if self._shutdown:
                    break
                cmd, self._latest = self._latest, None
            if cmd is None:
                continue
            kind, text, seq = cmd
            if kind == "stop":
                self._purge(voice)
                with self._lock:
                    self._speaking = False
                    if self._latest is None:
                        self._idle.set()
                continue
            # kind == "speak": if a newer command already arrived, don't start.
            if self._before_start is not None:
                self._before_start()          # test seam to force a race
            with self._lock:
                superseded = self._latest is not None or self._shutdown
            if superseded:
                continue                      # newer stop/speak wins; no audio
            self._purge(voice)                # replace anything currently playing
            try:
                voice.Speak(text, self._SVSF_ASYNC)
            except Exception:
                with self._lock:
                    if self._latest is None:
                        self._speaking = False
                        self._idle.set()
                continue
            self._await_utterance(voice)

    def _await_utterance(self, voice) -> None:
        while True:
            try:
                done = bool(voice.WaitUntilDone(self._poll_ms))
            except Exception:
                done = True
            with self._lock:
                interrupted = self._latest is not None or self._shutdown
                if not interrupted and done:
                    self._speaking = False
                    self._idle.set()
            if interrupted:
                self._purge(voice)            # cut current; outer loop handles next
                return
            if done:
                return
