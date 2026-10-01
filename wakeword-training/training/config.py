"""Configuration loading for the wake-word training pipeline.

A thin, explicit loader - no hidden defaults for anything that affects what
gets trained or exported. Missing/malformed required fields raise
ConfigError immediately (fail closed), rather than silently substituting a
guess, since a silently-wrong target phrase or sample count would waste an
entire training run.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """The training config is missing a required field or is malformed."""


# Top-level sections every config must have, and the required keys within
# each. This is intentionally a flat, readable list rather than a schema
# library - the config is small enough that this stays clear.
_REQUIRED = {
    "": ["model_name", "target_phrase", "output_dir", "data_dir", "seed"],
    "positive": ["tts_backend", "n_samples", "n_samples_val"],
    "negative": ["tts_backend", "n_adversarial_phrases", "n_adversarial_samples",
                "n_noise_samples", "n_neutral_speech_samples"],
    "augmentation": ["enabled", "variants_per_clip"],
    "features": ["sample_rate", "clip_seconds"],
    "model": ["model_type", "layer_dim", "n_blocks", "batch_size",
              "training_steps", "learning_rate"],
    "evaluation": ["threshold", "target_fp_per_hour"],
}

_VALID_TTS_BACKENDS = {"sapi", "piper"}


def _get_section(data: dict, section: str, config_path: str) -> dict:
    if section == "":
        return data
    value = data.get(section)
    if not isinstance(value, dict):
        raise ConfigError(
            f"{config_path}: missing or malformed section {section!r}")
    return value


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load and validate a training config. Raises ConfigError on any
    missing required field, unknown tts_backend, or non-existent path."""
    config_path = str(config_path)
    if not os.path.isfile(config_path):
        raise ConfigError(f"config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{config_path}: invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: top-level YAML must be a mapping")

    for section, keys in _REQUIRED.items():
        node = _get_section(data, section, config_path)
        missing = [k for k in keys if k not in node]
        if missing:
            where = section or "top level"
            raise ConfigError(
                f"{config_path}: missing required key(s) {missing} in {where!r}")

    for section in ("positive", "negative"):
        backend = data[section]["tts_backend"]
        if backend not in _VALID_TTS_BACKENDS:
            raise ConfigError(
                f"{config_path}: {section}.tts_backend must be one of "
                f"{sorted(_VALID_TTS_BACKENDS)}, got {backend!r}")

    if data["positive"]["tts_backend"] == "piper" and \
            not data["positive"].get("piper_sample_generator_path"):
        raise ConfigError(
            f"{config_path}: positive.tts_backend is 'piper' but "
            f"positive.piper_sample_generator_path is not set")

    return data


def resolve_path(config: dict, *parts: str) -> Path:
    """Resolve a data/model/output path relative to the config's own
    directory-bearing keys (output_dir/data_dir), always as an absolute
    Path, so callers never depend on the current working directory."""
    base = Path(parts[0])
    for p in parts[1:]:
        base = base / p
    return base.resolve()
