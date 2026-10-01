"""Controlled speech-to-text benchmark: what ACTUALLY makes a decode slow?

The live runtime's telemetry shows a heavy tail (non-empty decodes: p50 217 ms, p90 1.68 s, max 2.51 s over 40 real
commands since the GPU switch). One hypothesis was GPU contention with Ollama. This benchmark exists to test that
hypothesis rather than assume it, alongside the other candidates - device power state, model load, repetition,
audio length and CPU.

Conditions (--condition, default all):

  A alone            no model resident; GPU otherwise idle
  B ollama_idle      Ollama server up, NO model resident
  C ollama_loaded    the configured local model resident in VRAM, not generating
  C2 ollama_busy     decode WHILE Ollama is generating (the actual compute-contention case)
  D repeated         many decodes back to back, same warm model
  E cold             a FRESH PROCESS: first decode pays model load
  F warm             after warmup(), the shipping start-up behaviour
  G idle_gap         a decode after N seconds of GPU idleness (device power state / clock ramp)
  H wake_running     decode while V.O.I.D's OWN Gen 3 wake encoder runs (a second Whisper-small, same process)

Every decode records: wall clock, audio seconds, transcript, GPU utilisation / memory / SM clock / P-state
sampled DURING the decode, process CPU seconds and RSS, whether a model was resident in VRAM, and what Ollama was
doing. Results are written as JSON lines so they can be re-analysed without re-running.

usage:
    python scripts/bench/stt_bench.py --repeats 8 --out runs.jsonl
    python scripts/bench/stt_bench.py --condition E --cold-repeats 3
    python scripts/bench/stt_bench.py --analyse runs.jsonl

Clips are synthesised locally with Windows SAPI (never uploaded, never committed); --personal additionally uses the
owner's local wake recordings, which .gitignore already excludes.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from void.config import Config                                   # noqa: E402
from void.voice import cuda as _cuda                             # noqa: E402

CFG = Config.load()
OLLAMA = CFG.get("llm.local.base_url", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = CFG.get("llm.local.model", "qwen3:8b")

# Realistic spoken commands, short to long. The text matters for the transcript column; the LENGTH is what drives
# decode cost, so the set spans roughly 1-5 s.
PHRASES = [
    ("c1_short", "Open Chrome."),
    ("c2_name", "Open Visual Studio Code."),
    ("c3_multi", "Open VS Code and WhatsApp."),
    ("c4_folder", "Open my projects folder and Windows Terminal."),
    ("c5_question", "What is the weather going to be like in London tomorrow afternoon?"),
]


# --- machine state ------------------------------------------------------------------------------

_GPU_FIELDS = ("utilization.gpu", "memory.used", "clocks.sm", "pstate", "temperature.gpu")


def gpu_sample():
    """One nvidia-smi reading, or None when there is no NVIDIA GPU / no driver."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=" + ",".join(_GPU_FIELDS), "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    if len(parts) != len(_GPU_FIELDS):
        return None

    def num(v):
        try:
            return float(v)
        except ValueError:
            return v
    return {"util": num(parts[0]), "mem_mib": num(parts[1]), "sm_mhz": num(parts[2]),
            "pstate": parts[3], "temp_c": num(parts[4])}


class GpuStream:
    """ONE long-lived ``nvidia-smi --loop-ms`` process, sampled continuously for the whole benchmark.

    Spawning nvidia-smi per sample costs 138 ms here - more than a typical decode - so per-decode sampling would
    both miss the decode and add the CPU contention it is meant to observe. Streaming costs one process for the
    run; ``slice()`` then reports what the device was doing while a given decode ran, including whether its clocks
    were still ramping up out of the idle P-state.
    """

    def __init__(self, interval_ms=100):
        self.interval_ms = interval_ms
        self.samples = []                                # (monotonic_arrival, util, mem_mib, sm_mhz, pstate)
        self._proc = None
        self._t = None

    def __enter__(self):
        if gpu_sample() is None:
            return self                                  # no GPU: every field stays None, nothing else changes
        try:
            self._proc = subprocess.Popen(
                ["nvidia-smi", "--query-gpu=" + ",".join(_GPU_FIELDS),
                 "--format=csv,noheader,nounits", "--loop-ms=" + str(self.interval_ms)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except (OSError, subprocess.SubprocessError):
            return self
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()
        return self

    def _read(self):
        for line in self._proc.stdout:
            parts = [x.strip() for x in line.split(",")]
            if len(parts) != len(_GPU_FIELDS):
                continue
            try:
                self.samples.append((time.monotonic(), float(parts[0]), float(parts[1]),
                                     float(parts[2]), parts[3]))
            except ValueError:
                continue

    def __exit__(self, *exc):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        return False

    def slice(self, t0, t1):
        """What the GPU was doing between two monotonic instants."""
        got = [s for s in self.samples if t0 <= s[0] <= t1 + self.interval_ms / 1000.0]
        if not got:
            # A decode shorter than the sampling interval: report the nearest sample rather than nothing.
            got = sorted(self.samples, key=lambda s: abs(s[0] - t0))[:1]
        if not got:
            return {"gpu_samples": 0}
        util = [s[1] for s in got]
        mem = [s[2] for s in got]
        sm = [s[3] for s in got]
        return {"gpu_samples": len(got),
                "gpu_util_max": max(util), "gpu_util_mean": round(sum(util) / len(util), 1),
                "gpu_mem_max_mib": max(mem),
                "gpu_sm_min_mhz": min(sm), "gpu_sm_max_mhz": max(sm),
                "gpu_pstate_first": got[0][4], "gpu_pstate_last": got[-1][4]}


def proc_state():
    """This process's CPU time and resident memory, plus the system's free RAM when psutil is available."""
    out = {}
    try:
        import psutil
        p = psutil.Process()
        out["proc_cpu_s"] = round(sum(p.cpu_times()[:2]), 2)
        out["proc_rss_mib"] = round(p.memory_info().rss / 2 ** 20, 1)
        out["sys_ram_avail_mib"] = round(psutil.virtual_memory().available / 2 ** 20)
        out["sys_cpu_pct"] = psutil.cpu_percent(interval=None)
    except Exception:                                    # noqa: BLE001 - telemetry must never fail a benchmark
        pass
    return out


def _ollama(path, payload=None, timeout=300):
    url = OLLAMA + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ollama_resident():
    """What Ollama currently holds in VRAM."""
    try:
        d = _ollama("/api/ps", timeout=5)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return {"up": False, "models": []}
    return {"up": True,
            "models": [(m.get("name"), round(m.get("size_vram", 0) / 2 ** 30, 2)) for m in d.get("models", [])]}


def ollama_load(keep_alive="10m"):
    """Make the configured model resident WITHOUT generating anything (an empty prompt loads it)."""
    try:
        _ollama("/api/generate", {"model": OLLAMA_MODEL, "keep_alive": keep_alive}, timeout=600)
    except Exception as exc:                             # noqa: BLE001
        print("  ! could not load " + OLLAMA_MODEL + ": " + type(exc).__name__, file=sys.stderr)
        return False
    return bool(ollama_resident()["models"])


def ollama_unload():
    try:
        _ollama("/api/generate", {"model": OLLAMA_MODEL, "keep_alive": 0}, timeout=120)
    except Exception:                                    # noqa: BLE001
        pass


class WakeRunning:
    """Runs the REAL Gen 3 wake detector alongside the decodes, fed with audio, exactly as production does.

    The production runtime holds a second Whisper-small encoder in the same process and scores a rolling 2 s window
    every 200 ms. It is disarmed only on the next monitor tick after the session leaves IDLE, so it is still running
    when a capture ends - and a decode therefore does not have the process to itself. Nothing here touches the
    microphone: frames come from the benchmark's own clip, so no audio is captured or stored.
    """

    def __init__(self, frames_audio):
        self._audio = frames_audio
        self._det = None
        self._stop = threading.Event()
        self._t = None
        self.inferences = 0
        self.failed = ""

    def __enter__(self):
        try:
            from void.voice.wake import create_wake_detector
            self._det = create_wake_detector(CFG)
            self._det.start()
        except Exception as exc:                         # noqa: BLE001 - a machine without the model skips this
            self.failed = type(exc).__name__
            print("  ! wake detector unavailable (" + self.failed + "); condition H is not valid", file=sys.stderr)
            return self
        self._t = threading.Thread(target=self._feed, daemon=True)
        self._t.start()
        time.sleep(3.0)                                  # let the rolling window fill and inference begin
        return self

    def _feed(self):
        """30 ms frames of the clip, on a loop, at real time - the cadence the broker fans out."""
        pcm = (self._audio * 32767).astype("<i2").tobytes()
        frame = 480 * 2
        i = 0
        while not self._stop.is_set():
            chunk = pcm[i:i + frame]
            if len(chunk) < frame:
                i = 0
                continue
            i += frame
            try:
                self._det.feed_audio(chunk)
                self.inferences += 1
            except Exception:                            # noqa: BLE001
                return
            self._stop.wait(0.03)

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=10)
        if self._det is not None:
            try:
                self._det.stop()
                self._det.close()
            except Exception:                            # noqa: BLE001
                pass
        return False


class OllamaBusy:
    """Keeps Ollama GENERATING on a background thread, so a decode overlaps real GPU compute."""

    def __init__(self, prompt="Explain how a compiler works, in detail."):
        self.prompt = prompt
        self._stop = threading.Event()
        self._t = None
        self.calls = 0
        self.reached_load = False

    def _run(self):
        n = 0
        while not self._stop.is_set():
            n += 1
            try:
                # A fresh prompt each time: repeating one lets the KV cache answer instantly and the GPU goes idle.
                _ollama("/api/generate", {"model": OLLAMA_MODEL, "keep_alive": "10m", "stream": False,
                                          "prompt": self.prompt + " (variation " + str(n) + ")",
                                          "options": {"num_predict": 256}}, timeout=600)
                self.calls += 1
            except Exception:                            # noqa: BLE001
                self._stop.wait(1.0)

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        # Wait until the GPU is actually busy, otherwise the "busy" condition measures nothing.
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            s = gpu_sample()
            if s and isinstance(s["util"], float) and s["util"] >= 30:
                self.reached_load = True
                return self
            time.sleep(0.5)
        print("  ! Ollama never reached 30 % GPU utilisation; the 'busy' condition is not valid", file=sys.stderr)
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join(timeout=30)
        return False


# --- clips -------------------------------------------------------------------------------------

def synth_clips(out_dir):
    """Render the command phrases to 16 kHz mono wav with Windows SAPI. Cached; nothing leaves the machine."""
    out_dir.mkdir(parents=True, exist_ok=True)
    missing = [(cid, text) for cid, text in PHRASES if not (out_dir / (cid + ".wav")).exists()]
    if missing:
        ps = ["Add-Type -AssemblyName System.Speech",
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
              "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
              "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
              "[System.Speech.AudioFormat.AudioChannel]::Mono)"]
        for cid, text in missing:
            path = str(out_dir / (cid + ".wav")).replace("'", "''")
            ps.append("$s.SetOutputToWaveFile('" + path + "', $fmt); $s.Speak('"
                      + text.replace("'", "''") + "'); $s.SetOutputToNull()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(ps)],
                           capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise SystemExit("SAPI synthesis failed: " + r.stderr[-400:])
    return [(cid, out_dir / (cid + ".wav")) for cid, _ in PHRASES]


def load_audio(path):
    with wave.open(str(path)) as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, path
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if rate != 16000:                                    # faster-whisper expects 16 kHz
        n = int(len(audio) * 16000 / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


# --- the measurement ---------------------------------------------------------------------------

def make_stt():
    from void.voice.adapters import FasterWhisperSTT
    _cuda.register()
    return FasterWhisperSTT(model_name=CFG.get("voice.stt_model", "small"),
                            device=CFG.get("voice.stt_device", "cpu"),
                            compute_type=CFG.get("voice.stt_compute_type", "int8"),
                            beam_size=int(CFG.get("voice.stt_beam_size", 1)),
                            vad_filter=bool(CFG.get("voice.stt_vad_filter", True)))


def decode_once(stt, clip_id, audio, condition, extra, gpu=None):
    """One measured decode. The row carries everything needed to explain it afterwards.

    ``gpu`` is the shared :class:`GpuStream`; the state BEFORE the decode comes from its last sample rather than a
    fresh nvidia-smi call, so nothing is spawned on the measured path.
    """
    res = ollama_resident()
    before = {}
    if gpu is not None and gpu.samples:
        last = gpu.samples[-1]
        before = {"util": last[1], "mem_mib": last[2], "sm_mhz": last[3], "pstate": last[4]}
    t0 = time.monotonic()
    tp = time.perf_counter()
    text = stt.transcribe(audio)
    decode_s = time.perf_counter() - tp
    t1 = time.monotonic()
    row = {"condition": condition, "clip": clip_id, "audio_s": round(len(audio) / 16000.0, 3),
           "decode_s": round(decode_s, 4), "transcript": text,
           "device": stt.device,
           "gpu_util_before": before.get("util"), "gpu_mem_before_mib": before.get("mem_mib"),
           "gpu_sm_before_mhz": before.get("sm_mhz"), "gpu_pstate_before": before.get("pstate"),
           "ollama_up": res["up"], "ollama_resident_gb": round(sum(g for _, g in res["models"]), 2),
           "ts": round(time.time(), 3)}
    row.update(gpu.slice(t0, t1) if gpu is not None else {"gpu_samples": 0})
    row.update(proc_state())
    row.update(extra)
    return row


def run_conditions(args, sink, rows, gpu):
    clips = synth_clips(Path(args.clips_dir))
    audio = collections.OrderedDict((cid, load_audio(p)) for cid, p in clips)
    if args.personal:
        personal = sorted(Path("wakeword-training/data/personal_positive").glob("Recording*.wav"))
        for p in personal[:6]:
            try:
                audio["p_" + p.stem.replace(" ", "")] = load_audio(p)
            except AssertionError:
                pass
    print("clips: " + ", ".join("%s=%.2fs" % (k, len(v) / 16000.0) for k, v in audio.items()) + "\n")

    want = set(args.condition) if args.condition else {"A", "B", "C", "C2", "D", "E", "F", "G", "H"}

    # E (cold) must be measured in a FRESH PROCESS, because a loaded model cannot be unloaded honestly.
    if "E" in want:
        print("[E] cold: first decode in a fresh process (model load included)")
        for i in range(args.cold_repeats):
            r = subprocess.run([sys.executable, __file__, "--cold-child", "--clips-dir", args.clips_dir],
                               capture_output=True, text=True, timeout=900,
                               cwd=str(Path(__file__).resolve().parents[2]))
            for ln in r.stdout.splitlines():
                if ln.startswith("{"):
                    row = json.loads(ln)
                    row["run"] = i
                    sink(row)
                    print("    cold decode %d: %8.0f ms   model_load=%.0f ms  sm_during=%s MHz"
                          % (i, row["decode_s"] * 1000, row.get("model_load_s", 0) * 1000,
                             row.get("gpu_sm_max_mhz")))
            if r.returncode:
                print("    ! cold child failed: " + r.stderr[-300:], file=sys.stderr)

    if not (want - {"E"}):
        return

    ollama_unload()
    stt = make_stt()
    t0 = time.perf_counter()
    stt.warmup()
    warm_s = time.perf_counter() - t0
    print("\nSTT device=%s model=%s warmup=%.0f ms"
          % (stt.device, CFG.get("voice.stt_model", "small"), warm_s * 1000))

    def sweep(condition, repeats, extra=None):
        for i in range(repeats):
            for cid, a in audio.items():
                row = decode_once(stt, cid, a, condition, dict({"run": i}, **(extra or {})), gpu)
                sink(row)
                print("    %-14s %-20s %8.1f ms  util=%s%%  sm=%sMHz  ollama=%.2fGB  %r"
                      % (condition, cid, row["decode_s"] * 1000, row.get("gpu_util_max"),
                         row.get("gpu_sm_max_mhz"), row["ollama_resident_gb"], row["transcript"][:38]))

    if "F" in want:
        print("\n[F] warm: the shipping start-up behaviour (warmup() has run)")
        sweep("F_warm", args.repeats)

    if "A" in want:
        print("\n[A] alone: no model resident, GPU otherwise idle")
        ollama_unload()
        sweep("A_alone", args.repeats)

    if "B" in want:
        res = ollama_resident()
        print("\n[B] ollama_idle: server up=%s, no model resident" % res["up"])
        ollama_unload()
        sweep("B_ollama_idle", args.repeats, {"ollama_state": "idle"})

    if "C" in want:
        print("\n[C] ollama_loaded: %s resident in VRAM, not generating" % OLLAMA_MODEL)
        if ollama_load():
            print("    resident: %s" % (ollama_resident()["models"],))
            sweep("C_ollama_loaded", args.repeats, {"ollama_state": "loaded"})
        else:
            print("    skipped: the model could not be made resident")

    if "C2" in want:
        print("\n[C2] ollama_busy: decoding WHILE %s generates" % OLLAMA_MODEL)
        if ollama_load():
            with OllamaBusy() as busy:
                sweep("C2_ollama_busy", args.repeats,
                      {"ollama_state": "generating", "ollama_reached_load": busy.reached_load})
        else:
            print("    skipped: the model could not be made resident")
        ollama_unload()

    if "D" in want:
        print("\n[D] repeated: the same clip many times, back to back")
        cid = "c2_name"
        for i in range(args.repeated_n):
            sink(decode_once(stt, cid, audio[cid], "D_repeated", {"run": i}, gpu))
        rs = [r for r in rows if r["condition"] == "D_repeated"]
        print("    %d decodes: first=%.1f ms  min=%.1f ms  max=%.1f ms"
              % (len(rs), rs[0]["decode_s"] * 1000,
                 min(r["decode_s"] for r in rs) * 1000, max(r["decode_s"] for r in rs) * 1000))

    if "G" in want:
        print("\n[G] idle_gap: one decode after %.0f s of GPU idleness, then immediately again" % args.idle_s)
        ollama_unload()
        for i in range(args.idle_repeats):
            time.sleep(args.idle_s)
            s = gpu_sample() or {}
            row = decode_once(stt, "c2_name", audio["c2_name"], "G_idle_gap",
                              {"run": i, "idle_s": args.idle_s}, gpu)
            sink(row)
            print("    after %5.0fs idle (sm=%sMHz pstate=%s): %8.1f ms  sm_during=%sMHz"
                  % (args.idle_s, s.get("sm_mhz"), s.get("pstate"), row["decode_s"] * 1000,
                     row.get("gpu_sm_max_mhz")))
            row2 = decode_once(stt, "c2_name", audio["c2_name"], "G_immediate", {"run": i}, gpu)
            sink(row2)
            print("    immediately again:                            %8.1f ms  sm_during=%sMHz"
                  % (row2["decode_s"] * 1000, row2.get("gpu_sm_max_mhz")))

    if "H" in want:
        print("\n[H] wake_running: decoding while V.O.I.D's own Gen 3 wake encoder runs in this process")
        ollama_unload()
        with WakeRunning(audio["c5_question"]) as wake:
            if wake.failed:
                print("    skipped: " + wake.failed)
            else:
                sweep("H_wake_running", args.repeats, {"wake": "running"})
                print("    wake frames fed: %d" % wake.inferences)


def cold_child(args):
    """A single cold decode, printed as one JSON line. Run as a subprocess by condition E."""
    clips = synth_clips(Path(args.clips_dir))
    audio = load_audio(dict(clips)["c2_name"])
    stt = make_stt()
    t0 = time.perf_counter()
    stt._load()
    load_s = time.perf_counter() - t0
    with GpuStream() as gpu:
        time.sleep(0.3)                                  # let the stream produce a first sample
        print(json.dumps(decode_once(stt, "c2_name", audio, "E_cold",
                                     {"model_load_s": round(load_s, 4)}, gpu)))


# --- analysis ----------------------------------------------------------------------------------

def _pct(sorted_vals, frac):
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(frac * len(sorted_vals)))]


def analyse(rows):
    by = collections.defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    print("\n" + "=" * 112)
    print("%-18s %4s %9s %9s %9s %11s %13s %8s %10s"
          % ("condition", "n", "p50", "p90", "max", "ms/audio_s", "gpu_util_max", "sm_max", "ollama_gb"))
    print("-" * 112)
    for cond in sorted(by):
        rs = by[cond]
        d = sorted(r["decode_s"] for r in rs)
        ratio = [r["decode_s"] / r["audio_s"] for r in rs if r["audio_s"]]
        util = [r["gpu_util_max"] for r in rs if isinstance(r.get("gpu_util_max"), (int, float))]
        sm = [r["gpu_sm_max_mhz"] for r in rs if isinstance(r.get("gpu_sm_max_mhz"), (int, float))]
        oll = [r.get("ollama_resident_gb", 0) for r in rs]
        print("%-18s %4d %7.1fms %7.1fms %7.1fms %10.1f %12.0f%% %7.0f %9.2f"
              % (cond, len(d), _pct(d, 0.5) * 1000, _pct(d, 0.9) * 1000, d[-1] * 1000,
                 1000 * sum(ratio) / len(ratio) if ratio else 0,
                 max(util) if util else 0, max(sm) if sm else 0, max(oll) if oll else 0))
    print("\nby clip (all conditions):")
    byc = collections.defaultdict(list)
    for r in rows:
        byc[r["clip"]].append(r)
    for cid in sorted(byc):
        rs = byc[cid]
        d = sorted(r["decode_s"] for r in rs)
        print("  %-22s audio=%5.2fs n=%4d p50=%8.1fms p90=%8.1fms max=%8.1fms"
              % (cid, rs[0]["audio_s"], len(d), _pct(d, 0.5) * 1000, _pct(d, 0.9) * 1000, d[-1] * 1000))
    print("\ntranscripts (first occurrence per clip):")
    seen = set()
    for r in rows:
        if r["clip"] not in seen:
            seen.add(r["clip"])
            print("  %-22s %r" % (r["clip"], r["transcript"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=4)
    ap.add_argument("--repeated-n", type=int, default=25)
    ap.add_argument("--cold-repeats", type=int, default=3)
    ap.add_argument("--idle-s", type=float, default=60.0)
    ap.add_argument("--idle-repeats", type=int, default=3)
    ap.add_argument("--condition", action="append", help="A B C C2 D E F G (repeatable); default all")
    ap.add_argument("--clips-dir", default=str(Path(os.environ.get("TEMP", ".")) / "void_stt_clips"))
    ap.add_argument("--personal", action="store_true", help="also use the owner's local wake recordings")
    ap.add_argument("--out", default="")
    ap.add_argument("--analyse", default="", help="re-analyse an existing jsonl instead of measuring")
    ap.add_argument("--cold-child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.analyse:
        analyse([json.loads(l) for l in Path(args.analyse).read_text(encoding="utf-8").splitlines() if l.strip()])
        return
    if args.cold_child:
        cold_child(args)
        return

    rows = []
    out = Path(args.out) if args.out else None
    fh = out.open("w", encoding="utf-8") if out else None

    def sink(row):
        rows.append(row)
        if fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()

    try:
        with GpuStream() as gpu:
            time.sleep(0.3)
            run_conditions(args, sink, rows, gpu)
    finally:
        if fh:
            fh.close()
    analyse(rows)
    if out:
        print("\nrows written to " + str(out))


if __name__ == "__main__":
    main()
