"""Gen 3 streaming-realistic evaluation: since Gen 3 does not use
openWakeWord's Model class (no predict_clip() to call), this module
implements the equivalent sliding-window simulation directly, using the
SAME window-aware content-influenced region logic validated in Gen 2's
training/runtime_eval.py (a real methodology bug was found and fixed there
- treating ALL post-audio scores as invalid artifacts undercounted genuine
recognition for short utterances; the fix partitions by whether the
classifier's receptive field could possibly contain real audio, not by
nominal file duration).

A real WAV is padded with `padding` seconds of silence on each side, then
a `clip_seconds`-long window is slid across it in `step_seconds`
increments; each window position is scored by encoding it through the
frozen Whisper encoder and the trained Gen3Classifier. This never scores
padding-only windows as detections.
"""
from __future__ import annotations

import statistics
import wave
from pathlib import Path

import numpy as np

from training.whisper_features import encode_waveform, int16_to_float32


def evaluate_wav_streaming(wav_path: str | Path, whisper_model, classifier_net, *,
                           clip_seconds: float = 2.0, padding: float = 1.0,
                           step_seconds: float = 0.2, sample_rate: int = 16000) -> dict:
    import torch

    wav_path = Path(wav_path)
    with wave.open(str(wav_path), "rb") as wf:
        real_n_samples = wf.getnframes()
        actual_sr = wf.getframerate()
        raw = wf.readframes(real_n_samples)
    real_samples = np.frombuffer(raw, dtype=np.int16)
    real_duration_s = real_n_samples / actual_sr if actual_sr else 0.0

    pad_samples = int(padding * sample_rate)
    padded = np.concatenate([np.zeros(pad_samples, dtype=np.int16), real_samples,
                             np.zeros(pad_samples, dtype=np.int16)])
    window_samples = int(clip_seconds * sample_rate)
    step_samples = int(step_seconds * sample_rate)

    n_positions = max(1, (len(padded) - window_samples) // step_samples + 1)
    content: list[tuple[int, float, float]] = []
    silence: list[tuple[int, float, float]] = []

    net = classifier_net
    net.eval()
    for i in range(n_positions):
        start = i * step_samples
        window = padded[start:start + window_samples]
        if len(window) < window_samples:
            window = np.concatenate([window, np.zeros(window_samples - len(window), dtype=np.int16)])
        waveform = int16_to_float32(window)
        feats = encode_waveform(whisper_model, waveform)
        with torch.no_grad():
            logit = net(torch.from_numpy(feats[np.newaxis].astype(np.float32)))
            score = float(torch.sigmoid(logit).item())

        t_end_in_padded = (start + window_samples) / sample_rate
        t_relative_to_real_audio_start = t_end_in_padded - padding
        window_start_rel = t_relative_to_real_audio_start - clip_seconds
        overlaps_real_audio = (t_relative_to_real_audio_start > 0) and (window_start_rel < real_duration_s)
        entry = (i, t_end_in_padded, score)
        (content if overlaps_real_audio else silence).append(entry)

    def region_stats(triples):
        if not triples:
            return {"n": 0, "max": None, "mean": None, "median": None, "p95": None}
        vals = [s for _, _, s in triples]
        sorted_vals = sorted(vals)
        p95_idx = max(0, int(round(0.95 * (len(sorted_vals) - 1))))
        return {"n": len(vals), "max": max(vals), "mean": sum(vals) / len(vals),
                "median": statistics.median(vals), "p95": sorted_vals[p95_idx]}

    return {
        "wav_path": str(wav_path), "real_duration_s": real_duration_s,
        "n_total_windows": n_positions,
        "content_influenced": region_stats(content),
        "silence_only": region_stats(silence),
    }


def sweep_thresholds(scores: list[float], thresholds: list[float]) -> dict[float, float]:
    if not scores:
        return {t: 0.0 for t in thresholds}
    return {t: sum(1 for s in scores if s >= t) / len(scores) for t in thresholds}
