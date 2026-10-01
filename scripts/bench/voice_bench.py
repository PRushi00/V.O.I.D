"""EVALUATION-ONLY voice sample generator/latency probe. NOT a V.O.I.D TTS adapter (no adapter is built in this phase).

Providers reachable in this environment:
  gemini-tts   uses V.O.I.D's existing Gemini credential pool (no new secret); audio -> WAV; TTFA/total measured
  sapi         local Windows System.Speech (the current V.O.I.D voice family); WAV via PowerShell; full-render time measured
Providers WITHOUT a credential here (ElevenLabs, Cartesia, Fish Audio): nothing is called; the same script is used in their web
playgrounds (see the report). Secrets are read via CredentialPool in-process and never printed or written.

usage:
  python voice_bench.py gemini --model gemini-3.1-flash-tts-preview --voices Kore,Charon,Puck --out DIR [--lines V01,V03]
  python voice_bench.py sapi --voices "Microsoft David Desktop,Microsoft Zira Desktop" --out DIR
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

os.environ["USERPROFILE"] = os.environ["HOME"] = tempfile.mkdtemp(prefix="void-vbench-")
sys.path.insert(0, r"C:\V.O.I.D")
sys.path.insert(0, str(Path(__file__).parent))
from voice_script import LINES                                     # noqa: E402


def _write_wav(path: Path, pcm: bytes, rate=24000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)


def cmd_gemini(a):
    from google.genai import types
    from void.providers import gemini_provider as gp
    from void.security.credentials import CredentialPool
    from datetime import datetime, timezone
    pool = CredentialPool()
    from google import genai
    rows = []
    want = set(a.lines.split(",")) if a.lines else None
    for voice in a.voices.split(","):
        for lid, purpose, text, _listen in LINES:
            if want and not any(lid.startswith(x) for x in want):
                continue
            cfg = types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))))
            row = {"provider": "gemini-tts", "model": a.model, "voice": voice, "line": lid, "chars": len(text)}
            t0 = time.perf_counter(); chunks = []; ttfa = None; err = None
            try:
                cred = pool.get_next_available(now=datetime.now(timezone.utc))
                client = genai.Client(api_key=pool.get_value(cred), http_options=types.HttpOptions(timeout=40000))
                for ch in client.models.generate_content_stream(model=a.model, contents=text, config=cfg):
                    for cand in (ch.candidates or []):
                        for part in (cand.content.parts if cand.content and cand.content.parts else []):
                            if getattr(part, "inline_data", None) and part.inline_data.data:
                                if ttfa is None:
                                    ttfa = time.perf_counter() - t0
                                chunks.append(part.inline_data.data)
            except Exception as exc:
                err = f"{type(exc).__name__}:{getattr(exc, 'code', '')}"
            total = time.perf_counter() - t0
            pcm = b"".join(chunks)
            row.update({"ttfa_s": None if ttfa is None else round(ttfa, 3), "total_s": round(total, 3), "audio_s": round(len(pcm) / 48000, 2),
                        "bytes": len(pcm), "error": err, "stream_chunks": len(chunks)})
            if pcm:
                _write_wav(Path(a.out) / f"gemini-tts_{a.model}_{voice}" / f"{lid}.wav", pcm)
            rows.append(row)
            print(json.dumps(row), flush=True)
            time.sleep(4)
    Path(a.out, f"gemini_tts_{a.model}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")


def cmd_sapi(a):
    out = Path(a.out)
    ps = ["Add-Type -AssemblyName System.Speech", "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
          "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)"]
    for voice in a.voices.split(","):
        d = out / f"sapi_{voice.replace(' ', '_')}"
        d.mkdir(parents=True, exist_ok=True)
        ps.append(f"$s.SelectVoice('{voice}')")
        for lid, _p, text, _l in LINES:
            path = str(d / f"{lid}.wav").replace("'", "''")
            safe = text.replace("'", "''")
            ps.append(f"$sw = [Diagnostics.Stopwatch]::StartNew(); $s.SetOutputToWaveFile('{path}', $fmt); $s.Speak('{safe}'); $s.SetOutputToNull(); $sw.Stop(); "
                      f"Write-Output ('{voice}|{lid}|' + $sw.Elapsed.TotalSeconds)")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(ps)], capture_output=True, text=True, timeout=300)
    rows = []
    for ln in r.stdout.splitlines():
        v, lid, secs = ln.split("|")
        wav = out / f"sapi_{v.replace(' ', '_')}" / f"{lid}.wav"
        dur = None
        if wav.exists():
            with wave.open(str(wav)) as w:
                dur = round(w.getnframes() / w.getframerate(), 2)
        rows.append({"provider": "sapi", "voice": v, "line": lid, "render_total_s": round(float(secs), 3), "audio_s": dur})
        print(json.dumps(rows[-1]))
    (out / "sapi.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    if r.returncode:
        print("stderr:", r.stderr[-300:])


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gemini"); g.add_argument("--model", default="gemini-3.1-flash-tts-preview"); g.add_argument("--voices", default="Kore")
    g.add_argument("--out", required=True); g.add_argument("--lines"); g.set_defaults(fn=cmd_gemini)
    s = sub.add_parser("sapi"); s.add_argument("--voices", default="Microsoft David Desktop,Microsoft Zira Desktop"); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_sapi)
    args = ap.parse_args(); args.fn(args)
