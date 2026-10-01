"""Correct runtime-style (predict_clip) evaluation methodology.

A prior diagnostic found that openwakeword.model.Model.predict_clip() pads
its input with `padding` seconds of silence on each side before streaming
it through the real Model.predict() code path in `chunk_size`-sample
chunks, and that many of the highest scores observed against real human
recordings fell after the end of the spoken audio, inside that padding.
The FIRST hypothesis tested (against the pre-fix, 44.1kHz-corrupted
experimental model) was that this was a content-independent transition
artifact - supported at the time by a synthetic-noise control clip that
also scored high in the same region.

Re-investigating against the sample-rate-corrected model (Phase 6/7 of the
16kHz engineering pass) falsified that hypothesis as the SOLE explanation.
openWakeWord's classifier requires a fixed-size window of `clip_seconds`
(empirically 2.06s for this project's model - see
extract_features.resolve_clip_seconds) of CONTIGUOUS audio context to
produce one score; the window covers [t - clip_seconds, t] ending at the
current chunk's time t. For any utterance shorter than clip_seconds (most
real "hey void" utterances ARE shorter - the 23 personal recordings run
1.09-2.09s), the classifier CANNOT have the whole utterance in its
receptive field until enough trailing silence has streamed in after the
utterance ends to complete that window. Direct comparison confirmed this
mechanistically: for positive/adversarial clips >= clip_seconds long, the
score correctly peaks WITHIN the nominal real-audio span; for otherwise
identical clips < clip_seconds long, the *same* content peaks shortly
after the nominal real-audio span ends, and disappears entirely once
padding exceeds clip_seconds. Noise clips (no wake-word-like content) do
NOT show this post-audio peak at any duration - ruling out a pure,
content-independent silence-transition artifact as the general
explanation. (The original noise-control finding likely reflected the
PRE-FIX model's own degenerate, corrupted-training-data behavior, not a
property of predict_clip() or of openWakeWord itself.)

This module therefore partitions per-chunk scores by whether the
classifier's OWN receptive field ([t - clip_seconds, t]) could possibly
contain any real audio, not by whether `t` itself falls inside the file's
nominal duration:
  - content_influenced: chunks whose receptive field overlaps the real
    audio at all (from the first chunk after real audio starts entering
    the window, through `clip_seconds` after the real audio ends). This is
    the only region where the classifier's output can be causally
    influenced by the file's actual audio content - it is the valid
    evidence region for wake-word recognition, whether the peak happens to
    land before or after the file's own nominal duration.
  - silence_only: chunks whose entire receptive field is artificial
    padding, with no possible influence from the file's content (the head
    of leading padding still filling the window's first `clip_seconds`,
    and anything past `real_duration_s + clip_seconds`). Scores here
    reflect a pure-silence input and must never be treated as evidence of
    recognition.

This module does not modify predict_clip()'s implementation (openwakeword's
installed package is never touched) - it calls it unmodified and then
POST-PROCESSES the returned per-chunk scores using this window-aware split.
The legacy nominal-duration-only split is still computed and returned
(as `leading_padding`/`real_audio_nominal`/`trailing_padding`) for
transparency/comparison, but is NOT the basis for any pass/fail judgment.
"""
from __future__ import annotations

import statistics
import wave
from pathlib import Path


def evaluate_wav_via_predict_clip(model, wav_path: str | Path, model_key: str, *,
                                  padding: int = 1, chunk_size: int = 1280,
                                  sample_rate: int = 16000,
                                  clip_seconds: float = 2.06) -> dict:
    """Runs the real, unmodified Model.predict_clip() on `wav_path` and
    splits the returned per-chunk scores using two schemes:

    1. Window-aware (the basis for any recognition judgment):
       - content_influenced: the classifier's receptive field
         ([t - clip_seconds, t]) overlaps the real audio at all.
       - silence_only: the receptive field is pure artificial silence.

    2. Legacy nominal-duration-only (transparency/comparison only):
       - leading_padding / real_audio_nominal / trailing_padding, split by
         whether `t` itself falls inside the file's own header-reported
         duration - this OVER-penalizes utterances shorter than
         `clip_seconds`, since the classifier cannot fire within their
         nominal span even when recognition is genuine. Kept only so old
         and new evaluations remain comparable.
    """
    wav_path = Path(wav_path)
    with wave.open(str(wav_path), "rb") as wf:
        real_n_samples = wf.getnframes()
        actual_sr = wf.getframerate()
    real_duration_s = real_n_samples / actual_sr if actual_sr else 0.0

    predictions = model.predict_clip(str(wav_path), padding=padding, chunk_size=chunk_size)
    scores = [float(p[model_key]) for p in predictions]

    chunk_s = chunk_size / sample_rate
    pad_chunks = padding / chunk_s

    leading: list[tuple[int, float, float]] = []
    real: list[tuple[int, float, float]] = []
    trailing: list[tuple[int, float, float]] = []
    content: list[tuple[int, float, float]] = []
    silence: list[tuple[int, float, float]] = []
    for i, s in enumerate(scores):
        t_in_padded_clip = i * chunk_s
        t_relative_to_real_audio_start = t_in_padded_clip - padding

        # legacy nominal-duration-only split
        if i < pad_chunks:
            leading.append((i, t_in_padded_clip, s))
        elif t_relative_to_real_audio_start > real_duration_s:
            trailing.append((i, t_in_padded_clip, s))
        else:
            real.append((i, t_in_padded_clip, s))

        # window-aware split: does [t - clip_seconds, t] (relative to real
        # audio start) overlap [0, real_duration_s] at all?
        window_start = t_relative_to_real_audio_start - clip_seconds
        window_end = t_relative_to_real_audio_start
        overlaps_real_audio = (window_end > 0) and (window_start < real_duration_s)
        if overlaps_real_audio:
            content.append((i, t_in_padded_clip, s))
        else:
            silence.append((i, t_in_padded_clip, s))

    def region_stats(triples: list[tuple[int, float, float]]) -> dict:
        if not triples:
            return {"n": 0, "max": None, "mean": None, "median": None,
                    "p95": None, "peak_chunk_index": None,
                    "peak_time_s_relative_to_real_audio_start": None}
        vals = [s for _, _, s in triples]
        peak_pos = max(range(len(vals)), key=lambda k: vals[k])
        sorted_vals = sorted(vals)
        p95_idx = max(0, int(round(0.95 * (len(sorted_vals) - 1))))
        return {
            "n": len(vals),
            "max": max(vals),
            "mean": sum(vals) / len(vals),
            "median": statistics.median(vals),
            "p95": sorted_vals[p95_idx],
            "peak_chunk_index": triples[peak_pos][0],
            "peak_time_s_relative_to_real_audio_start": triples[peak_pos][1] - padding,
        }

    return {
        "wav_path": str(wav_path),
        "real_duration_s": real_duration_s,
        "clip_seconds": clip_seconds,
        "n_total_chunks": len(scores),
        "content_influenced": region_stats(content),
        "silence_only": region_stats(silence),
        "leading_padding": region_stats(leading),
        "real_audio_nominal": region_stats(real),
        "trailing_padding": region_stats(trailing),
        "all_scores": scores,
    }


def threshold_crossings(max_score: float | None, thresholds: list[float]) -> dict:
    """Returns {threshold: bool} for whether `max_score` crosses each
    threshold - None (no real-audio chunks) crosses nothing."""
    if max_score is None:
        return {t: False for t in thresholds}
    return {t: max_score >= t for t in thresholds}


def sweep_thresholds(scores: list[float], thresholds: list[float]) -> dict[float, float]:
    """Returns {threshold: fraction_of_scores_at_or_above_threshold} - the
    shared threshold-sweep computation used by every candidate-vs-target
    comparison in the FP-reduction pass (real-human recall and
    adversarial/speech/noise false-accept rate are both "fraction of a
    per-clip max-score population crossing a threshold", just applied to
    positive vs. negative populations respectively). Returns 0.0 for an
    empty `scores` list rather than raising, so an empty category can still
    be reported uniformly instead of crashing a sweep over several
    categories."""
    if not scores:
        return {t: 0.0 for t in thresholds}
    return {t: sum(1 for s in scores if s >= t) / len(scores) for t in thresholds}
