"""Do spoken application names survive speech-to-text well enough to resolve?

The measure that matters is NOT word error rate: it is whether the transcript still resolves to the right installed
application through the existing deterministic hierarchy (exact -> spacing -> prefix -> publisher -> sound). A
transcript can be wrong and still work ("what's up" -> WhatsApp), and can be nearly right and still fail.

Commands are synthesised locally with Windows SAPI for the applications ACTUALLY installed on this machine, in two
voices, at normal level and DEGRADED (quieter, with noise) because quiet speech is the known failure mode. Each
transcript is then put through ``fast_path.launch_phrases`` and ``AppCatalog.resolve_name`` exactly as production
would, and the outcome is recorded as resolved / wrong / ambiguous / missed.

usage:
    python scripts/bench/stt_accuracy.py
    python scripts/bench/stt_accuracy.py --temperature 0        # the deterministic single-pass decode
    python scripts/bench/stt_accuracy.py --both                 # compare default fallback against temperature=0

Nothing is uploaded and nothing is committed: the clips are TTS output in the temp directory.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from void.actions.computer import AppCatalog, RealWindowsBackend     # noqa: E402
from void.config import Config                                      # noqa: E402
from void.core.fast_path import launch_phrases                      # noqa: E402
from void.voice import cuda as _cuda                                # noqa: E402

CFG = Config.load()

# (spoken command, the installed application it must resolve to). Only applications present on this machine are
# used; the set deliberately covers what the milestone named - short names, similar-sounding names, spelled-out
# initialisms, a vendor word the installed name omits, and one application that is NOT installed.
COMMANDS = [
    ("Open ChatGPT.", "ChatGPT"),
    ("Open WhatsApp.", "WhatsApp"),
    ("Open VS Code.", "Visual Studio Code"),
    ("Open Visual Studio Code.", "Visual Studio Code"),
    ("Open Windows Terminal.", "Terminal"),
    ("Open Terminal.", "Terminal"),
    ("Open File Explorer.", "File Explorer"),
    ("Open Discord.", "Discord"),
    ("Open Cursor.", "Cursor"),
    ("Open Opera GX.", "Opera GX Browser"),
    ("Open Microsoft Teams.", "Microsoft Teams"),
    ("Open Teams.", "Microsoft Teams"),
    ("Open Word.", "Word"),
    ("Open Excel.", "Excel"),
    ("Open Paint.", "Paint"),
    ("Open Photos.", "Photos"),
    ("Open Settings.", "Settings"),
    ("Open Calculator.", "Calculator"),
    ("Open Clock.", "Clock"),
    ("Open Git Bash.", "Git Bash"),
    ("Open Chrome.", "Google Chrome"),
    ("Open Notepad.", "notepad"),
    ("Open Notepad plus plus.", None),          # NOT installed: must MISS, never resolve to something else
]

VOICES = ("Microsoft David Desktop", "Microsoft Zira Desktop")


def synth(out_dir: Path, voice: str):
    """Render every command once per voice, 16 kHz mono. Cached."""
    d = out_dir / voice.replace(" ", "_")
    d.mkdir(parents=True, exist_ok=True)
    todo = []
    for i, (text, _want) in enumerate(COMMANDS):
        p = d / ("cmd%02d.wav" % i)
        if not p.exists():
            todo.append((p, text))
    if todo:
        ps = ["Add-Type -AssemblyName System.Speech",
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
              "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
              "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
              "[System.Speech.AudioFormat.AudioChannel]::Mono)",
              "$s.SelectVoice('" + voice + "')"]
        for p, text in todo:
            ps.append("$s.SetOutputToWaveFile('" + str(p).replace("'", "''") + "', $fmt); $s.Speak('"
                      + text.replace("'", "''") + "'); $s.SetOutputToNull()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(ps)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode:
            raise SystemExit("SAPI synthesis failed for " + voice + ": " + r.stderr[-300:])
    return [d / ("cmd%02d.wav" % i) for i in range(len(COMMANDS))]


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def degrade(audio: np.ndarray, rng, gain=0.22, noise=0.05) -> np.ndarray:
    """Quieter, with broadband noise: the condition under which real commands are observed to fail."""
    return (audio * gain + rng.standard_normal(len(audio)).astype(np.float32) * noise).astype(np.float32)


def outcome(transcript: str, want: str | None, catalog: AppCatalog):
    """What production would DO with this transcript."""
    phrases = launch_phrases(transcript)
    if not phrases:
        return "not_a_launch", ""
    for phrase in phrases:
        m = catalog.resolve_name(phrase)
        if m.entry is not None:
            if want is None:
                return "wrong", m.entry.name              # should not have resolved at all
            return ("resolved" if m.entry.name == want else "wrong"), m.entry.name + " [" + (m.tier or "") + "]"
        if m.reason == "ambiguous":
            return "ambiguous", ", ".join(c.name for c in m.candidates[:3])
    return ("missed" if want else "correctly_missed"), ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--temperature", type=float, default=None,
                    help="0 disables Whisper's rising-temperature retry (the shipping default is 0..1 in six steps)")
    ap.add_argument("--both", action="store_true", help="measure the default AND temperature=0")
    ap.add_argument("--clips-dir", default=str(Path(os.environ.get("TEMP", ".")) / "void_stt_acc"))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    _cuda.register()
    from void.voice.adapters import FasterWhisperSTT
    catalog = AppCatalog(RealWindowsBackend())
    print("catalog: %d applications" % len(catalog.entries()))

    settings = [None, 0.0] if args.both else [args.temperature]
    rng = np.random.default_rng(11)
    clips = {v: synth(Path(args.clips_dir), v) for v in VOICES}
    rows = []

    for temp in settings:
        stt = FasterWhisperSTT(model_name=CFG.get("voice.stt_model", "small"),
                               device=CFG.get("voice.stt_device", "cpu"),
                               compute_type=CFG.get("voice.stt_compute_type", "float16"),
                               beam_size=int(CFG.get("voice.stt_beam_size", 1)),
                               vad_filter=bool(CFG.get("voice.stt_vad_filter", True)))
        if temp is not None:
            # Same code path, one option changed, so the comparison is honest.
            base = stt._decode

            def patched(audio, *, vad, _base=base, _t=temp):
                model = stt._model
                kw = dict(language=stt._language, beam_size=stt._beam_size,
                          condition_on_previous_text=False, temperature=_t)
                if vad:
                    kw["vad_filter"] = True
                segs, _info = model.transcribe(audio, **kw)
                return "".join(s.text for s in segs).strip()
            stt._decode = patched
        stt.warmup()
        label = "default" if temp is None else ("temperature=%g" % temp)
        print("\n=== %s (device=%s) ===" % (label, stt.device))
        print("%-28s %-9s %-11s %-38s %s" % ("command", "level", "outcome", "transcript", "resolved to"))
        for voice, paths in clips.items():
            for (text, want), path in zip(COMMANDS, paths):
                clean = load(path)
                for level, audio in (("normal", clean), ("degraded", degrade(clean, rng))):
                    t0 = time.perf_counter()
                    tr = stt.transcribe(audio)
                    ms = (time.perf_counter() - t0) * 1000
                    got, detail = outcome(tr, want, catalog)
                    rows.append({"setting": label, "voice": voice, "level": level, "said": text,
                                 "want": want, "transcript": tr, "outcome": got, "resolved": detail,
                                 "decode_ms": round(ms, 1)})
                    flag = "" if got in ("resolved", "correctly_missed") else "   <-- "
                    print("%-28s %-9s %-11s %-38s %s%s"
                          % (text[:27], level, got, repr(tr)[:37], detail[:34], flag))

    print("\n" + "=" * 100)
    by = collections.defaultdict(collections.Counter)
    ms = collections.defaultdict(list)
    for r in rows:
        by[(r["setting"], r["level"])][r["outcome"]] += 1
        ms[(r["setting"], r["level"])].append(r["decode_ms"])
    print("%-22s %-9s %5s %9s %9s %9s %8s %9s %10s"
          % ("setting", "level", "n", "resolved", "missed", "wrong", "ambig", "no-launch", "p50 decode"))
    for key in sorted(by):
        c = by[key]
        n = sum(c.values())
        d = sorted(ms[key])
        ok = c["resolved"] + c["correctly_missed"]
        print("%-22s %-9s %5d %8d%% %9d %9d %8d %9d %9.0fms"
              % (key[0], key[1], n, round(100 * ok / n), c["missed"], c["wrong"], c["ambiguous"],
                 c["not_a_launch"], d[len(d) // 2]))
    print("\nfailures (anything that would not have opened the right application):")
    for r in rows:
        if r["outcome"] not in ("resolved", "correctly_missed"):
            print("  %-22s %-9s %-24s %-11s %r -> %s"
                  % (r["setting"], r["level"], r["said"][:23], r["outcome"], r["transcript"][:40], r["resolved"]))
    if args.out:
        Path(args.out).write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        print("\nrows written to " + args.out)


if __name__ == "__main__":
    main()
