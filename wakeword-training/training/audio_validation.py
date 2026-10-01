"""Deterministic WAV audio-contract validation.

The final audio representation entering the wake-word feature/model pipeline
must be 16,000 Hz, mono, 16-bit PCM. This module exists because that
invariant was previously assumed, never checked - a SAPI configuration
defect (see training/tts_backends.py's corrected _SAFT_16KHZ_16BIT_MONO
constant) caused every SAPI-generated positive/adversarial/speech WAV to be
written at 44,100 Hz instead, and nothing in the pipeline noticed: sample
counts were treated as if they were always at the configured rate, silently
truncating audio and distorting its effective time/frequency content.

This module provides the single, shared check used at every point audio
enters or leaves generation/feature-extraction, so a mismatch is caught
immediately and loudly rather than silently propagating: never resample or
reinterpret without saying so.
"""
from __future__ import annotations

import wave
from pathlib import Path

EXPECTED_SAMPLE_RATE = 16000
EXPECTED_CHANNELS = 1
EXPECTED_SAMPLE_WIDTH = 2  # bytes (16-bit)


class AudioFormatError(RuntimeError):
    """A WAV file does not match the required 16 kHz / mono / 16-bit PCM
    contract. Raised instead of silently proceeding with a sample count that
    would be misinterpreted against the wrong rate."""


def read_wav_format(path: str | Path) -> tuple[int, int, int, int]:
    """Returns (sample_rate, channels, sample_width_bytes, n_frames) read
    directly from the WAV header - never assumed from configuration."""
    with wave.open(str(path), "rb") as wf:
        return wf.getframerate(), wf.getnchannels(), wf.getsampwidth(), wf.getnframes()


def validate_wav_format(path: str | Path, *,
                        expected_sample_rate: int = EXPECTED_SAMPLE_RATE,
                        expected_channels: int = EXPECTED_CHANNELS,
                        expected_sample_width: int = EXPECTED_SAMPLE_WIDTH) -> None:
    """Raises AudioFormatError with the exact observed values if `path`'s
    actual WAV header does not match the required contract. Never converts,
    never resamples, never silently accepts - fails loudly per the project's
    explicit invariant: no WAV may be silently interpreted at the wrong
    sample rate."""
    sr, ch, sw, _n = read_wav_format(path)
    problems = []
    if sr != expected_sample_rate:
        problems.append(f"sample_rate={sr} (expected {expected_sample_rate})")
    if ch != expected_channels:
        problems.append(f"channels={ch} (expected {expected_channels})")
    if sw != expected_sample_width:
        problems.append(f"sample_width={sw} bytes (expected {expected_sample_width})")
    if problems:
        raise AudioFormatError(
            f"{path}: does not satisfy the required 16kHz/mono/16-bit PCM "
            f"audio contract - {'; '.join(problems)}")


def scan_directory_audio_format(directory: str | Path, *,
                                expected_sample_rate: int = EXPECTED_SAMPLE_RATE,
                                expected_channels: int = EXPECTED_CHANNELS,
                                expected_sample_width: int = EXPECTED_SAMPLE_WIDTH
                                ) -> dict:
    """Scans every *.wav in `directory` (non-recursive) and returns a summary
    dict: count, sample_rate/channel/sample_width distributions (as
    {value: count}), duration min/max/mean, and a list of files that violate
    the expected contract. Used by the dataset-integrity gate to verify an
    entire generated category before it is allowed into feature extraction.
    Never raises on its own - callers decide whether the summary is
    acceptable (e.g. `invalid_files` must be empty before proceeding)."""
    directory = Path(directory)
    sr_dist: dict[int, int] = {}
    ch_dist: dict[int, int] = {}
    sw_dist: dict[int, int] = {}
    durations: list[float] = []
    invalid_files: list[dict] = []
    count = 0

    for p in sorted(directory.glob("*.wav")):
        count += 1
        sr, ch, sw, n = read_wav_format(p)
        sr_dist[sr] = sr_dist.get(sr, 0) + 1
        ch_dist[ch] = ch_dist.get(ch, 0) + 1
        sw_dist[sw] = sw_dist.get(sw, 0) + 1
        durations.append(n / sr if sr else 0.0)
        if sr != expected_sample_rate or ch != expected_channels or sw != expected_sample_width:
            invalid_files.append({"path": str(p), "sample_rate": sr, "channels": ch,
                                  "sample_width": sw, "n_frames": n})

    return {
        "directory": str(directory),
        "count": count,
        "sample_rate_distribution": sr_dist,
        "channel_distribution": ch_dist,
        "sample_width_distribution": sw_dist,
        "duration_min": min(durations) if durations else None,
        "duration_max": max(durations) if durations else None,
        "duration_mean": (sum(durations) / len(durations)) if durations else None,
        "invalid_files": invalid_files,
        "all_valid": len(invalid_files) == 0 and count > 0,
    }
