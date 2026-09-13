"""Gen 3 wake-word detector: frozen Whisper-small encoder + a small
conv/global-max-pool classifier, behind the SAME WakeWordDetector contract
as the existing openWakeWord detector.

Torch-free at inference: the Whisper encoder runs via CTranslate2
(faster-whisper, already V.O.I.D's STT backend) and the classifier via
onnxruntime - both verified to import AND run under Windows Smart App
Control in V.O.I.D's production venv. Gen 3 uses neither torch, scipy, nor
openwakeword, so it is unaffected by the Smart App Control DLL blocks that
disable those packages here.

It is ONLY a wake detector: like every WakeWordDetector it emits the opaque
WAKE_DETECTED signal on a rising threshold crossing and nothing else -
never touching Assistant, RiskGate, KillSwitch, or Task state.

Runtime shape (single-microphone-owner preserved): the AudioCaptureBroker
remains the sole audio owner. This detector is a plain broker consumer;
``feed_audio`` only appends the broker's PCM bytes to a small rolling
buffer and returns immediately, so it NEVER blocks the broker's shared
pump thread. A background *compute* thread (not an audio/capture thread; it
opens no device and touches no broker) periodically encodes the most recent
``window`` seconds and scores it. This matters because a Whisper encode
takes ~85 ms - far longer than a 30 ms broker frame - so doing it inline in
feed_audio would stall audio fan-out to every consumer and drop frames.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from void.voice.wake import (
    WakeWordDetector, WAKE_DETECTED, WakeWordConfigError, WakeWordBackendError,
)
from void.voice.adapters import VoiceDependencyError

_SAMPLE_RATE = 16000
_DEFAULT_THRESHOLD = 0.34          # validated Gen3 Exp02 operating point (owner voice 22/23)
_DEFAULT_WINDOW_S = 2.0            # must match training/eval clip_seconds
_DEFAULT_HOP_S = 0.2              # inference cadence
_MIN_INFER_SAMPLES = int(0.6 * _SAMPLE_RATE)   # don't score until ~0.6 s has arrived


class WhisperGen3WakeDetector(WakeWordDetector):
    """Rolling-window wake detector. Buffers the broker's 16 kHz mono LE16
    PCM byte frames (fast, non-blocking); a background thread encodes the
    most recent ``window`` seconds through the frozen Whisper encoder and
    scores it with the Gen3 ONNX classifier, emitting exactly one
    WAKE_DETECTED per rising crossing of ``threshold``.

    ``_encoder_factory`` / ``_session_factory`` are test seams (inject
    fakes); production passes only real paths/threshold/on_wake.
    """

    def __init__(self, *, classifier_path: str | None,
                 threshold: float = _DEFAULT_THRESHOLD,
                 whisper_model: str = "small",
                 whisper_compute_type: str = "int8",
                 window_seconds: float = _DEFAULT_WINDOW_S,
                 hop_seconds: float = _DEFAULT_HOP_S,
                 on_wake: Callable[[str], None] | None = None,
                 _encoder_factory: Callable[[], object] | None = None,
                 _session_factory: Callable[[], object] | None = None):
        super().__init__(on_wake)
        self._classifier_path = classifier_path
        self._threshold = float(threshold)
        self._whisper_model = whisper_model
        self._whisper_compute_type = whisper_compute_type
        self._window_samples = int(window_seconds * _SAMPLE_RATE)
        self._hop_seconds = float(hop_seconds)

        self._encoder_factory = _encoder_factory
        self._session_factory = _session_factory
        self._encoder = None
        self._session = None
        self._input_name = None
        self._np = None

        self._buf_lock = threading.Lock()
        self._buf = None                 # np.int16 rolling buffer (guarded by _buf_lock)
        self._infer_thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._running = False
        self._closed = False
        self._above = False              # rising-edge latch (infer thread only)

    @property
    def threshold(self) -> float:
        return self._threshold

    # --- model loading (lazy; fail-loud) ------------------------------
    def _load(self):
        if self._encoder is not None and self._session is not None:
            return
        import numpy as np
        self._np = np

        if self._session_factory is not None:
            self._session = self._session_factory()
        else:
            if not self._classifier_path:
                raise WakeWordConfigError(
                    "no Gen3 classifier configured; set voice.wake_model_path to the "
                    "gen3 classifier .onnx (e.g. models/hey_void_gen3.onnx).")
            if not Path(self._classifier_path).is_file():
                raise WakeWordConfigError(
                    f"gen3 classifier not found: {self._classifier_path}")
            try:
                import onnxruntime as ort
            except ImportError as exc:
                raise VoiceDependencyError("gen3 wake word needs 'onnxruntime'") from exc
            try:
                self._session = ort.InferenceSession(
                    self._classifier_path, providers=["CPUExecutionProvider"])
            except Exception as exc:
                raise WakeWordBackendError(f"could not load gen3 classifier: {exc}") from exc
        self._input_name = self._session.get_inputs()[0].name

        if self._encoder_factory is not None:
            self._encoder = self._encoder_factory()
        else:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise VoiceDependencyError(
                    "gen3 wake word needs 'faster-whisper' (already V.O.I.D's STT backend)") from exc
            try:
                self._encoder = WhisperModel(
                    self._whisper_model, device="cpu",
                    compute_type=self._whisper_compute_type)
            except Exception as exc:
                raise WakeWordBackendError(f"could not load Whisper encoder: {exc}") from exc
        return self._encoder

    # --- lifecycle ----------------------------------------------------
    def start(self) -> None:
        if self._closed or self._running:
            return
        self._load()                     # surfaces config/dep/backend errors here
        self._buf = self._np.zeros(0, dtype=self._np.int16)
        self._above = False
        self._stop_evt.clear()
        self._running = True
        self._infer_thread = threading.Thread(
            target=self._infer_loop, name="void-wake-gen3", daemon=True)
        self._infer_thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_evt.set()
        t = self._infer_thread
        self._infer_thread = None
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._above = False
        if self._np is not None:
            with self._buf_lock:
                self._buf = self._np.zeros(0, dtype=self._np.int16)

    def feed_audio(self, frame) -> None:
        # Runs on the broker pump thread: MUST be cheap and non-blocking.
        if self._closed or not self._running:
            return
        np = self._np
        try:
            samples = np.frombuffer(frame, dtype="<i2")
        except Exception as exc:
            raise WakeWordBackendError(
                f"wake inference failed: could not decode audio frame: {exc}") from exc
        with self._buf_lock:
            self._buf = np.concatenate([self._buf, samples])
            if len(self._buf) > self._window_samples:
                self._buf = self._buf[-self._window_samples:]

    # --- background inference -----------------------------------------
    def _infer_loop(self) -> None:
        # Pace with the stop event so stop() is prompt; each iteration scores
        # the most recent window and edge-triggers a single wake per rising
        # crossing. An inference error disables the loop rather than spinning.
        while not self._stop_evt.wait(self._hop_seconds):
            if not self._running or self._closed:
                return
            try:
                score = self._score_current_window()
            except Exception:
                # Never let a transient inference error kill wake silently in a
                # tight spin; back off one hop and retry. Persistent failure
                # simply means no wakes (PTT still works).
                continue
            if score is None:
                continue
            if score >= self._threshold:
                if not self._above:
                    self._above = True
                    self._emit_wake()          # exactly one event per rising edge
            else:
                self._above = False

    def _score_current_window(self):
        np = self._np
        with self._buf_lock:
            if self._buf is None or len(self._buf) < _MIN_INFER_SAMPLES:
                return None
            window = self._buf.copy()
        if len(window) < self._window_samples:      # left-pad with silence
            window = np.concatenate(
                [np.zeros(self._window_samples - len(window), dtype=np.int16), window])
        waveform = (window.astype(np.float32) / 32768.0)
        import ctranslate2
        mel = self._encoder.feature_extractor(waveform)
        feats = ctranslate2.StorageView.from_array(mel[np.newaxis].astype(np.float32))
        enc = np.array(self._encoder.model.encode(feats, to_cpu=True))  # (1, T, 768)
        logit = self._session.run(None, {self._input_name: enc.astype(np.float32)})[0]
        logit = float(np.asarray(logit).reshape(-1)[0])
        return 1.0 / (1.0 + np.exp(-logit))         # sigmoid

    def close(self) -> None:
        self.stop()
        self._closed = True
        self._running = False
        self._encoder = None
        self._session = None
        self._buf = None
