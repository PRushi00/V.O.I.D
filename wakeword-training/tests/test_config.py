from __future__ import annotations

import pytest

from training.config import ConfigError, load_config


def _write(tmp_path, text):
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_raises_config_error(tmp_path):
    p = _write(tmp_path, "model_name: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(p)


def test_non_mapping_yaml_raises_config_error(tmp_path):
    p = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(p)


def test_missing_top_level_key_raises_config_error(tmp_path):
    p = _write(tmp_path, "model_name: x\n")
    with pytest.raises(ConfigError, match="missing required key"):
        load_config(p)


def test_missing_section_raises_config_error(tmp_path):
    p = _write(tmp_path, """
model_name: x
target_phrase: "hey void"
output_dir: models
data_dir: data
seed: 1
""")
    with pytest.raises(ConfigError, match="missing or malformed section"):
        load_config(p)


def test_unknown_tts_backend_value(tmp_path, minimal_config_yaml):
    text = minimal_config_yaml.replace('tts_backend: "sapi"', 'tts_backend: "espeak"', 1)
    p = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="tts_backend"):
        load_config(p)


def test_piper_backend_without_path_raises_config_error(tmp_path, minimal_config_yaml):
    text = minimal_config_yaml.replace('tts_backend: "sapi"', 'tts_backend: "piper"', 1)
    p = _write(tmp_path, text)
    with pytest.raises(ConfigError, match="piper_sample_generator_path"):
        load_config(p)


def test_valid_config_loads(tmp_path, minimal_config_yaml):
    p = _write(tmp_path, minimal_config_yaml)
    cfg = load_config(p)
    assert cfg["model_name"] == "hey_void_test"
    assert cfg["target_phrase"] == "hey void"


@pytest.fixture
def minimal_config_yaml():
    return """
model_name: hey_void_test
target_phrase: "hey void"
output_dir: models
data_dir: data
seed: 42

positive:
  tts_backend: "sapi"
  n_samples: 4
  n_samples_val: 2

negative:
  tts_backend: "sapi"
  n_adversarial_phrases: 3
  n_adversarial_samples: 4
  n_noise_samples: 4
  n_neutral_speech_samples: 2

augmentation:
  enabled: true
  variants_per_clip: 1

features:
  sample_rate: 16000
  clip_seconds: 2.1

model:
  model_type: dnn
  layer_dim: 8
  n_blocks: 1
  batch_size: 4
  training_steps: 5
  learning_rate: 0.01

evaluation:
  threshold: 0.5
  target_fp_per_hour: 0.5
"""
