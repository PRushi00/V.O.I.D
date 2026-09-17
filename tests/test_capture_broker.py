"""Phase 9C-2 audio-capture broker tests - deterministic, fakes only.

No microphone, no sounddevice, no PortAudio, no real device. A FakeCaptureBackend
(injected in place of the real sounddevice backend) drives frame delivery
deterministically; broker.drain() joins the pump so assertions never race a
timer. These tests cover the single-owner invariant, the normalized frame
contract, lifecycle determinism/idempotency, consumer fan-out, bounded
backpressure, consumer-failure isolation, and the authority boundary.
"""
import inspect
import sys

import pytest

from void.voice.adapters import VoiceDependencyError
from void.voice.capture_broker import (
    CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, AudioBrokerError, AudioCaptureBroker,
    CaptureBackend, NullCaptureBackend, SoundDeviceCaptureBackend,
    available_backends, create_audio_broker, register_capture_backend,
)
import void.voice.capture_broker as brokermod


class FakeCaptureBackend(CaptureBackend):
    """Stands in for the real sounddevice backend: emits frames on demand."""

    def __init__(self):
        self.starts = 0
        self.stops = 0
        self.closes = 0
        self.running = False
        self._on_frame = None

    def start(self, on_frame):
        self.starts += 1
        self._on_frame = on_frame
        self.running = True

    def stop(self):
        self.stops += 1
        self.running = False

    def close(self):
        self.closes += 1
        self.running = False

    # test driver -----------------------------------------------------
    def emit(self, frame):
        if self.running and self._on_frame is not None:
            self._on_frame(frame)


class Recorder:
    """A minimal consumer: records every frame it is handed."""

    def __init__(self):
        self.frames = []

    def __call__(self, frame):
        self.frames.append(frame)


def _broker(**kw):
    backend = kw.pop("backend", None) or FakeCaptureBackend()
    return AudioCaptureBroker(backend=backend, **kw), backend


# --- construction / contract (1, 15) ---------------------------------

def test_broker_constructs():
    b, _ = _broker()
    assert isinstance(b, AudioCaptureBroker)
    assert not b.running and not b.closed and b.subscriber_count == 0


def test_broker_declares_16k_mono_int16_contract():
    assert SAMPLE_RATE == 16000 and CHANNELS == 1 and SAMPLE_WIDTH == 2


def test_frame_bytes_tracks_frame_ms():
    b, _ = _broker(frame_ms=30)
    assert b.frame_bytes == (16000 * 30 // 1000) * 2 == 960
    b2, _ = _broker(frame_ms=10)
    assert b2.frame_bytes == 320


def test_frames_pass_through_unmodified_as_bytes():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.start()
    payload = b"\x01\x02\x03\x04" * 240      # 960 bytes, arbitrary PCM
    backend.emit(payload)
    b.drain()
    assert rec.frames == [payload]
    assert all(isinstance(f, (bytes, bytearray)) for f in rec.frames)
    b.close()


# --- start / single owner (2, 3, 17) -------------------------------

def test_start_opens_exactly_one_backend():
    b, backend = _broker()
    b.start()
    assert backend.starts == 1 and backend.running
    b.close()


def test_repeated_start_does_not_open_a_second_backend():
    b, backend = _broker()
    b.start(); b.start(); b.start()
    assert backend.starts == 1
    b.close()


def test_many_subscribers_still_one_backend():
    b, backend = _broker()
    for _ in range(5):
        b.subscribe(Recorder())
    b.start()
    assert backend.starts == 1 and b.subscriber_count == 5
    b.close()
    assert backend.starts == 1


# --- stop / close (4, 5, 6, 7) -----------------------------------

def test_stop_stops_capture():
    b, backend = _broker()
    b.start()
    b.stop()
    assert backend.stops == 1 and not backend.running and not b.running


def test_repeated_stop_is_safe():
    b, backend = _broker()
    b.start()
    b.stop(); b.stop(); b.stop()
    assert backend.stops == 1        # only the first stop reached the backend


def test_stop_before_start_is_safe():
    b, backend = _broker()
    b.stop()
    assert backend.stops == 0 and not b.running


def test_close_releases_capture():
    b, backend = _broker()
    b.start()
    b.close()
    assert backend.closes == 1 and b.closed and not b.running


def test_close_is_idempotent():
    b, backend = _broker()
    b.start()
    b.close(); b.close()
    assert backend.closes == 1


def test_start_after_close_is_rejected():
    b, _ = _broker()
    b.start()
    b.close()
    with pytest.raises(AudioBrokerError):
        b.start()


def test_no_frames_delivered_after_close():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.start()
    b.close()
    backend.running = True                 # even if the backend misbehaves
    backend._on_frame = b._ingest
    backend.emit(b"late")
    b.drain()
    assert rec.frames == []


def test_restart_after_stop_is_allowed():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.start()
    b.stop()
    b.start()                              # stop is not terminal
    backend.emit(b"frame-2")
    b.drain()
    assert rec.frames == [b"frame-2"] and backend.starts == 2
    b.close()


# --- subscribe / fan-out (8, 9, 10) -----------------------------

def test_subscriber_receives_frames():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.start()
    backend.emit(b"a"); backend.emit(b"b")
    b.drain()
    assert rec.frames == [b"a", b"b"]
    b.close()


def test_multiple_subscribers_receive_the_same_frames():
    b, backend = _broker()
    a, c = Recorder(), Recorder()
    b.subscribe(a); b.subscribe(c)
    b.start()
    for f in (b"1", b"2", b"3"):
        backend.emit(f)
    b.drain()
    assert a.frames == [b"1", b"2", b"3"]
    assert c.frames == [b"1", b"2", b"3"]
    # the SAME object is fanned out, not a per-consumer copy
    assert a.frames[0] is c.frames[0]
    b.close()


def test_unsubscribe_stops_delivery():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.start()
    backend.emit(b"before")
    b.drain()
    b.unsubscribe(rec)
    backend.emit(b"after")
    b.drain()
    assert rec.frames == [b"before"]
    b.close()


def test_duplicate_subscribe_is_a_noop():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec); b.subscribe(rec)
    assert b.subscriber_count == 1
    b.start()
    backend.emit(b"x")
    b.drain()
    assert rec.frames == [b"x"]            # delivered once, not twice
    b.close()


def test_unsubscribe_unknown_is_safe():
    b, _ = _broker()
    b.unsubscribe(lambda _f: None)         # never subscribed -> no error


def test_subscribe_after_close_is_a_noop():
    b, _ = _broker()
    b.close()
    b.subscribe(Recorder())
    assert b.subscriber_count == 0


def test_unsubscribe_after_close_is_a_noop():
    b, _ = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.close()
    b.unsubscribe(rec)                     # no error


def test_non_callable_consumer_is_rejected():
    b, _ = _broker()
    with pytest.raises(TypeError):
        b.subscribe(object())


# --- backpressure (11) -----------------------------------------------

def test_slow_consumer_cannot_grow_buffers_without_bound():
    import threading
    gate = threading.Event()

    def slow(_frame):
        gate.wait(timeout=2.0)            # block the pump until released

    b, backend = _broker(intake_frames=8, consumer_frames=4)
    fast = Recorder()
    b.subscribe(slow)
    b.subscribe(fast)
    b.start()
    # Flood far past every bound while the pump is wedged on `slow`.
    for i in range(500):
        backend.emit(bytes([i % 256]))
    # Intake is bounded: it never exceeds its configured maxsize, and the
    # in-flight count is bounded by that depth plus the one frame in the pump.
    assert b._intake.qsize() <= 8
    assert b._inflight <= b._intake_depth + 1
    gate.set()
    b.drain(timeout=2.0)
    # The fast consumer only ever saw the bounded set of frames that survived
    # (frame 0 already in the pump + whatever remained within the intake bound);
    # the ~490 dropped frames never accumulated anywhere.
    assert len(fast.frames) <= b._intake_depth + 1
    b.close()


def test_intake_drops_oldest_when_full():
    import threading
    gate = threading.Event()

    def slow(_frame):
        gate.wait(timeout=2.0)

    b, backend = _broker(intake_frames=4, consumer_frames=64)
    seen = Recorder()
    b.subscribe(slow)                     # wedges the pump on the first frame
    b.subscribe(seen)
    b.start()
    for i in range(20):
        backend.emit(bytes([i]))
    gate.set()
    b.drain(timeout=2.0)
    # The pump was wedged while the flood arrived, so the middle frames were
    # dropped-oldest down to the intake bound. Deterministic properties: the
    # newest frame is never dropped, and far fewer than 20 survived.
    assert seen.frames[-1] == bytes([19])
    assert 0 < len(seen.frames) <= b._intake_depth + 1 < 20
    b.close()


# --- consumer failure isolation (12, 13) --------------------------

def test_consumer_exception_does_not_kill_the_broker():
    b, backend = _broker()

    def boom(_frame):
        raise RuntimeError("consumer blew up")

    b.subscribe(boom)
    b.start()
    for _ in range(3):
        backend.emit(b"f")
    b.drain()
    assert b.running                      # pump + capture still alive
    assert b.consumer_error_count(boom) == 3
    b.close()


def test_other_consumers_keep_receiving_after_one_fails():
    b, backend = _broker()
    good = Recorder()

    def boom(_frame):
        raise RuntimeError("nope")

    b.subscribe(boom)
    b.subscribe(good)
    b.start()
    for f in (b"1", b"2", b"3"):
        backend.emit(f)
    b.drain()
    assert good.frames == [b"1", b"2", b"3"]
    b.close()


def test_on_consumer_error_callback_is_invoked():
    seen = []
    backend = FakeCaptureBackend()
    b = AudioCaptureBroker(
        backend=backend,
        on_consumer_error=lambda fn, exc: seen.append((fn, type(exc))))

    def boom(_frame):
        raise ValueError("x")

    b.subscribe(boom)
    b.start()
    backend.emit(b"f")
    b.drain()
    assert seen and seen[0][0] is boom and seen[0][1] is ValueError
    b.close()


def test_faulty_error_callback_is_swallowed():
    backend = FakeCaptureBackend()
    b = AudioCaptureBroker(
        backend=backend,
        on_consumer_error=lambda *_: (_ for _ in ()).throw(RuntimeError("bad")))
    b.subscribe(lambda _f: (_ for _ in ()).throw(RuntimeError("boom")))
    b.start()
    backend.emit(b"f")
    b.drain()
    assert b.running                      # neither exception escaped
    b.close()


# --- pump-thread resilience (the tray-alive-mic-dead bug) -----------------
#
# Reproduces, deterministically and without any real audio hardware, the
# structural gap found while investigating "tray icon visible, microphone no
# longer active": _fanout's per-consumer isolation already existed, but
# nothing guarded the PUMP LOOP ITSELF - an exception escaping from anywhere
# else in _fanout (its own bookkeeping, not a consumer callback) used to kill
# the pump thread permanently and silently: the backend kept delivering
# frames (and logging its own health marker) forever after, but no consumer
# - wake detector, PTT capture - would ever receive another one.

def test_pump_survives_an_internal_fanout_error(monkeypatch):
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    b.start()

    real_fanout = b._fanout
    calls = {"n": 0}

    def _flaky_fanout(frame):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated broker-internal fault")
        return real_fanout(frame)

    monkeypatch.setattr(b, "_fanout", _flaky_fanout)
    backend.emit(b"first-poisoned")   # would have killed the old pump forever
    b.drain()
    backend.emit(b"second")           # proves the pump is still alive afterward
    b.drain()
    assert rec.frames == [b"second"]
    assert b.running                  # the pump thread itself survived
    b.close()


def test_pump_error_is_logged_but_never_raised(monkeypatch, caplog):
    import logging

    b, backend = _broker()
    b.start()

    def _boom(_frame):
        raise RuntimeError("simulated broker-internal fault")

    monkeypatch.setattr(b, "_fanout", _boom)
    with caplog.at_level(logging.ERROR, logger="void.voice.capture_broker"):
        backend.emit(b"x")
        b.drain()
    assert any("AUDIO_PUMP_ERROR" in r.message for r in caplog.records)
    b.close()


# --- mic-health timestamp (feeds void.voice.runtime's supervisor) ---------

def test_seconds_since_last_frame_is_none_before_any_frame():
    b, _ = _broker()
    assert b.seconds_since_last_frame() is None
    b.close()


def test_seconds_since_last_frame_resets_on_each_frame(monkeypatch):
    import void.voice.capture_broker as mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    b, backend = _broker()
    b.start()
    backend.emit(b"f")
    b.drain()
    assert b.seconds_since_last_frame() == 0.0
    clock["t"] += 5.0
    assert b.seconds_since_last_frame() == 5.0
    clock["t"] += 1.0
    backend.emit(b"f2")
    b.drain()
    assert b.seconds_since_last_frame() == 0.0   # a fresh frame resets it
    b.close()


# --- lifecycle determinism (18) ------------------------------------

def test_repeated_lifecycle_is_deterministic():
    b, backend = _broker()
    rec = Recorder()
    b.subscribe(rec)
    for round_no in range(3):
        b.start()
        backend.emit(f"r{round_no}".encode())
        b.drain()
        b.stop()
    assert rec.frames == [b"r0", b"r1", b"r2"]
    assert backend.starts == 3 and backend.stops == 3
    b.close()
    assert backend.closes == 1


def test_drain_returns_true_when_idle_and_false_on_timeout():
    import threading
    gate = threading.Event()
    b, backend = _broker()
    b.subscribe(lambda _f: gate.wait(timeout=2.0))
    b.start()
    assert b.drain(timeout=0.5) is True   # nothing in flight yet
    backend.emit(b"f")
    assert b.drain(timeout=0.1) is False  # pump is blocked -> times out
    gate.set()
    assert b.drain(timeout=2.0) is True
    b.close()


# --- no real microphone / device handle (14, 16) -----------------

def test_constructing_broker_imports_no_audio_stack():
    for mod in ("void.app", "void.voice", "void.voice.capture_broker"):
        __import__(mod)
    AudioCaptureBroker(backend=None)       # real backend NOT built until start()
    # nothing here should have pulled sounddevice into the process
    assert "sounddevice" not in sys.modules


def test_real_backend_defers_sounddevice_import_to_start(monkeypatch):
    b = AudioCaptureBroker(backend=None)   # will lazily build the real backend

    real_import = __import__

    def _blocked(name, *a, **k):
        if name == "sounddevice":
            raise ImportError("sounddevice not installed")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _blocked)
    with pytest.raises(VoiceDependencyError):
        b.start()                         # only now is the import attempted


def test_device_handle_is_never_exposed():
    b, backend = _broker()
    captured = []
    b.subscribe(captured.append)
    b.start()
    backend.emit(b"pcm")
    b.drain()
    # consumers only ever see bytes frames, never a stream/device object
    assert captured == [b"pcm"]
    assert not hasattr(b, "stream") and not hasattr(b, "_stream")
    assert not hasattr(b, "device")
    b.close()


def test_sounddevice_backend_normalizes_blocks_to_pcm_bytes(monkeypatch):
    np = pytest.importorskip("numpy")

    class _FakeStream:
        def __init__(self, **kw):
            self.kw = kw
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            pass

    made = {}

    class _FakeSD:
        def InputStream(self, **kw):       # noqa: N802 (match sounddevice API)
            made["kw"] = kw
            made["stream"] = _FakeStream(**kw)
            return made["stream"]

    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSD())
    out = []
    be = SoundDeviceCaptureBackend(frame_samples=480)
    be.start(out.append)
    # requested exactly a 16 kHz mono int16 stream
    assert made["kw"]["samplerate"] == 16000
    assert made["kw"]["channels"] == 1
    assert made["kw"]["dtype"] == "int16"
    assert made["kw"]["blocksize"] == 480
    # feed a realistic (480, 1) int16 block through the PortAudio-style callback
    block = np.arange(480, dtype="int16").reshape(-1, 1)
    made["kw"]["callback"](block, 480, None, None)
    assert len(out) == 1 and isinstance(out[0], bytes)
    assert len(out[0]) == 480 * 2         # int16 mono
    assert np.frombuffer(out[0], dtype="int16").tolist() == list(range(480))
    be.close()


def test_sounddevice_backend_wraps_open_failure(monkeypatch):
    pytest.importorskip("numpy")

    class _FakeSD:
        def InputStream(self, **kw):       # noqa: N802
            raise RuntimeError("no input device")

    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSD())
    be = SoundDeviceCaptureBackend(frame_samples=480)
    with pytest.raises(AudioBrokerError):
        be.start(lambda _f: None)


def test_sounddevice_backend_logs_which_device_it_opened(monkeypatch, caplog):
    # Post-reboot diagnosability: "the stream opened" is not the same as
    # "the right physical microphone opened" - a wrong/virtual/silent default
    # input device can still open successfully. Record device identity so a
    # stale/incorrect default device is distinguishable after the fact.
    import logging

    pytest.importorskip("numpy")

    class _FakeStream:
        def start(self): pass
        def stop(self): pass
        def close(self): pass

    class _FakeSD:
        class default:
            device = (3, 7)

        def InputStream(self, **kw):       # noqa: N802
            return _FakeStream()

        def query_devices(self, idx):
            assert idx == 3
            return {"name": "Fake USB Microphone"}

    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSD())
    be = SoundDeviceCaptureBackend(frame_samples=480)
    with caplog.at_level(logging.INFO, logger="void.voice.capture_broker"):
        be.start(lambda _f: None)
    assert any("MICROPHONE_OPENED" in r.message and "Fake USB Microphone" in r.message
              for r in caplog.records)
    be.close()


def test_sounddevice_backend_device_query_failure_does_not_break_start(monkeypatch, caplog):
    # A device-identity query is diagnostics only - if it fails, capture must
    # still proceed; the health marker just falls back to "unknown".
    import logging

    pytest.importorskip("numpy")

    class _FakeStream:
        def start(self): pass
        def stop(self): pass
        def close(self): pass

    class _FakeSD:
        def InputStream(self, **kw):       # noqa: N802
            return _FakeStream()
        # no .default / .query_devices at all

    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSD())
    be = SoundDeviceCaptureBackend(frame_samples=480)
    with caplog.at_level(logging.INFO, logger="void.voice.capture_broker"):
        be.start(lambda _f: None)          # must not raise
    assert any("MICROPHONE_OPENED" in r.message for r in caplog.records)
    be.close()


def test_broker_emits_periodic_audio_frames_received_health_marker(monkeypatch, caplog):
    # Post-reboot diagnosability: prove the broker is actually delivering
    # frames from the backend, without inspecting or logging frame content.
    import logging

    import void.voice.capture_broker as mod
    monkeypatch.setattr(mod, "_FRAME_LOG_PERIOD_S", 0.0)   # log on first frame
    b, backend = _broker()
    with caplog.at_level(logging.INFO, logger="void.voice.capture_broker"):
        b.start()
        backend.emit(b"\x00\x00" * 480)
        b.drain()
    b.close()
    markers = [r for r in caplog.records if "AUDIO_FRAMES_RECEIVED" in r.message]
    assert markers
    assert "count=" in markers[0].message


# --- authority boundary (12 of the spec) --------------------------

def test_broker_holds_no_authority_references():
    b, _ = _broker()
    for attr in ("assistant", "_assistant", "kill_switch", "_kill_switch",
                 "ks", "_ks", "risk_gate", "_risk_gate", "riskgate",
                 "session", "_session", "state", "_state", "voicestate",
                 "tools", "_tools", "credentials", "_credentials",
                 "wake", "_wake", "detector", "_detector", "stt", "_stt",
                 "assistant_run", "run"):
        assert not hasattr(b, attr), f"broker unexpectedly exposes {attr!r}"


def test_capture_broker_module_imports_stay_infra_only():
    import ast
    tree = ast.parse(inspect.getsource(brokermod))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    # stdlib + the shared voice-IO error module + the two lazy audio deps only
    assert imported <= {
        "__future__", "queue", "threading", "time", "collections", "typing",
        "logging", "void.voice.adapters", "numpy", "sounddevice",
    }, f"unexpected imports: {imported}"
    for banned in ("void.voice.wake", "void.voice.session", "void.voice.state",
                   "void.voice.runtime", "void.core.agent",
                   "void.core.kill_switch", "faster_whisper", "openwakeword"):
        assert banned not in imported


def test_no_wake_detector_or_stt_is_instantiated(monkeypatch):
    import void.voice.wake as wake
    from void.voice import adapters
    tripped = []
    monkeypatch.setattr(wake, "OpenWakeWordDetector",
                        lambda *a, **k: tripped.append("wake"))
    monkeypatch.setattr(wake, "NullWakeDetector",
                        lambda *a, **k: tripped.append("wake"))
    monkeypatch.setattr(adapters, "FasterWhisperSTT",
                        lambda *a, **k: tripped.append("stt"))
    monkeypatch.setattr(adapters, "MicAudioCapture",
                        lambda *a, **k: tripped.append("mic"))
    b, backend = _broker()
    b.subscribe(Recorder())
    b.start()
    backend.emit(b"f")
    b.drain()
    b.close()
    create_audio_broker()
    assert tripped == []


def test_consumer_return_value_is_ignored():
    # A callback that "returns a command" cannot influence the broker: its
    # return value is discarded (no implicit authority path through fan-out).
    b, backend = _broker()

    def returns_stuff(_frame):
        return {"action": "delete_everything"}

    b.subscribe(returns_stuff)
    b.start()
    backend.emit(b"f")
    b.drain()
    assert b.running                      # nothing acted on the return value
    b.close()


# --- null backend + factory + registry ---------------------------

def test_null_backend_produces_no_frames():
    be = NullCaptureBackend()
    b = AudioCaptureBroker(backend=be)
    rec = Recorder()
    b.subscribe(rec)
    b.start()
    b.drain()
    assert rec.frames == [] and be.starts == 1
    b.stop(); b.close()
    assert be.stops == 1 and be.closes == 1


def test_factory_builds_broker_with_defaults():
    b = create_audio_broker()
    assert isinstance(b, AudioCaptureBroker)
    assert b.frame_bytes == 960           # 30 ms default


def test_factory_reads_frame_and_buffer_sizes_from_config():
    class _Cfg:
        def __init__(self, v):
            self._v = v

        def get(self, key, default=None):
            return self._v.get(key, default)

    b = create_audio_broker(_Cfg({
        "voice.mic_frame_ms": 20,
        "voice.mic_intake_frames": 10,
        "voice.mic_consumer_frames": 5,
    }), backend=FakeCaptureBackend())
    assert b.frame_bytes == (16000 * 20 // 1000) * 2 == 640
    assert b._intake_depth == 10 and b._consumer_depth == 5


def test_registry_lists_builtin_backends():
    assert "sounddevice" in available_backends()
    assert "null" in available_backends()


def test_register_capture_backend_is_replaceable():
    register_capture_backend("pretend", NullCaptureBackend)
    try:
        assert "pretend" in available_backends()
    finally:
        brokermod._BACKENDS.pop("pretend", None)
