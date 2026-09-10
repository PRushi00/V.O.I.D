"""Single microphone / audio-capture broker (Phase 9C-2).

The architectural invariant is: ONE physical microphone owner.

``AudioCaptureBroker`` owns exactly one capture source and fans its frames out
to any number of opaque consumers, each receiving the SAME normalized stream:
16 kHz mono 16-bit little-endian PCM, as immutable ``bytes`` frames. No wake
detector, STT adapter, VoiceSession, VoiceController, or future consumer may
open the microphone itself - they subscribe here instead.

The broker is an INFRASTRUCTURE boundary only. It does not decide when a command
starts or ends, and it has NO authority: it holds no reference to the Assistant,
RiskGate, Task state, credentials, the KillSwitch, VoiceState, the wake
detector, or STT, and it never consumes a consumer's return value (so a callback
cannot become an implicit control path). Treat its output as untrusted audio
transport.

This module mirrors the TTS / wake provider layers (:mod:`void.voice.tts`,
:mod:`void.voice.wake`): one small backend-neutral interface, a safe null
implementation, a real adapter whose heavy dependency (``sounddevice``) is
imported lazily, and a name-keyed factory. 9C-2 wires the broker into nothing -
no session, no controller, no wake detector - and nothing in production
constructs it yet; the existing PTT/STT ``MicAudioCapture`` path is unchanged
and remains the only runtime microphone owner until a later, explicitly scoped
increment migrates it here.

Backpressure: the intake queue and every per-consumer buffer are bounded and
drop the OLDEST frame when full. A slow consumer loses stale frames; it never
grows memory without bound, never blocks the capture thread, and never stalls
the other consumers. Consumer callbacks are expected to be cheap (buffer the
frame and return); heavy work belongs on the consumer's own thread in a later
phase.

Privacy: audio exists only in those bounded in-memory buffers for the duration
of active capture. Nothing is written to disk, nothing logs raw audio, and
``stop`` / ``close`` discard any buffered frames. There is no recording API.
"""
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable

from void.voice.adapters import VoiceDependencyError

# One frame is raw PCM bytes: 16-bit signed little-endian, mono, 16 kHz.
Frame = bytes
Consumer = Callable[[Frame], None]

SAMPLE_RATE = 16000          # Hz  - the only rate the broker exposes
CHANNELS = 1                 # mono
SAMPLE_WIDTH = 2             # bytes per sample (int16)

_DEFAULT_FRAME_MS = 30       # 480 samples / 960 bytes per frame at 16 kHz
_DEFAULT_INTAKE_FRAMES = 64  # bounded broker intake buffer (drop-oldest)
_DEFAULT_CONSUMER_FRAMES = 32  # bounded per-consumer buffer (drop-oldest)


class AudioBrokerError(RuntimeError):
    """The audio backend failed to open, or the broker was used after close()."""


# --- capture backend seam --------------------------------------------------
#
#   CaptureBackend
#       |-- NullCaptureBackend          (opens nothing; safe default / tests)
#       `-- SoundDeviceCaptureBackend   (real mic via sounddevice, lazy import)

class CaptureBackend:
    """Backend-neutral SINGLE audio source. The broker owns exactly one.

    ``start(on_frame)`` begins delivering normalized frames (16 kHz mono int16
    PCM ``bytes``) by calling ``on_frame`` once per frame on the backend's own
    thread. ``stop`` halts delivery; ``close`` releases the device. All three
    are idempotent. A backend never fans out, buffers unboundedly, or persists
    audio - fan-out and (bounded) buffering are the broker's job.
    """

    def start(self, on_frame: Consumer) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class NullCaptureBackend(CaptureBackend):
    """A backend that opens no device and produces no audio. The safe default
    when capture is disabled, and the deterministic stand-in for tests that do
    not inject their own fake. Records lifecycle calls for observability."""

    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self._on_frame: Consumer | None = None

    def start(self, on_frame: Consumer) -> None:
        self._on_frame = on_frame
        self.starts += 1

    def stop(self) -> None:
        self._on_frame = None
        self.stops += 1

    def close(self) -> None:
        self._on_frame = None
        self.closes += 1


class SoundDeviceCaptureBackend(CaptureBackend):
    """Real microphone via ``sounddevice`` (imported lazily inside ``start``).

    Requests a 16 kHz mono int16 input stream from the OS and forwards each
    block to ``on_frame`` as raw little-endian PCM ``bytes`` on PortAudio's
    callback thread. There is no resampling in 9C-2: a device that cannot
    provide 16 kHz mono int16 fails cleanly at ``start`` with
    :class:`AudioBrokerError`. Nothing is downloaded and no audio is written
    anywhere. The underlying stream object is private and never exposed.
    """

    def __init__(self, *, frame_samples: int):
        self._frame_samples = int(frame_samples)
        self._stream = None          # never handed outside this object

    def start(self, on_frame: Consumer) -> None:
        if self._stream is not None:
            return                   # idempotent: one stream only
        try:
            import numpy  # noqa: F401  (int16 buffer -> bytes)
            import sounddevice as sd
        except ImportError as exc:
            raise VoiceDependencyError(
                "microphone capture needs 'sounddevice' and 'numpy' "
                "(pip install -r requirements-voice.txt)") from exc

        def _cb(indata, _frames, _time, _status):
            # indata: (frames, 1) int16 ndarray -> contiguous mono LE16 bytes.
            try:
                on_frame(bytes(indata.tobytes()))
            except Exception:
                # Fan-out isolation is the broker's responsibility; never let a
                # delivery error propagate into PortAudio and kill the stream.
                pass

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                blocksize=self._frame_samples, callback=_cb)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioBrokerError(
                f"could not open a {SAMPLE_RATE} Hz mono microphone stream: "
                f"{exc}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

    def close(self) -> None:
        self.stop()


# --- per-consumer bookkeeping -------------------------------------------
class _Sub:
    """One subscriber: its callback, a bounded drop-oldest buffer, error count."""
    __slots__ = ("fn", "buffer", "errors")

    def __init__(self, fn: Consumer, depth: int):
        self.fn = fn
        self.buffer: "deque[Frame]" = deque(maxlen=depth)
        self.errors = 0


# --- the broker -------------------------------------------------------------
class AudioCaptureBroker:
    """The single owner of the physical microphone; a pure audio transport.

    One backend in, many opaque consumers out, all receiving the SAME
    normalized frame stream. The broker never inspects a frame, never decides
    utterance boundaries, and never gains execution authority.

    Threading: exactly one capture source (the backend, on its own thread) and
    exactly one broker pump thread that fans frames out to consumers. Consumers
    get neither a thread nor the device handle.

    Lifecycle (idempotent unless noted):
      * ``start()``  - build/open the backend and start the pump. Repeated call:
        no-op, and never opens a second backend. After ``close()``: raises
        :class:`AudioBrokerError`.
      * ``stop()``   - halt capture, join the pump, discard buffered audio.
        Repeated call: no-op. ``start()`` again is allowed - ``stop`` is not
        terminal.
      * ``close()``  - ``stop()`` then release the backend device; terminal. No
        further capture, delivery, or restart.
      * ``subscribe`` / ``unsubscribe`` - keyed by callable identity; safe at
        any time and from any thread; both are no-ops after ``close()``; a
        duplicate ``subscribe`` and an unknown ``unsubscribe`` are no-ops.

    A frame already being delivered when a consumer unsubscribes may still
    complete; no new frames follow.
    """

    def __init__(self, backend: CaptureBackend | None = None, *,
                 frame_ms: int = _DEFAULT_FRAME_MS,
                 intake_frames: int = _DEFAULT_INTAKE_FRAMES,
                 consumer_frames: int = _DEFAULT_CONSUMER_FRAMES,
                 on_consumer_error: Callable[[Consumer, Exception], None] | None = None):
        self._frame_samples = max(1, SAMPLE_RATE * int(frame_ms) // 1000)
        self._backend = backend           # None -> a real backend is built at start()
        self._intake_depth = max(1, int(intake_frames))
        self._consumer_depth = max(1, int(consumer_frames))
        self._on_consumer_error = on_consumer_error

        self._lock = threading.RLock()
        self._done = threading.Condition(self._lock)   # notified when intake drains
        self._intake: "queue.Queue" = queue.Queue(maxsize=self._intake_depth)
        self._inflight = 0                # frames enqueued but not yet fanned out
        self._subs: "dict[Consumer, _Sub]" = {}
        self._pump: threading.Thread | None = None
        self._running = False
        self._closed = False

    # --- introspection ----------------------------------------------------
    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def frame_bytes(self) -> int:
        """Size of one normalized frame in bytes (samples * 2, mono int16)."""
        return self._frame_samples * SAMPLE_WIDTH

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def consumer_error_count(self, consumer: Consumer) -> int:
        with self._lock:
            sub = self._subs.get(consumer)
            return sub.errors if sub is not None else 0

    # --- subscription ---------------------------------------------------
    def subscribe(self, consumer: Consumer) -> None:
        if not callable(consumer):
            raise TypeError("consumer must be callable(frame_bytes) -> None")
        with self._lock:
            if self._closed or consumer in self._subs:
                return                    # terminal / duplicate -> no-op
            self._subs[consumer] = _Sub(consumer, self._consumer_depth)

    def unsubscribe(self, consumer: Consumer) -> None:
        with self._lock:
            self._subs.pop(consumer, None)   # unknown / after close -> no-op

    # --- lifecycle ----------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise AudioBrokerError("broker is closed; construct a new one")
            if self._running:
                return                    # idempotent: no second backend / pump
            if self._backend is None:
                self._backend = SoundDeviceCaptureBackend(
                    frame_samples=self._frame_samples)
            self._drain_intake_locked()
            self._pump = threading.Thread(
                target=self._run_pump, name="void-audio-broker", daemon=True)
            self._running = True
            self._pump.start()
            backend = self._backend
        # backend.start() may briefly block opening PortAudio - do it unlocked.
        try:
            backend.start(self._ingest)
        except Exception:
            self._teardown_pump()
            with self._lock:
                self._running = False
            raise

    def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False         # gate _ingest before the backend halts
            backend = self._backend
        try:
            if backend is not None:
                backend.stop()
        except Exception:
            pass
        self._teardown_pump()
        with self._lock:
            self._drain_intake_locked()
            for sub in self._subs.values():
                sub.buffer.clear()        # privacy: retain no audio past capture

    def close(self) -> None:
        self.stop()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            backend = self._backend
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

    def drain(self, timeout: float | None = 1.0) -> bool:
        """Block until every frame handed to the pump has been fanned out (or
        ``timeout`` seconds elapse). A shutdown / test aid - never needed on the
        hot path. Returns True if the intake fully drained."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._done:
            while self._inflight > 0:
                if deadline is None:
                    self._done.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._done.wait(remaining)
            return True

    # --- internals ----------------------------------------------------
    def _ingest(self, frame: Frame) -> None:
        """Backend-thread entry point. Bounded, drop-oldest, never blocks."""
        with self._lock:
            if not self._running:
                return                    # stopped / closed -> no late frames
            if self._intake.full():
                try:
                    self._intake.get_nowait()
                    self._intake.task_done()
                    self._inflight -= 1
                except queue.Empty:
                    pass
            self._intake.put_nowait(frame)
            self._inflight += 1

    def _run_pump(self) -> None:
        while True:
            frame = self._intake.get()
            if frame is None:             # sentinel: shut the pump down
                self._intake.task_done()
                return
            try:
                self._fanout(frame)
            finally:
                self._intake.task_done()
                with self._done:
                    self._inflight -= 1
                    if self._inflight <= 0:
                        self._inflight = 0
                        self._done.notify_all()

    def _fanout(self, frame: Frame) -> None:
        with self._lock:
            subs = list(self._subs.values())   # snapshot: sub/unsub-safe
        for sub in subs:
            sub.buffer.append(frame)      # bounded deque -> oldest dropped if full
            while sub.buffer:
                f = sub.buffer.popleft()
                try:
                    sub.fn(f)
                except Exception as exc:  # one bad consumer never affects others
                    sub.errors += 1
                    cb = self._on_consumer_error
                    if cb is not None:
                        try:
                            cb(sub.fn, exc)
                        except Exception:
                            pass

    def _teardown_pump(self) -> None:
        with self._lock:
            pump, self._pump = self._pump, None
            if pump is not None:
                self._enqueue_sentinel_locked()
        if pump is not None:
            pump.join(timeout=2.0)

    def _enqueue_sentinel_locked(self) -> None:
        try:
            self._intake.put_nowait(None)
        except queue.Full:
            try:
                self._intake.get_nowait()
                self._intake.task_done()
                self._inflight -= 1
            except queue.Empty:
                pass
            try:
                self._intake.put_nowait(None)
            except queue.Full:            # pragma: no cover - room was just made
                pass

    def _drain_intake_locked(self) -> None:
        while True:
            try:
                item = self._intake.get_nowait()
            except queue.Empty:
                break
            self._intake.task_done()
            if item is not None:
                self._inflight -= 1
        self._inflight = 0
        self._done.notify_all()


# --- backend registry + factory (mirrors create_tts_provider / create_wake_detector)
_BACKENDS: "dict[str, Callable[..., CaptureBackend]]" = {
    "sounddevice": SoundDeviceCaptureBackend,
    "null": NullCaptureBackend,
}


def register_capture_backend(name: str,
                             factory: Callable[..., CaptureBackend]) -> None:
    _BACKENDS[str(name).strip().lower()] = factory


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


def create_audio_broker(config=None, *, backend: CaptureBackend | None = None,
                        on_consumer_error: Callable[[Consumer, Exception], None] | None = None
                        ) -> AudioCaptureBroker:
    """Build an :class:`AudioCaptureBroker` from config.

    Reads ``voice.mic_frame_ms`` / ``voice.mic_intake_frames`` /
    ``voice.mic_consumer_frames`` when a Config-like object is given, else uses
    conservative defaults. Construction is cheap and imports nothing: the real
    ``sounddevice`` backend is created and opened only at ``start()``. Pass an
    explicit ``backend`` (e.g. a fake) to bypass the real device entirely.
    """
    def _cfg(key, default):
        if config is None:
            return default
        try:
            return config.get(key, default)
        except Exception:
            return default

    return AudioCaptureBroker(
        backend=backend,
        frame_ms=_cfg("voice.mic_frame_ms", _DEFAULT_FRAME_MS),
        intake_frames=_cfg("voice.mic_intake_frames", _DEFAULT_INTAKE_FRAMES),
        consumer_frames=_cfg("voice.mic_consumer_frames", _DEFAULT_CONSUMER_FRAMES),
        on_consumer_error=on_consumer_error,
    )
