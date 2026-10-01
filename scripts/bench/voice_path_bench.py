"""The whole spoken-command path, stage by stage, in one process.

Endpointing is only one term of what the owner waits for, and the point of separating them is to stop any one stage
being quoted as the total. Measured here, in real time, through the REAL broker, session, endpointer, speech-to-text
and assistant:

    A  speech end      -> endpoint decision     (the endpointer's own latency)
    B  endpoint        -> speech-to-text starts (worker hand-off, frame join, the pre-STT guard)
    C  speech-to-text                           (CUDA decode)
    D  dispatch                                 (resolution + RiskGate + launch)
    E  speech end      -> the action happens     = A + B + C + D

Stage A is judged against faster-whisper's OFFLINE Silero VAD, exactly as scripts/bench/endpoint_adaptive_bench.py
does, so the two agree on what "end of speech" means. Frames are fed at real time (30 ms apart), because B exists
only because of a thread hand-off and feeding faster than real time would not measure it.

Launches are recorded, never performed: starting programs measures Windows, not V.O.I.D.

usage:
    python scripts/bench/voice_path_bench.py
    python scripts/bench/voice_path_bench.py --fixed     # the pre-milestone fixed budget, for the before column
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from faster_whisper.vad import VadOptions, get_speech_timestamps        # noqa: E402

FRAME = 480
FRAME_S = FRAME / 16000


def frames_of(path):
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
            return None
        raw = w.readframes(w.getnframes())
    return [raw[i * FRAME * 2:(i + 1) * FRAME * 2] for i in range(len(raw) // (FRAME * 2))]


def reference_end_frame(frames):
    pcm = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    segs = get_speech_timestamps(pcm, VadOptions(min_silence_duration_ms=100, speech_pad_ms=0))
    return int(segs[-1]["end"] / FRAME) if segs else None


def q(v, f):
    v = sorted(v)
    return v[min(len(v) - 1, int(f * len(v)))] if v else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixed", action="store_true",
                    help="disable the adaptive budget (fast = safe), i.e. the behaviour before this milestone")
    ap.add_argument("--clips-dir", default=str(Path(os.environ.get("TEMP", "."))
                                               / "void_endpoint_bench" / "synth"))
    ap.add_argument("--repeats", type=int, default=2)
    args = ap.parse_args()

    from void.config import Config
    from void.voice import cuda as _cuda
    _cuda.register()
    from void.core.kill_switch import KillSwitch
    from void.voice.adapters import BrokerCapture, FasterWhisperSTT, TTS
    from void.voice.capture_broker import AudioCaptureBroker, CaptureBackend
    from void.voice.session import VoiceSession
    from void.voice.state import VoiceState
    from void.voice.runtime import VoiceController, _SerialVoiceWorker, _WakePolicy
    from void.voice.wake import WAKE_DETECTED

    cfg = Config.load()
    pol = _WakePolicy.from_config(cfg)
    if args.fixed:
        pol = _WakePolicy(**{**pol.__dict__, "fast_silence_s": pol.silence_s})
    print("policy: safe=%.2fs fast=%.2fs window=[%.2f, %.2f)s pause_evidence=%.2fs"
          % (pol.silence_s, pol.fast_silence_s, pol.fast_after_speech_s,
             pol.fast_until_speech_s, pol.pause_evidence_s))

    class Backend(CaptureBackend):
        def __init__(self):
            self.running = False
            self._on_frame = None

        def start(self, on_frame):
            self._on_frame, self.running = on_frame, True

        def stop(self):
            self.running = False

        def close(self):
            self.running = False

        def emit(self, frame):
            if self.running and self._on_frame is not None:
                self._on_frame(frame)

    class Wake:
        def __init__(self):
            self.on_wake = None

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

        def feed_audio(self, frame):
            pass

        def fire(self):
            if self.on_wake:
                self.on_wake(WAKE_DETECTED)

    class SilentTTS(TTS):
        def speak(self, text):
            pass

        def stop(self):
            pass

    # launches recorded, not performed
    import void.actions.apps as apps_mod
    launched = []
    apps_mod.subprocess.Popen = lambda argv, *a, **k: launched.append(tuple(argv))
    apps_mod.os.startfile = lambda t: launched.append(("startfile", t))

    from void.app import Assistant
    from void.providers.base import LLMProvider, LLMResponse
    from void.providers.registry import ProviderRegistry

    class Counting(LLMProvider):
        def __init__(self, name):
            self.name, self.calls = name, 0

        def available(self):
            return True

        def generate(self, messages, tools=None):
            self.calls += 1
            return LLMResponse(text="(the model answered)")

    asst = Assistant(config=cfg)
    gemini, local = Counting("gemini"), Counting("local")
    asst.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])
    asst._fast.catalog.entries()
    if asst._fast.folders is not None:
        asst._fast.folders.entries()

    stt = FasterWhisperSTT(model_name=cfg.get("voice.stt_model", "small"),
                           device=cfg.get("voice.stt_device", "cpu"),
                           compute_type=cfg.get("voice.stt_compute_type", "int8"),
                           beam_size=int(cfg.get("voice.stt_beam_size", 1)),
                           vad_filter=bool(cfg.get("voice.stt_vad_filter", True)),
                           temperature=cfg.get("voice.stt_temperature", 0.0))
    stt.warmup()

    marks: dict = {}

    class TimedSTT(FasterWhisperSTT):
        pass

    def timed_transcribe(audio, _inner=stt.transcribe):
        marks["stt_start"] = time.perf_counter()
        out = _inner(audio)
        marks["stt_end"] = time.perf_counter()
        return out
    stt.transcribe = timed_transcribe

    clips = sorted(Path(args.clips_dir).glob("*.wav"))
    if not clips:
        raise SystemExit("no clips in %s - run scripts/bench/endpoint_adaptive_bench.py first" % args.clips_dir)

    rows = []
    for rep in range(args.repeats):
        for path in clips:
            frames = frames_of(path)
            ref = reference_end_frame(frames) if frames else None
            if ref is None:
                continue
            backend = Backend()
            broker = AudioCaptureBroker(backend=backend)
            session = VoiceSession(asst, KillSwitch(), capture=BrokerCapture(broker), stt=stt,
                                   tts=SilentTTS(), speak_response=False,
                                   min_speech_ms=float(cfg.get("voice.min_speech_ms", 250)),
                                   speak_successful_actions=bool(
                                       cfg.get("voice.speak_successful_actions", False)))
            worker = _SerialVoiceWorker()
            wake = Wake()
            ctrl = VoiceController(session, None, poll_interval=0.01, worker=worker,
                                   broker=broker, wake=wake, wake_policy=pol)
            marks.clear()
            inner_final = ctrl._wake_finalize

            def finalize(reason, _inner=inner_final):
                marks["endpoint"] = time.perf_counter()
                marks["endpoint_frame"] = emitted[0]
                _inner(reason)
            ctrl._wake_finalize = finalize

            class _NoActivation:
                """No push-to-talk here: every capture in this bench is wake-initiated, like production."""

                def start(self):
                    pass

                def stop(self):
                    pass
            ctrl._activation = _NoActivation()
            ctrl.start(monitor=False)
            for _ in range(ctrl._rearm_ticks + 1):
                ctrl.poll_once()
            wake.fire()
            # Wait WITHOUT polling. The reconciler cancels a capture whose session has not reached LISTENING yet,
            # and the worker needs a moment to get there - polling in a tight loop here cancels the wake before the
            # endpointer is ever attached. (In production the monitor ticks every 100 ms and the worker runs in
            # microseconds, so the same window exists but is vanishingly narrow; noted in the milestone document.)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and ctrl._endpointer is None:
                time.sleep(0.005)
            emitted = [0]
            t_speech_end = None
            for i, f in enumerate(frames):
                backend.emit(f)
                emitted[0] = i
                if i == ref:
                    t_speech_end = time.perf_counter()
                time.sleep(FRAME_S)
                if "endpoint" in marks:
                    break
            silence = b"\x00\x00" * FRAME
            for _ in range(300):
                if "endpoint" in marks:
                    break
                backend.emit(silence)
                emitted[0] += 1
                time.sleep(FRAME_S)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and session.state not in (VoiceState.IDLE, VoiceState.ERROR):
                ctrl.poll_once()
                time.sleep(0.005)
            t_done = time.perf_counter()
            ctrl.shutdown("bench")
            broker.close()
            if t_speech_end is None and "endpoint" in marks:
                # The capture closed BEFORE the reference end of speech: the command was cut short. Reported, not
                # silently skipped - a truncation is the one outcome that must never be averaged away.
                print("  %-18s TRUNCATED (endpoint fired %d frames before speech ended)"
                      % (path.stem, ref - marks["endpoint_frame"]))
                continue
            if "endpoint" not in marks or "stt_end" not in marks:
                print("  %-18s skipped (endpoint=%s stt=%s)"
                      % (path.stem, "endpoint" in marks, "stt_end" in marks))
                continue
            a = (marks["endpoint_frame"] - ref) * FRAME_S
            b = marks["stt_start"] - marks["endpoint"]
            c = marks["stt_end"] - marks["stt_start"]
            d = t_done - marks["stt_end"]
            rows.append((path.stem, a, b, c, d, a + b + c + d))
            if rep == 0:
                print("  %-18s A=%6.0fms B=%5.1fms C=%6.0fms D=%6.0fms  E=%6.0fms"
                      % (path.stem, a * 1000, b * 1000, c * 1000, d * 1000, (a + b + c + d) * 1000))

    print("\n%-28s %9s %9s %9s %9s" % ("stage", "p50", "p90", "p95", "max"))
    print("-" * 70)
    for idx, label in ((1, "A  speech end -> endpoint"), (2, "B  endpoint -> STT start"),
                       (3, "C  speech-to-text"), (4, "D  dispatch (resolve + launch)"),
                       (5, "E  TOTAL after speech ends")):
        v = [r[idx] * 1000 for r in rows]
        print("%-28s %8.1fms %8.1fms %8.1fms %8.1fms"
              % (label, q(v, 0.5), q(v, 0.9), q(v, 0.95), max(v) if v else float("nan")))
    print("\nn=%d  model calls: gemini %d, ollama %d  (launches recorded: %d)"
          % (len(rows), gemini.calls, local.calls, len(launched)))


if __name__ == "__main__":
    main()
