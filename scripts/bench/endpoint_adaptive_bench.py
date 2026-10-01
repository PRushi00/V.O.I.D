"""Adaptive trailing silence, measured against a fixed one.

The endpointer under test is the PRODUCTION ``_WakeEndpointer``, driven frame by frame exactly as the broker drives
it. The judge is faster-whisper's OFFLINE Silero VAD, which sees the whole recording and is therefore better
informed than any streaming decision:

    endpoint latency = seconds between the reference end of speech and the capture closing
    truncated        = the capture closed BEFORE the reference end of speech (the command is cut)
    never fired      = the capture never closed on silence at all

Four corpora, and they are not equally authoritative:

  * ``real``      - the owner's own microphone recordings. A shipping decision rests on this one.
  * ``degraded``  - deliberately degraded training variants of those recordings: a robustness stress test.
  * ``spliced``   - two real recordings joined by a gap of REAL room noise of a known length, built here. This is
                    the only corpus with a controlled intra-utterance pause and real acoustics on both sides of it,
                    which is exactly what an adaptive budget has to survive.
  * ``synth``     - Windows SAPI renders of the command list this milestone was asked to cover. Useful for phrase
                    COVERAGE (a long sentence, a multi-application command, a deliberate pause); weak evidence about
                    acoustics, because synthesised speech has a different energy envelope from a real voice.

Audio is read and written locally only; the spliced clips go to the temp directory, never the repository.

usage:
    python scripts/bench/endpoint_adaptive_bench.py
    python scripts/bench/endpoint_adaptive_bench.py --corpus real --corpus spliced
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from faster_whisper.vad import VadOptions, get_speech_timestamps        # noqa: E402

from void.voice.runtime import _rms_int16, _WakeEndpointer, _WakePolicy  # noqa: E402

FRAME = 480
FRAME_S = FRAME / 16000
GATE = 300.0
REAL_DIR = "wakeword-training/data/personal_positive"

# The commands this milestone was asked to cover. The pause case uses SSML so the gap is a KNOWN length.
PHRASES = [
    ("p01_whatsapp", "Open WhatsApp."),
    ("p02_vscode", "Open VS Code."),
    ("p03_chatgpt", "Open ChatGPT."),
    ("p04_terminal", "Open Windows Terminal."),
    ("p05_projects", "Open Projects."),
    ("p06_arp", "Explain ARP."),
    ("p07_dns", "What is DNS?"),
    ("p08_long", "What is the difference between TCP and UDP, and when would I use each of them?"),
    ("p09_polite", "Can you open WhatsApp for me?"),
    ("p10_multi", "Open VS Code and WhatsApp."),
    ("p11_pause_short", 'Open <break time="250ms"/> WhatsApp.'),
    ("p12_pause_long", 'Open VS Code <break time="450ms"/> and WhatsApp.'),
    ("p13_hesitation", 'Explain <break time="300ms"/> ARP to me.'),
]


def frames_of(path):
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 16000:
            return None
        raw = w.readframes(w.getnframes())
    return [raw[i * FRAME * 2:(i + 1) * FRAME * 2] for i in range(len(raw) // (FRAME * 2))]


def reference_span(frames):
    """(first, last) speech frames according to the offline reference, or None if it hears no speech."""
    pcm = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    segs = get_speech_timestamps(pcm, VadOptions(min_silence_duration_ms=100, speech_pad_ms=0))
    if not segs:
        return None
    return int(segs[0]["start"] / FRAME), int(segs[-1]["end"] / FRAME)


def reference_end_frame(frames):
    """The last frame of speech according to the offline reference, or None if it hears no speech."""
    span = reference_span(frames)
    return None if span is None else span[1]


def run(frames, policy, tail_frames=300):
    """Drive the REAL endpointer. Returns (fire_frame, reason) - silence is appended so a capture always ends."""
    out = {}
    ep = _WakeEndpointer(policy, lambda reason: out.setdefault("reason", reason))
    silence = b"\x00\x00" * FRAME
    for i, f in enumerate(frames):
        ep(f)
        if out:
            return i, out["reason"]
    for j in range(tail_frames):
        ep(silence)
        if out:
            return len(frames) + j, out["reason"]
    return None, None


# --- corpora ----------------------------------------------------------------------------------

def room_noise(clips):
    """Frames of the owner's REAL room floor, taken from after speech ends. Never digital zeros."""
    noise = []
    for frames, ref in clips:
        noise.extend(frames[ref + 2:])
    return [f for f in noise if _rms_int16(f) < GATE] or [b"\x00\x00" * FRAME]


def speech_start(frames):
    """The first frame the offline reference calls speech - so a splice can begin exactly there."""
    span = reference_span(frames)
    return 0 if span is None else span[0]


def build_spliced(clips, out_dir, gaps_ms=(120, 180, 240, 300, 360, 420, 480, 540, 600)):
    """Two real utterances joined by a gap of real room noise, at a range of known lengths.

    This is the case an adaptive budget must survive: genuine speech on both sides, a genuine room floor in the
    middle, and a pause length we chose. Nothing is uploaded; the files live in the temp directory.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    noise = room_noise(clips)
    made = []
    # ONE fixed pair of utterances for every gap length, so the gap is the only variable and the series reads as
    # "the longest mid-utterance pause this configuration survives". Varying the pair per column made the columns
    # incomparable.
    a, b = clips[0], clips[3 % len(clips)]
    for gap_ms in gaps_ms:
        gap = [noise[i % len(noise)] for i in range(max(1, int(round(gap_ms / 1000 / FRAME_S))))]
        # From the first utterance's speech END to the second utterance's speech START, so the silence between them
        # is exactly gap_ms. Appending the second clip whole would add its own ~0.8 s of leading room floor, and the
        # corpus would be measuring that instead - which it was, truncating every configuration including the
        # baseline that truncates nothing on the real corpus.
        pcm = b"".join(a[0][:a[1] + 1] + gap + b[0][speech_start(b[0]):])
        path = out_dir / ("spliced_%03dms.wav" % gap_ms)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        made.append(path)
    return made


def build_two_pause(clips, out_dir, first_ms=250, second_gaps_ms=(240, 300, 360, 420, 480, 540)):
    """Three real utterances with TWO real-noise gaps: the case the pause latch exists for.

    A speaker who has already hesitated once is likely to hesitate again. The latch turns a survived first pause
    into evidence and restores the full budget - so this measures whether that actually buys anything, rather than
    assuming it. The first gap is fixed and long enough to count as evidence; the second is varied.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    noise = room_noise(clips)
    a, b, c = clips[0], clips[3 % len(clips)], clips[5 % len(clips)]

    def gap(ms):
        return [noise[i % len(noise)] for i in range(max(1, int(round(ms / 1000 / FRAME_S))))]
    made = []
    for second_ms in second_gaps_ms:
        mid = b[0][speech_start(b[0]):b[1] + 1]
        pcm = b"".join(a[0][:a[1] + 1] + gap(first_ms) + mid + gap(second_ms)
                       + c[0][speech_start(c[0]):])
        path = out_dir / ("two_pause_%03d_%03dms.wav" % (first_ms, second_ms))
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        made.append((second_ms, path))
    return made


def build_synth(out_dir):
    """SAPI renders of the command list, 16 kHz mono. Cached."""
    out_dir.mkdir(parents=True, exist_ok=True)
    todo = [(cid, t) for cid, t in PHRASES if not (out_dir / (cid + ".wav")).exists()]
    if todo:
        ps = ["Add-Type -AssemblyName System.Speech",
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
              "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, "
              "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
              "[System.Speech.AudioFormat.AudioChannel]::Mono)"]
        for cid, text in todo:
            path = str(out_dir / (cid + ".wav")).replace("'", "''")
            body = text.replace("'", "''")
            if "<break" in text:
                body = ("<speak version=\"1.0\" xmlns=\"http://www.w3.org/2001/10/synthesis\" "
                        "xml:lang=\"en-US\">" + body + "</speak>")
                say = "$s.SpeakSsml('" + body + "')"
            else:
                say = "$s.Speak('" + body + "')"
            ps.append("$s.SetOutputToWaveFile('" + path + "', $fmt); " + say + "; $s.SetOutputToNull()")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(ps)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode:
            print("  ! SAPI synthesis failed: " + r.stderr[-300:], file=sys.stderr)
    return [(cid, out_dir / (cid + ".wav")) for cid, _ in PHRASES if (out_dir / (cid + ".wav")).exists()]


# --- configurations under test ----------------------------------------------------------------

def policy(silence_s=0.6, fast=None, after=0.3, evidence=0.2, until=1.8):
    """``fast=None`` means "no adaptive budget" - the single fixed timeout that shipped before this milestone."""
    return _WakePolicy(no_speech_s=4.0, silence_s=silence_s, max_capture_s=15.0, rearm_delay_ms=500,
                       energy_threshold=GATE, lead_grace_s=0.4,
                       fast_silence_s=silence_s if fast is None else fast,
                       fast_after_speech_s=after, fast_until_speech_s=until,
                       pause_evidence_s=evidence)


CONFIGS = [
    ("FIXED 0.60 s  (baseline, shipped)", policy(0.60)),
    ("FIXED 0.40 s  (unconditional)", policy(0.40)),
    ("FIXED 0.30 s  (unconditional)", policy(0.30)),
    ("ADAPTIVE 0.40 after 0.30 s  (chosen)", policy(0.60, fast=0.40, after=0.30, evidence=0.20)),
    ("ADAPTIVE 0.35 after 0.30 s", policy(0.60, fast=0.35, after=0.30, evidence=0.20)),
    ("ADAPTIVE 0.30 after 0.30 s", policy(0.60, fast=0.30, after=0.30, evidence=0.20)),
    ("ADAPTIVE 0.40 after 0.30 s, no pause latch", policy(0.60, fast=0.40, after=0.30, evidence=0.0)),
    ("ADAPTIVE 0.40 after 0.60 s", policy(0.60, fast=0.40, after=0.60, evidence=0.20)),
]


def sweep_configs():
    """The grid the chosen policy was picked from.

    The first attempt (fast 0.40 s, evidence 0.20 s) truncated the long TCP/UDP sentence and the 450 ms-pause
    multi-application command: a long sentence's FIRST clause break can exceed the fast budget, and the latch only
    protects a pause once an earlier one has been survived. So the grid varies both the budget and how little
    hesitation counts as evidence.
    """
    out = []
    for fast in (0.30, 0.35, 0.40, 0.45):
        for until in (1.2, 1.8, 2.4, 99.0):
            out.append(("fast %.2f / until %.1fs" % (fast, until),
                        policy(0.60, fast=fast, after=0.30, evidence=0.15, until=until)))
    return out


def q(vals, frac):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(frac * len(vals)))] if vals else float("nan")


def score(clips, pol):
    lat, truncated, never = [], 0, 0
    for frames, ref in clips:
        fire, reason = run(frames, pol)
        if fire is None or reason != "silence":
            never += 1
            continue
        if fire < ref:
            truncated += 1
            continue
        lat.append((fire - ref) * FRAME_S)
    return lat, truncated, never


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", action="append", help="real degraded spliced synth (repeatable); default all")
    ap.add_argument("--work-dir", default=str(Path(os.environ.get("TEMP", ".")) / "void_endpoint_bench"))
    ap.add_argument("--sweep", action="store_true",
                    help="score the parameter grid on every corpus at once, to choose a policy")
    args = ap.parse_args()
    want = set(args.corpus) if args.corpus else {"real", "degraded", "spliced", "synth"}
    work = Path(args.work_dir)

    def load_many(paths):
        out = []
        for p in paths:
            fr = frames_of(p)
            if not fr:
                continue
            ref = reference_end_frame(fr)
            if ref is None:
                continue
            out.append((fr, ref))
        return out

    real = load_many(sorted(glob.glob(REAL_DIR + "/*.wav")))
    corpora = {}
    if "real" in want:
        corpora["real (owner's microphone)"] = real
    if "degraded" in want:
        deg = []
        for pat in ("wakeword-training/data/personal_augmented/**/*.wav",
                    "wakeword-training/data/personal_train_augmented/**/*.wav"):
            deg.extend(sorted(glob.glob(pat, recursive=True)))
        corpora["degraded variants"] = load_many(deg)
    spliced_named = []
    if "spliced" in want and real:
        spliced_named = [(int(p.stem.split("_")[1].rstrip("ms")), p)
                         for p in build_spliced(real, work / "spliced")]
    if "synth" in want:
        synth = build_synth(work / "synth")
        corpora["synth (the command list)"] = load_many([p for _, p in synth])
        corpora["_synth_named"] = [(cid, p) for cid, p in synth]

    if args.sweep:
        two = build_two_pause(real, work / "spliced") if real else []

        def longest_survived(items, pol):
            best = 0
            for gap, path in items:
                fr = frames_of(path)
                ref = reference_end_frame(fr) if fr else None
                if ref is None:
                    continue
                fire, _r = run(fr, pol)
                if fire is not None and fire >= ref:
                    best = max(best, gap)
            return best

        print("\n" + "=" * 112)
        print("PARAMETER SWEEP - a configuration is only acceptable if it truncates NOTHING the baseline keeps")
        print("%-26s %8s %8s %6s %6s %6s %8s %8s" % ("configuration", "real p50", "real p90", "cut:R",
                                                     "cut:D", "cut:S", "gap 1x", "gap 2x"))
        print("-" * 112)
        rows = [("FIXED 0.60 (baseline)", policy(0.60))] + sweep_configs()
        for label, pol in rows:
            lat_r, cut_r, _ = score(corpora.get("real (owner's microphone)", []), pol)
            _lat_d, cut_d, _ = score(corpora.get("degraded variants", []), pol)
            _lat_s, cut_s, _ = score(corpora.get("synth (the command list)", []), pol)
            g1 = longest_survived(spliced_named, pol)
            g2 = longest_survived(two, pol)
            print("%-26s %7.2fs %7.2fs %6d %6d %6d %7dms %7dms"
                  % (label, q(lat_r, 0.5), q(lat_r, 0.9), cut_r, cut_d, cut_s, g1, g2))
        return

    for name, clips in corpora.items():
        if name.startswith("_"):
            continue
        print("\n" + "=" * 96)
        print("%s   n=%d" % (name, len(clips)))
        print("%-44s %8s %8s %8s %8s %11s %11s" % ("configuration", "p50", "p90", "p95", "max",
                                                   "truncated", "never fired"))
        print("-" * 96)
        for label, pol in CONFIGS:
            lat, truncated, never = score(clips, pol)
            flag = "  <-- CUTS" if truncated else ""
            print("%-44s %7.2fs %7.2fs %7.2fs %7.2fs %11s %11d%s"
                  % (label, q(lat, 0.5), q(lat, 0.9), q(lat, 0.95), max(lat) if lat else float("nan"),
                     truncated, never, flag))

    if spliced_named:
        # Reported PER GAP, not as a truncation count: a pause longer than the budget is SUPPOSED to end the
        # capture - that is what endpointing is. What matters is the longest mid-utterance pause each configuration
        # survives, and that is a property of the gap length, not an average over a mixed corpus.
        print("\n" + "=" * 96)
        print("spliced: real speech either side of a REAL room-noise gap of a known length")
        print("survived = the capture stayed open through the pause and ended after the second utterance\n")
        head = "%-44s" % "configuration"
        for gap, _ in spliced_named:
            head += "%6d" % gap
        print(head + "   (gap ms)")
        print("-" * 96)
        for label, pol in CONFIGS:
            row = "%-44s" % label
            for _gap, path in spliced_named:
                fr = frames_of(path)
                ref = reference_end_frame(fr) if fr else None
                if ref is None:
                    row += "%6s" % "?"
                    continue
                fire, reason = run(fr, pol)
                row += "%6s" % ("ok" if (fire is not None and fire >= ref) else "cut")
            print(row)

    if spliced_named and real:
        two = build_two_pause(real, work / "spliced")
        print("\n" + "=" * 96)
        print("two pauses: a 250 ms hesitation first, then a second gap of varying length")
        print("this is what the pause latch is for - a speaker who hesitated once is likely to hesitate again\n")
        head = "%-44s" % "configuration"
        for gap, _ in two:
            head += "%6d" % gap
        print(head + "   (second gap, ms)")
        print("-" * 96)
        for label, pol in CONFIGS:
            row = "%-44s" % label
            for _gap, path in two:
                fr = frames_of(path)
                ref = reference_end_frame(fr) if fr else None
                if ref is None:
                    row += "%6s" % "?"
                    continue
                fire, _reason = run(fr, pol)
                row += "%6s" % ("ok" if (fire is not None and fire >= ref) else "cut")
            print(row)

    named = corpora.get("_synth_named") or []
    if named:
        print("\n" + "=" * 96)
        print("per phrase, baseline against the chosen adaptive policy")
        print("%-18s %6s %10s %10s %9s  %s" % ("phrase", "audio", "fixed 0.60", "adaptive", "saved", "cut?"))
        print("-" * 96)
        base, chosen = policy(0.60), policy(0.60, fast=0.40, after=0.30, evidence=0.20)
        for cid, path in named:
            fr = frames_of(path)
            ref = reference_end_frame(fr) if fr else None
            if ref is None:
                continue
            fb, _ = run(fr, base)
            fa, _ = run(fr, chosen)
            lb, la = (fb - ref) * FRAME_S, (fa - ref) * FRAME_S
            print("%-18s %5.2fs %9.2fs %9.2fs %8.0fms  %s"
                  % (cid, len(fr) * FRAME_S, lb, la, (lb - la) * 1000, "TRUNCATED" if fa < ref else ""))


if __name__ == "__main__":
    main()
