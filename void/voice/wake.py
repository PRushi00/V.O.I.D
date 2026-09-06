"""Provider-neutral wake-word detection boundary (Phase 9C-1).

A wake detector is an ACTIVATION signal source and nothing more. It consumes
16 kHz mono PCM audio frames and, when the wake phrase is detected, emits a
single opaque event: ``WAKE_DETECTED``. It has NO other authority - it never
calls Assistant.run(), executes tools, touches RiskGate / Task state / the
KillSwitch, modifies VoiceState, performs confirmation, or dispatches commands.
Treat its output as an untrusted signal.

This module mirrors the TTS provider layer (:mod:`void.voice.tts`): one small
backend-neutral interface, a safe null implementation, a real adapter whose
heavy dependency is imported lazily, and a name-keyed factory. Wiring a detector
into the microphone/session lifecycle is a LATER 9C phase and is deliberately
absent here - 9C-1 opens no microphone and starts no background thread.

V1 wake phrase (declared): "Hey V.O.I.D."  openWakeWord ships no pretrained
model for this phrase, so the OpenWakeWord adapter REQUIRES an explicit custom
model path and fails clearly and deterministically when it is missing. It never
substitutes a different phrase's model and never downloads anything.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from void.voice.adapters import VoiceDependencyError

# The wake detector's ONLY semantic output. It carries no command, authorization,
# risk level, task, session, or credential - just "the wake phrase fired".
WAKE_DETECTED = "wake_detected"

# The declared V1 wake phrase. openWakeWord has no pretrained model for it, so a
# custom model must be supplied via configuration (see OpenWakeWordDetector).
WAKE_PHRASE = "Hey V.O.I.D."

# Conservative default: a false negative is preferable to a false positive,
# because an accidental wake is a privacy event. Real tuning happens in 9C-5.
_DEFAULT_THRESHOLD = 0.6


class WakeWordError(RuntimeError):
    """Base class for wake-word detector failures."""


class WakeWordConfigError(WakeWordError):
    """The detector is misconfigured (e.g. missing/invalid custom model path)."""


class WakeWordBackendError(WakeWordError):
    """The backend failed to initialize or run inference."""


class WakeWordDetector:
    """Backend-neutral wake detector contract.

    Deterministic, idempotent lifecycle (mirrors the STT/TTS adapters):
      * feed_audio before start / after stop / after close -> ignored (no wake).
      * start / stop / close are safe to call repeatedly.
    Subclasses operate ONLY on audio frames; they own no application/session
    state. ``on_wake`` is invoked with the ``WAKE_DETECTED`` constant and nothing
    else.
    """

    def __init__(self, on_wake: Callable[[str], None] | None = None):
        self.on_wake = on_wake

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def feed_audio(self, frame) -> None:
        """Consume one 16 kHz mono PCM frame. Emits WAKE_DETECTED on a rising
        detection edge; a no-op unless the detector is started."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    # Shared, safe emission helper: passes ONLY the opaque wake constant.
    def _emit_wake(self) -> None:
        cb = self.on_wake
        if cb is not None:
            cb(WAKE_DETECTED)


class NullWakeDetector(WakeWordDetector):
    """A detector that never wakes. Safe default when wake is disabled or the
    backend is unavailable; also handy in tests. All lifecycle calls are no-ops."""

    def __init__(self, on_wake: Callable[[str], None] | None = None):
        super().__init__(on_wake)
        self.started = False
        self.closed = False

    def start(self) -> None:
        if self.closed:
            return
        self.started = True

    def stop(self) -> None:
        self.started = False

    def feed_audio(self, frame) -> None:
        return None                      # never emits

    def close(self) -> None:
        self.started = False
        self.closed = True


class OpenWakeWordDetector(WakeWordDetector):
    """openWakeWord-backed detector behind the neutral interface.

    Requires an explicit custom model path (``model_path``) for "Hey V.O.I.D." -
    openWakeWord ships no pretrained model for this phrase, so:
      * a missing/empty path -> WakeWordConfigError (deterministic),
      * a path that does not exist -> WakeWordConfigError,
      * the 'openwakeword' package being absent -> VoiceDependencyError,
      * backend init/inference failure -> WakeWordBackendError.
    Nothing is downloaded and no other phrase's model is ever substituted.

    Detection is edge-triggered: exactly one WAKE_DETECTED per rising crossing of
    ``threshold`` (it does not re-fire while the score stays above threshold).
    ``_model_factory`` is an internal test seam (inject a fake model); production
    passes only ``model_path`` / ``threshold`` / ``on_wake``.
    """

    def __init__(self, *, model_path: str | None, threshold: float = _DEFAULT_THRESHOLD,
                 model_name: str | None = None,
                 on_wake: Callable[[str], None] | None = None,
                 _model_factory: Callable[[], object] | None = None):
        super().__init__(on_wake)
        self._model_path = model_path
        self._threshold = float(threshold)
        self._model_name = model_name
        self._model_factory = _model_factory
        self._model = None
        self._running = False
        self._closed = False
        self._above = False              # rising-edge latch

    @property
    def threshold(self) -> float:
        return self._threshold

    def _load(self):
        if self._model is not None:
            return self._model
        if self._model_factory is not None:
            try:
                self._model = self._model_factory()
            except Exception as exc:
                raise WakeWordBackendError(
                    f"wake backend failed to initialize: {exc}") from exc
            return self._model
        # Real backend: explicit custom-model path is mandatory.
        if not self._model_path:
            raise WakeWordConfigError(
                "no wake-word model configured; set voice.wake_model_path to a "
                f"custom '{WAKE_PHRASE}' openWakeWord model.")
        if not Path(self._model_path).is_file():
            raise WakeWordConfigError(
                f"wake-word model not found: {self._model_path}")
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise VoiceDependencyError(
                "wake word needs the 'openwakeword' package "
                "(pip install -r requirements-voice.txt)") from exc
        try:
            self._model = Model(wakeword_models=[self._model_path])
        except Exception as exc:
            raise WakeWordBackendError(
                f"could not initialize openWakeWord model: {exc}") from exc
        return self._model

    def start(self) -> None:
        if self._closed or self._running:
            return
        self._load()                     # surfaces config/dep/backend errors here
        self._above = False
        self._running = True

    def stop(self) -> None:
        self._running = False
        self._above = False

    def feed_audio(self, frame) -> None:
        if self._closed or not self._running:
            return                        # ignored before start / after stop/close
        try:
            scores = self._model.predict(frame)
        except Exception as exc:
            raise WakeWordBackendError(f"wake inference failed: {exc}") from exc
        score = self._score_of(scores)
        if score >= self._threshold:
            if not self._above:
                self._above = True
                self._emit_wake()         # exactly one event per rising edge
        else:
            self._above = False

    def _score_of(self, scores) -> float:
        if not scores:
            return 0.0
        if self._model_name is not None and self._model_name in scores:
            return float(scores[self._model_name])
        try:
            return float(max(scores.values()))
        except (ValueError, TypeError):
            return 0.0

    def close(self) -> None:
        self._running = False
        self._closed = True
        self._model = None


# --- provider registry + factory (mirrors create_tts_provider) ----------
_PROVIDERS: dict[str, Callable[..., WakeWordDetector]] = {
    "openwakeword": OpenWakeWordDetector,
    "null": NullWakeDetector,
}


def register_wake_provider(name: str, factory: Callable[..., WakeWordDetector]) -> None:
    _PROVIDERS[str(name).strip().lower()] = factory


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def create_wake_detector(config=None, *, provider: str | None = None,
                         on_wake: Callable[[str], None] | None = None
                         ) -> WakeWordDetector:
    """Build a wake detector from config. Provider name resolves from the
    ``provider`` arg, then ``config.get('voice.wake_provider')``, else
    'openwakeword'. An unknown provider raises WakeWordConfigError (never a
    silent fallback). Construction is cheap/lazy; a missing custom model is
    surfaced deterministically at start(), not here."""
    def _cfg(key, default):
        if config is None:
            return default
        try:
            return config.get(key, default)
        except Exception:
            return default

    name = (provider or _cfg("voice.wake_provider", "openwakeword")).strip().lower()
    factory = _PROVIDERS.get(name)
    if factory is None:
        raise WakeWordConfigError(
            f"unknown wake provider {name!r}; known: {available_providers()}")
    if name == "null":
        return factory(on_wake=on_wake)
    if name == "openwakeword":
        return factory(
            model_path=_cfg("voice.wake_model_path", "") or None,
            threshold=_cfg("voice.wake_threshold", _DEFAULT_THRESHOLD),
            on_wake=on_wake,
        )
    return factory(on_wake=on_wake)
