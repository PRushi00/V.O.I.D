"""Pluggable text-to-speech backends for synthetic positive/negative samples.

Two backends, selected by config (`tts_backend: "sapi" | "piper"`):

- SapiSampleGenerator: Windows SAPI via pywin32. Already installable with no
  extra downloads, so the development config can run this pipeline
  end-to-end today. Acoustic diversity is lower than a real TTS corpus.

- PiperSampleGenerator: mirrors openwakeword's OWN official train.py usage
  exactly (see openwakeword/train.py's `__main__` block, which imports
  `generate_samples` from an external `piper_sample_generator_path` checkout
  of github.com/rhasspy/piper-sample-generator). This is the intended
  production-quality path, but requires that external repo and Piper voice
  models to be present locally - neither is installed by this project.
  Calling it without that setup raises a clear TTSBackendError, never a
  silent fallback or a fabricated result.

Both backends expose the same small interface:
    generate(texts: list[str], output_dir, n_samples, file_names=None) -> list[Path]
"""
from __future__ import annotations

import os
import random
import sys
import uuid
import wave
from pathlib import Path


class TTSBackendError(RuntimeError):
    """A TTS backend could not run (missing dependency, missing external
    asset, or a backend failure) - raised instead of silently producing
    nothing or substituting a different backend."""


# --- SAPI (Windows, no extra assets) ---------------------------------------

# SAPI's SpeechAudioFormatType.SAFT16kHz16BitMono and
# SpFileStream's SpeechStreamFileMode.SSFMCreateForWrite - hardcoded because
# late-bound win32com.client.Dispatch() does not expose named constants
# without a registered/generated type library.
#
# CORRECTED VALUE (empirically verified, see the 16kHz-fix engineering pass):
# this constant was previously 34, which is actually SAFT44kHz16BitMono, not
# SAFT16kHz16BitMono - confirmed by directly probing SpFileStream.Format.Type
# across the documented SpeechAudioFormatType range and reading back the
# actual WAV header produced for each value. Value 34 produced genuine
# 44,100 Hz mono 16-bit output; value 18 produced genuine 16,000 Hz mono
# 16-bit output. This was the root cause of every SAPI-generated
# positive/adversarial/speech WAV being written at 44.1kHz while the entire
# training pipeline (extract_features.py's sample-count-based length
# fitting) assumed 16kHz - silently truncating audio to under half its real
# duration and distorting its effective time/frequency content by a factor
# of ~2.76x. Not a resampling problem: SAPI can natively produce correct
# 16kHz mono 16-bit output when given the correct enum value.
_SAFT_16KHZ_16BIT_MONO = 18
_SSFM_CREATE_FOR_WRITE = 3


def voice_tokens_for(voices: list[str] | None, count: int) -> list[str | None]:
    """Deterministic round-robin voice assignment for `count` samples given a
    configured voice list (or None per sample if no voices configured). Pure
    function extracted so callers outside the backend (metadata/lineage
    recording in generate_negative.py/generate_positive.py) can independently
    recompute exactly which voice a given sample index received, without
    duplicating this logic or depending on SAPI-specific internals."""
    if not voices:
        return [None] * count
    return [voices[i % len(voices)] for i in range(count)]


class SapiSampleGenerator:
    """Generates WAV clips via Windows SAPI (pywin32). Real, actual speech
    audio, produced locally with zero additional downloads - the deliberate
    development-scale backend so the pipeline is runnable today."""

    def __init__(self, voices: list[str] | None = None, sample_rate: int = 16000):
        if sample_rate != 16000:
            raise TTSBackendError(
                "SapiSampleGenerator only supports 16 kHz output (the "
                "openWakeWord feature pipeline's expected input rate)")
        self.voices = voices or []

    def _voice_tokens(self, count: int) -> list[str | None]:
        """Cycle through configured voice-name substrings (or None = default
        voice) so successive samples get some speaker variety."""
        return voice_tokens_for(self.voices, count)

    def _rates(self, count: int) -> list[int]:
        # SAPI Rate range is -10..10. A small spread gives some speed
        # diversity, deterministically derived from a local Random instance
        # seeded by the caller (see generate_positive.py/generate_negative.py)
        # rather than the global random module, so this stays reproducible
        # without affecting unrelated code that also uses `random`.
        rng = random.Random(1234)
        return [rng.choice([-1, 0, 0, 1]) for _ in range(count)]

    def generate(self, texts: list[str], output_dir: str | Path, n_samples: int,
                file_names: list[str] | None = None) -> list[Path]:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise TTSBackendError(
                "SAPI backend requires pywin32 (pip install pywin32)") from exc

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        names = file_names or [f"{uuid.uuid4().hex}.wav" for _ in range(n_samples)]
        if len(names) < n_samples:
            names = names + [f"{uuid.uuid4().hex}.wav" for _ in range(n_samples - len(names))]

        voice_tokens = self._voice_tokens(n_samples)
        rates = self._rates(n_samples)
        rng = random.Random(5678)
        written: list[Path] = []

        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            available_voices = speaker.GetVoices()
            for i in range(n_samples):
                text = texts[i % len(texts)] if texts else ""
                if not text:
                    continue
                out_path = output_dir / names[i]

                token = voice_tokens[i]
                if token:
                    for v_ndx in range(available_voices.Count):
                        candidate = available_voices.Item(v_ndx)
                        if token.lower() in candidate.GetDescription().lower():
                            speaker.Voice = candidate
                            break
                speaker.Rate = rates[i] + rng.choice([-1, 0, 1])

                stream = win32com.client.Dispatch("SAPI.SpFileStream")
                stream.Format.Type = _SAFT_16KHZ_16BIT_MONO
                stream.Open(str(out_path), _SSFM_CREATE_FOR_WRITE, False)
                try:
                    speaker.AudioOutputStream = stream
                    speaker.Speak(text)
                finally:
                    stream.Close()

                # Fail loudly rather than silently writing (or leaving on
                # disk) a WAV whose actual format doesn't match what every
                # downstream stage assumes - this is the exact invariant
                # that was previously violated undetected (see the
                # _SAFT_16KHZ_16BIT_MONO correction above).
                from training.audio_validation import validate_wav_format
                validate_wav_format(out_path)

                written.append(out_path)
        finally:
            pythoncom.CoUninitialize()

        return written


# --- Piper (production quality, external assets required) -----------------

class PiperSampleGenerator:
    """Mirrors openwakeword's own train.py Piper invocation exactly (same
    generate_samples() kwargs). Requires an external, not-bundled checkout of
    github.com/rhasspy/piper-sample-generator plus Piper voice models -
    neither is installed by this project. Raises TTSBackendError with an
    actionable message if that setup is missing, rather than silently doing
    nothing or falling back to a different backend."""

    def __init__(self, sample_generator_path: str, voices: list[str] | None = None):
        self._path = sample_generator_path
        self.voices = voices or []

    def generate(self, texts: list[str], output_dir: str | Path, n_samples: int,
                file_names: list[str] | None = None,
                batch_size: int = 50) -> list[Path]:
        if not self._path or not os.path.isdir(self._path):
            raise TTSBackendError(
                f"positive.piper_sample_generator_path {self._path!r} does not "
                f"exist. Clone https://github.com/rhasspy/piper-sample-generator "
                f"there and configure positive.piper_voices to use the 'piper' "
                f"backend; the 'sapi' backend works with no extra setup.")
        sys.path.insert(0, os.path.abspath(self._path))
        try:
            from generate_samples import generate_samples  # type: ignore
        except ImportError as exc:
            raise TTSBackendError(
                f"could not import generate_samples from "
                f"{self._path!r}: {exc}") from exc

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        names = file_names or [f"{uuid.uuid4().hex}.wav" for _ in range(n_samples)]

        # Exactly openwakeword's own train.py call shape (positive clip
        # generation branch) - same kwargs, same noise/length-scale spread.
        generate_samples(
            text=texts, max_samples=n_samples, batch_size=batch_size,
            noise_scales=[0.98], noise_scale_ws=[0.98],
            length_scales=[0.75, 1.0, 1.25],
            output_dir=str(output_dir), auto_reduce_batch_size=True,
            file_names=names,
        )
        return [output_dir / n for n in names]


def get_tts_backend(name: str, *, voices=None, sample_rate: int = 16000,
                    piper_sample_generator_path: str | None = None):
    """Factory: returns the configured backend. Unknown names raise
    immediately rather than silently defaulting to one or the other."""
    if name == "sapi":
        return SapiSampleGenerator(voices=voices, sample_rate=sample_rate)
    if name == "piper":
        return PiperSampleGenerator(piper_sample_generator_path or "", voices=voices)
    raise TTSBackendError(f"unknown tts_backend {name!r}; expected 'sapi' or 'piper'")


def wav_duration_seconds(path: str | Path) -> float:
    """Small helper used by tests/evaluation - avoids depending on a heavier
    audio library just to sanity-check a generated clip's length."""
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())
