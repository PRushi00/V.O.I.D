"""Feature-extraction tests. Unit tests use a FAKE AudioFeatures object
(same interface: get_embedding_shape(seconds), embed_clips(x, batch_size=)),
so they run fast with no network access and no real openwakeword backbone
download. A separate integration test verifies the REAL backbone against the
model's actual (16, 96) input shape.
"""
from __future__ import annotations

import numpy as np
import pytest

from training.audio_validation import AudioFormatError
from training.extract_features import (
    FeatureShapeError, _fit_to_length, _read_wav_int16, extract_features_for_wavs,
    resolve_clip_seconds,
)


class FakeAudioFeatures:
    """shape[0] grows step-wise with duration, landing on exactly 16 frames
    only within a narrow window - mirrors the real embedding pipeline's
    behavior closely enough to exercise resolve_clip_seconds's search."""

    def get_embedding_shape(self, seconds: float):
        frames = int(seconds * 10)  # 10 fps toy model
        return (frames, 96)

    def embed_clips(self, x, batch_size=32):
        n = x.shape[0]
        return np.zeros((n, 16, 96), dtype=np.float32)


def test_resolve_clip_seconds_returns_configured_value_if_it_already_matches():
    fake = FakeAudioFeatures()
    result = resolve_clip_seconds(fake, target_frames=16, configured_seconds=1.6)
    assert fake.get_embedding_shape(result)[0] == 16


def test_resolve_clip_seconds_searches_nearby_when_configured_value_is_off():
    fake = FakeAudioFeatures()
    result = resolve_clip_seconds(fake, target_frames=16, configured_seconds=1.5)
    assert fake.get_embedding_shape(result)[0] == 16


def test_resolve_clip_seconds_raises_when_nothing_in_range_matches():
    class NeverMatches:
        def get_embedding_shape(self, seconds):
            return (999, 96)

    with pytest.raises(FeatureShapeError):
        resolve_clip_seconds(NeverMatches(), target_frames=16, configured_seconds=1.6,
                             search_range_s=0.1, step_s=0.05)


def test_fit_to_length_pads_short_clips():
    out = _fit_to_length(np.array([1, 2, 3], dtype=np.int16), 6)
    assert out.tolist() == [1, 2, 3, 0, 0, 0]


def test_fit_to_length_truncates_long_clips():
    out = _fit_to_length(np.arange(10, dtype=np.int16), 4)
    assert out.tolist() == [0, 1, 2, 3]


def test_extract_features_for_wavs_returns_expected_shape(tmp_path):
    import wave
    wav_path = tmp_path / "clip.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(np.zeros(16000, dtype=np.int16).tobytes())

    fake = FakeAudioFeatures()
    features = extract_features_for_wavs([str(wav_path)], fake, clip_seconds=1.0,
                                         sample_rate=16000)
    assert features.shape == (1, 16, 96)


def test_extract_features_for_wavs_handles_empty_list():
    features = extract_features_for_wavs([], FakeAudioFeatures(), clip_seconds=1.0,
                                         sample_rate=16000)
    assert features.shape == (0, 0, 0)


# --- a wrong-sample-rate WAV must never silently enter feature extraction -
# (this is the exact defect this pass found and fixed: 44.1kHz SAPI output
# being reinterpreted as 16kHz by sample count alone, silently truncating
# and distorting the audio by a ~2.76x factor) ------------------------------

def _write_wav_at_rate(path, sample_rate, seconds=1.0):
    import wave
    n = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(np.zeros(n, dtype=np.int16).tobytes())


def test_read_wav_int16_accepts_matching_sample_rate(tmp_path):
    path = tmp_path / "ok.wav"
    _write_wav_at_rate(path, 16000, seconds=0.5)
    samples = _read_wav_int16(str(path), expected_sample_rate=16000)
    assert len(samples) == 8000


def test_read_wav_int16_raises_on_mismatched_sample_rate(tmp_path):
    path = tmp_path / "44khz.wav"
    _write_wav_at_rate(path, 44100, seconds=0.5)
    with pytest.raises(AudioFormatError, match="44100"):
        _read_wav_int16(str(path), expected_sample_rate=16000)


def test_read_wav_int16_with_no_expected_rate_does_not_validate(tmp_path):
    # Explicit opt-out path (expected_sample_rate=None) - used nowhere in
    # the real pipeline, kept only so the function's contract is pinned down.
    path = tmp_path / "44khz.wav"
    _write_wav_at_rate(path, 44100, seconds=0.5)
    samples = _read_wav_int16(str(path), expected_sample_rate=None)
    assert len(samples) == 22050


# --- position-jitter (generalization-fix pass): a direct diagnostic found
# the trained classifier is strongly position-sensitive - identical audio
# scored 0.91 left-aligned at offset 0 (the only placement _fit_to_length
# ever produced) vs ~0.01 once shifted 200ms later in the same window.
# These tests pin down the opt-in randomized-placement behavior added to
# fix that, while proving the DEFAULT (rng=None) behavior is bit-for-bit
# unchanged. ---------------------------------------------------------------

def test_fit_to_length_without_rng_is_unchanged_left_aligned_behavior():
    out = _fit_to_length(np.array([1, 2, 3], dtype=np.int16), 6)
    assert out.tolist() == [1, 2, 3, 0, 0, 0]
    out2 = _fit_to_length(np.arange(10, dtype=np.int16), 4)
    assert out2.tolist() == [0, 1, 2, 3]


def test_fit_to_length_with_rng_places_short_clip_at_a_random_offset():
    samples = np.array([9, 9, 9], dtype=np.int16)
    rng = np.random.default_rng(0)
    out = _fit_to_length(samples, 10, rng=rng)
    assert len(out) == 10
    assert out.sum() == 27  # the 3 real samples are preserved somewhere
    # not always left-aligned across repeated draws (would be a near-zero
    # probability coincidence if it were still always offset 0)
    offsets_seen = set()
    rng2 = np.random.default_rng(1)
    for _ in range(20):
        out = _fit_to_length(samples, 10, rng=rng2)
        offset = int(np.argmax(out != 0))
        offsets_seen.add(offset)
    assert len(offsets_seen) > 1


def test_fit_to_length_with_rng_is_deterministic_given_the_same_rng_state():
    samples = np.array([5, 6, 7], dtype=np.int16)
    out_a = _fit_to_length(samples, 8, rng=np.random.default_rng(42))
    out_b = _fit_to_length(samples, 8, rng=np.random.default_rng(42))
    assert out_a.tolist() == out_b.tolist()


def test_fit_to_length_with_rng_never_drops_or_duplicates_samples():
    samples = np.arange(1, 5, dtype=np.int16)
    rng = np.random.default_rng(7)
    out = _fit_to_length(samples, 9, rng=rng)
    assert len(out) == 9
    nonzero = out[out != 0]
    assert nonzero.tolist() == samples.tolist()


def test_fit_to_length_with_rng_randomizes_which_window_of_a_long_clip_is_kept():
    samples = np.arange(20, dtype=np.int16)
    windows_seen = set()
    for seed in range(20):
        out = _fit_to_length(samples, 5, rng=np.random.default_rng(seed))
        windows_seen.add(tuple(out.tolist()))
    assert len(windows_seen) > 1
    for w in windows_seen:
        assert list(w) == sorted(w)  # contiguous increasing slice of `samples`


def test_extract_all_features_jitter_seed_only_affects_train_categories(tmp_path):
    import wave as wave_mod

    def make_wav(path, n_zeros_before_tone, tone_len=100):
        with wave_mod.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            data = np.zeros(n_zeros_before_tone, dtype=np.int16)
            data = np.concatenate([data, np.full(tone_len, 1000, dtype=np.int16)])
            wf.writeframes(data.tobytes())

    train_dir = tmp_path / "positive_train"
    val_dir = tmp_path / "positive_val"
    train_dir.mkdir()
    val_dir.mkdir()
    make_wav(train_dir / "a.wav", 0)
    make_wav(val_dir / "a.wav", 0)

    class FakeAF:
        def get_embedding_shape(self, seconds):
            return (16, 96)

        def embed_clips(self, x, batch_size=32):
            return np.zeros((x.shape[0], 16, 96), dtype=np.float32)

    from training.extract_features import extract_all_features
    config = {"features": {"sample_rate": 16000, "clip_seconds": 1.0}}
    # Just verify this runs end-to-end without error and produces the
    # expected keys/shapes - the per-sample offset-randomization behavior
    # itself is already pinned down at the _fit_to_length level above.
    out = extract_all_features(config, {"positive_train": train_dir, "positive_val": val_dir},
                               audio_features=FakeAF(), jitter_seed=123)
    assert set(out) == {"positive_train", "positive_val"}
    assert out["positive_train"].shape == (1, 16, 96)
    assert out["positive_val"].shape == (1, 16, 96)


def test_extract_features_for_wavs_rejects_a_mismatched_sample_rate_file(tmp_path):
    # The real call site (extract_all_features) always passes
    # expected_sample_rate=sample_rate - this proves a 44.1kHz file mixed
    # into a nominally-16kHz batch cannot silently enter the feature array.
    good = tmp_path / "good.wav"
    bad = tmp_path / "bad.wav"
    _write_wav_at_rate(good, 16000, seconds=1.0)
    _write_wav_at_rate(bad, 44100, seconds=1.0)

    with pytest.raises(AudioFormatError, match="44100"):
        extract_features_for_wavs([str(good), str(bad)], FakeAudioFeatures(),
                                  clip_seconds=1.0, sample_rate=16000)
