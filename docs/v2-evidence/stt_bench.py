"""STT compute/VRAM measurement for the V2 plan (scratch; no downloads: local_files_only).
Audio is SYNTHETIC (Windows SAPI voice) => valid for compute/latency, NOT for accuracy."""
import os, sys, time, wave, subprocess, statistics as st
import numpy as np
S = os.path.dirname(os.path.abspath(__file__))

def vram_used_mib():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out.splitlines()[0])

def make_wav(text, path):
    import win32com.client, pythoncom
    pythoncom.CoInitialize()
    v = win32com.client.Dispatch("SAPI.SpVoice"); fs = win32com.client.Dispatch("SAPI.SpFileStream")
    fs.Format.Type = 22            # 22 kHz 16-bit mono
    fs.Open(path, 3); v.AudioOutputStream = fs; v.Speak(text); fs.Close()

def load16k(path):
    with wave.open(path, "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1: pcm = pcm.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        t_old = np.arange(len(pcm)) / sr; t_new = np.arange(0, len(pcm) / sr, 1 / 16000)
        pcm = np.interp(t_new, t_old, pcm).astype(np.float32)
    return pcm

clips = {"short": "open notepad",
         "medium": "find my cybersecurity notes and open the folder called projects"}
audio = {}
for k, txt in clips.items():
    p = os.path.join(S, f"clip_{k}.wav"); make_wav(txt, p); audio[k] = load16k(p)
    print(f"clip {k}: {len(audio[k])/16000:.2f}s of audio")

from faster_whisper import WhisperModel
def bench(label, **kw):
    try:
        v0 = vram_used_mib(); t0 = time.perf_counter()
        m = WhisperModel("small", local_files_only=True, **kw)
        load_s = time.perf_counter() - t0; v1 = vram_used_mib()
    except Exception as e:
        print(f"[{label}] LOAD FAILED: {type(e).__name__}: {str(e)[:160]}"); return
    res = {}
    try:
        for k, a in audio.items():
            ts = []
            for i in range(4):
                t0 = time.perf_counter()
                segs, _ = m.transcribe(a, language="en", beam_size=1, condition_on_previous_text=False, vad_filter=True)
                text = "".join(s.text for s in segs).strip(); ts.append(time.perf_counter() - t0)
            res[k] = (st.median(ts[1:]), min(ts[1:]), text)       # drop the first (warm-up) run
    except Exception as e:
        print(f"[{label}] TRANSCRIBE FAILED: {type(e).__name__}: {str(e)[:160]}"); del m; return
    v2 = vram_used_mib()
    print(f"[{label}] load {load_s:.1f}s | VRAM used delta at load {v1-v0:+d} MiB, after decode {v2-v0:+d} MiB | " +
          " | ".join(f"{k}: median {r[0]:.2f}s (min {r[1]:.2f}) -> {r[2]!r}" for k, r in res.items()))
    del m

which = sys.argv[1]
print(f"VRAM used before STT model: {vram_used_mib()} MiB", flush=True)
if which == "cuda_fp16":    bench("CUDA float16", device="cuda", compute_type="float16")
elif which == "cuda_i8f16": bench("CUDA int8_float16", device="cuda", compute_type="int8_float16")
elif which.startswith("cpu"): bench(f"CPU int8 cpu_threads={which[3:]}", device="cpu", compute_type="int8", cpu_threads=int(which[3:]))
print("done", flush=True)
