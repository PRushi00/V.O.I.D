"""Positive sample generation: synthetic speech of the target wake phrase.

Phonetic verification (per project requirement - do not silently assume the
text fed to TTS says what we think it says): the target phrase is read from
config as plain text ("hey void"), never as "V.O.I.D." with periods. Every
TTS engine (SAPI, Piper) is handed that same plain-English string, so the
output is deterministically the ordinary word "void" - not spelled-out
letters, not an engine-specific acronym-expansion guess. See README.md for
the full reasoning; a human listening check of a generated sample remains a
recommended follow-up (this module cannot verify pronunciation acoustically).
"""
from __future__ import annotations

from pathlib import Path

from training.tts_backends import get_tts_backend


def generate_positive_samples(config: dict, data_dir: str | Path) -> dict[str, Path]:
    """Generates positive_train/ and positive_val/ WAV directories.

    Returns {"train": Path, "val": Path}. Idempotent-ish: does not delete
    existing files, but will top up to the configured count only if the
    directory currently holds noticeably fewer files than requested (mirrors
    openwakeword's own train.py behavior of skipping regeneration when
    ~enough samples already exist).
    """
    pos_cfg = config["positive"]
    phrase = config["target_phrase"]
    backend = get_tts_backend(
        pos_cfg["tts_backend"],
        voices=pos_cfg.get("voices") or None,
        sample_rate=config["features"]["sample_rate"],
        piper_sample_generator_path=pos_cfg.get("piper_sample_generator_path"),
    )

    data_dir = Path(data_dir)
    train_dir = data_dir / "positive" / "train"
    val_dir = data_dir / "positive" / "val"

    _fill_dir(backend, [phrase], train_dir, pos_cfg["n_samples"])
    _fill_dir(backend, [phrase], val_dir, pos_cfg["n_samples_val"])

    return {"train": train_dir, "val": val_dir}


def _fill_dir(backend, texts: list[str], target_dir: Path, n_samples: int) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    existing = len(list(target_dir.glob("*.wav")))
    if existing >= int(0.95 * n_samples):
        return
    backend.generate(texts, target_dir, n_samples - existing)
