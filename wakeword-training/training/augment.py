"""Audio augmentation for robustness to normal desktop/laptop mic conditions.

Uses `audiomentations` directly rather than reinventing gain/noise/pitch/
reverb/distortion transforms - its transform set maps almost one-to-one onto
what this project needs (Gain, PitchShift, AddGaussianNoise, BandStopFilter,
TanhDistortion, RoomSimulator), and it has no torch/tensorflow dependency, so
it doesn't drag in openwakeword's much heavier `full`-extra augmentation
stack (torch-audiomentations/speechbrain) just for this. Each transform is
applied probabilistically per its own `p=` argument, matching the
probabilities in config/hey_void.yaml.
"""
from __future__ import annotations

import glob
import logging
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def _read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sr


def _write_wav(path: str, samples: np.ndarray, sr: int) -> None:
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def build_augmentation_pipeline(aug_cfg: dict, seed: int):
    """Builds an audiomentations.Compose pipeline from config. Transforms
    whose class isn't available in the installed audiomentations version are
    skipped with a warning rather than failing the whole pipeline - keeps
    this robust to version differences without silently pretending an effect
    ran when it didn't."""
    import audiomentations as AA

    gain_lo, gain_hi = aug_cfg["gain_db_range"]
    pitch_lo, pitch_hi = aug_cfg["pitch_semitone_range"]
    snr_lo, snr_hi = aug_cfg["noise_snr_db_range"]

    transforms = [AA.Gain(min_gain_db=gain_lo, max_gain_db=gain_hi, p=1.0)]
    transforms.append(AA.PitchShift(min_semitones=pitch_lo, max_semitones=pitch_hi, p=0.5))
    transforms.append(AA.AddGaussianNoise(
        min_amplitude=0.001, max_amplitude=0.02,
        p=aug_cfg["add_noise_probability"]))

    if hasattr(AA, "BandStopFilter"):
        transforms.append(AA.BandStopFilter(p=aug_cfg["bandstop_probability"]))
    else:
        logger.warning("audiomentations.BandStopFilter not available; skipping")

    if hasattr(AA, "TanhDistortion"):
        transforms.append(AA.TanhDistortion(p=aug_cfg["distortion_probability"]))
    else:
        logger.warning("audiomentations.TanhDistortion not available; skipping")

    if hasattr(AA, "RoomSimulator"):
        transforms.append(AA.RoomSimulator(p=aug_cfg["reverb_probability"]))
    else:
        logger.warning("audiomentations.RoomSimulator not available; skipping reverb")

    return AA.Compose(transforms)


def augment_directory(input_dir: str | Path, output_dir: str | Path,
                      aug_cfg: dict, seed: int) -> list[Path]:
    """Reads every .wav in input_dir, writes `variants_per_clip` augmented
    copies of each into output_dir (plus the untouched original), returns
    the list of output paths. Deterministic given the same seed and the same
    installed audiomentations version."""
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if not aug_cfg.get("enabled", True):
        for src in sorted(glob.glob(str(input_dir / "*.wav"))):
            samples, sr = _read_wav(src)
            dst = output_dir / Path(src).name
            _write_wav(str(dst), samples, sr)
            written.append(dst)
        return written

    # audiomentations' transforms draw from numpy's global RNG with no seed
    # parameter of their own (confirmed by reading the installed version's
    # source) - `seed` was previously accepted here but never used, so
    # augmentation output was NOT actually reproducible run-to-run despite
    # looking like a seeded pipeline. Seeding numpy's global state
    # immediately before the (deterministically-ordered, sorted-glob) loop
    # makes the whole directory's augmentation reproducible given the same
    # seed and input files, at the same well-known "reseeds the process-wide
    # RNG" cost already accepted elsewhere in this pipeline (see
    # generate_negative.generate_adversarial_negative_texts).
    np.random.seed(seed)
    pipeline = build_augmentation_pipeline(aug_cfg, seed)
    variants = int(aug_cfg["variants_per_clip"])

    for idx, src in enumerate(sorted(glob.glob(str(input_dir / "*.wav")))):
        samples, sr = _read_wav(src)
        stem = Path(src).stem

        original_dst = output_dir / f"{stem}_orig.wav"
        _write_wav(str(original_dst), samples, sr)
        written.append(original_dst)

        for v in range(variants):
            augmented = pipeline(samples=samples, sample_rate=sr)
            dst = output_dir / f"{stem}_aug{v}.wav"
            _write_wav(str(dst), augmented, sr)
            written.append(dst)

    return written
