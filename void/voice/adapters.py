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
    @property
    def is_speaking(self) -> bool:
        raise NotImplementedError

    def speak(self, text: str) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class SapiTTS(TTS):
    """Interruptible local Windows SAPI TTS via win32com (verified by the
    Phase 9A SAPI spike: async speak + immediate purge from another path)."""

    _SVSF_ASYNC = 1
    _SVSF_PURGE = 2
    _RS_SPEAKING = 2

    def __init__(self):
        self._voice = None

    def _load(self):
        if self._voice is not None:
            return self._voice
        try:
            import win32com.client
        except ImportError as exc:
            raise VoiceDependencyError(
                "Windows TTS needs pywin32 (pip install -r requirements.txt)"
            ) from exc
        try:
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
        except Exception as exc:
            raise TTSError(f"could not initialize SAPI voice: {exc}") from exc
        return self._voice

    @property
    def is_speaking(self) -> bool:
        if self._voice is None:
            return False
        try:
            return self._voice.Status.RunningState == self._RS_SPEAKING
        except Exception:
            return False

    def speak(self, text: str) -> None:
        v = self._load()
        try:
            v.Speak(text, self._SVSF_ASYNC)   # async: returns immediately
        except Exception as exc:
            raise TTSError(f"speak failed: {exc}") from exc

    def stop(self) -> None:
        if self._voice is None:
            return
        try:
            # Purge queue + stop current utterance immediately.
            self._voice.Speak("", self._SVSF_ASYNC | self._SVSF_PURGE)
        except Exception:
            pass
