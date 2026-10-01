# "Hey V.O.I.D." wake-word training pipeline

A standalone, reproducible pipeline that trains a custom openWakeWord model
for the phrase **"Hey V.O.I.D."** and exports it as `models/hey_void.onnx`,
compatible with V.O.I.D's existing (unmodified) `void/voice/wake.py`
inference path.

This project is deliberately isolated from V.O.I.D itself:

- It lives in its own directory, is never imported by `void/*`, and never
  requires the V.O.I.D runtime to be running.
- It uses its **own virtual environment** (`.venv-training/`, Python 3.12) -
  **never** `C:\V.O.I.D\.venv` (which is Python 3.14 and must not be
  modified).
- It does not touch `void/`, `config/`, `tests/`, `requirements.txt`, or
  `requirements-voice.txt` under `C:\V.O.I.D`.

## Quick start

```bash
# One-time setup (from this directory)
"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe" -m venv .venv-training
.venv-training\Scripts\python.exe -m pip install -r requirements.txt

# Run the whole pipeline at development scale
.venv-training\Scripts\python.exe train_void.py --config config\hey_void.yaml --all
```

The resulting model lands at `models/hey_void.onnx`. To use it, point
`voice.wake_model_path` in V.O.I.D's `config/local_config.yaml` at that file
- a config-only change; **no V.O.I.D code changes are required or
  permitted.**

Individual stages (each can be re-run on its own):

```bash
python train_void.py --config config/hey_void.yaml --generate
python train_void.py --config config/hey_void.yaml --augment
python train_void.py --config config/hey_void.yaml --features
python train_void.py --config config/hey_void.yaml --train
python train_void.py --config config/hey_void.yaml --evaluate
```

## Why Python 3.12, not 3.14

`C:\V.O.I.D\.venv` runs Python 3.14. Several training-stack packages
(`tensorflow-cpu==2.8.1`, part of openWakeWord's own `full` extra) do not
ship wheels past Python 3.10, and other packages in this project's own
`requirements.txt` are far more commonly tested against 3.10-3.12 than 3.14.
Python 3.12 was confirmed present on this machine
(`C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe`) and was
used instead. This also physically enforces the isolation requirement: this
venv cannot accidentally become V.O.I.D's own venv.

## Dependency discipline: what's installed, and why (verified, not assumed)

Naively running `pip install openwakeword[full]` pulls in a large stack most
of which this project doesn't need. Determined by actually reading
`train.py`/`data.py`/`utils.py` in the installed `openwakeword` package and
then verifying by iteratively installing and importing:

- **`openwakeword.train.Model`** (the real DNN classifier architecture) and
  **`openwakeword.data.generate_adversarial_texts`** (real phonetic-negative
  generation) live in modules whose own top-level imports require far more
  than what those two specific things use: `torch_audiomentations`,
  `speechbrain`, `mutagen`, and `acoustics` are all required just to
  **import** `openwakeword.train`/`openwakeword.data`, regardless of which
  function is actually called. This was discovered by trying the import and
  fixing one `ModuleNotFoundError` at a time - not predicted in advance.
- `acoustics` (needed transitively, see above) depends on
  `scipy.special.sph_harm`, removed in SciPy 1.15 (renamed `sph_harm_y`).
  `requirements.txt` pins `scipy<1.15` - a normal compatible-version
  constraint (openWakeWord itself only requires `scipy<2,>=1.3`), not a
  patch to any package's code.
- **`tensorflow-cpu==2.8.1`, `tensorflow-probability`, `onnx`, `onnx-tf`,
  `datasets`, `deep-phonemizer`** (all part of openWakeWord's `full` extra)
  are **not installed**, traced to two specific, unneeded code paths:
  - `tensorflow`/`onnx`/`onnx-tf` are used only inside
    `openwakeword.train.convert_onnx_to_tflite()` - an optional ONNX->TFLite
    converter with function-local imports, never called here, since V.O.I.D
    is ONNX-only on Windows (confirmed in the prior wake-word diagnosis:
    `tflite_runtime` isn't installable on Windows for a modern Python, and
    V.O.I.D's `wake.py` never requests it).
  - `deep-phonemizer` is only needed by `generate_adversarial_texts`'s
    out-of-vocabulary fallback. `training/generate_negative.py` explicitly
    checks (via `pronouncing.phones_for_word`) that every word in
    `target_phrase` is in the CMU pronouncing dictionary before calling it,
    and raises a clear `NegativeDataError` instead of ever silently hitting
    that fallback. Verified: `pronouncing.phones_for_word("void")` ->
    `['V OY1 D']`, `phones_for_word("hey")` -> `['HH EY1']` - both present.
  - `tensorflow-cpu==2.8.1` would not even install on Python 3.14 or 3.12
    (its wheels stop at Python 3.10) - this alone would have blocked a naive
    `openwakeword[full]` install in this project's own venv.

See `requirements.txt` for the exact, verified-working package list.

## Phonetic verification of the target phrase

The project brief explicitly required not silently assuming the text handed
to TTS says what's intended. `config/hey_void.yaml` sets `target_phrase: "hey
void"` (plain English text, no periods) - every TTS backend receives exactly
that string, never "V.O.I.D." with periods (which a TTS engine's
acronym/number normalizer could otherwise read as spelled-out letters).
Verified via `pronouncing.phones_for_word("void")` -> `V OY1 D` - the CMU
phonetic transcription for the ordinary word "void" (rhymes with "avoid"),
confirming the text is read as intended. This project has no way to
literally listen to generated audio; a human listening check of a generated
sample remains a recommended follow-up (see "Known limitations").

## Architecture

```
config/hey_void.yaml
        |
training/generate_positive.py -----+
training/generate_negative.py -----+---> data/{positive,negative}/{train,val}/*.wav
        |  (pluggable TTS backend: sapi (dev, no assets) or piper (production,
        |   mirrors openwakeword's own train.py Piper usage exactly))
        v
training/augment.py  (audiomentations: gain/pitch/noise/bandstop/distortion/reverb)
        v
data/augmented/<category>/*.wav
        v
training/extract_features.py  (REUSES openwakeword.utils.AudioFeatures -
        |                       the real melspectrogram + shared Google
        |                       embedding pipeline - never reinvented)
        v
data/features/<category>.npy   (N, 16, 96) arrays
        v
training/train.py  (REUSES openwakeword.train.Model - the real architecture
        |            and its real export_to_onnx(); a small transparent
        |            PyTorch loop instead of openwakeword's own mmap-batch-
        |            generator fit loop, which is tuned for production-scale
        |            data volumes this dev pipeline doesn't have)
        v
models/hey_void.onnx + hey_void.pt (checkpoint) + hey_void_history.json
        v
training/evaluate.py  (accuracy/recall/FP/FN/threshold sweep/FP-per-hour;
                        ALWAYS reports explicit caveats about dev-scale data)
```

## Positive samples

Synthesized speech of `target_phrase`. Two backends, selected by
`positive.tts_backend`:

- **`sapi`** (default in the dev config): Windows SAPI via `pywin32`. Real,
  actual audio, zero extra downloads - this is what makes `--all` runnable
  today. Lower acoustic diversity than a real multi-voice TTS corpus.
- **`piper`**: mirrors openWakeWord's own official `train.py` mechanism
  exactly (same `generate_samples(...)` call shape, imported from an
  external `positive.piper_sample_generator_path` checkout of
  [rhasspy/piper-sample-generator](https://github.com/rhasspy/piper-sample-generator)).
  **Not installed or cloned by this project** - set that config path and
  `positive.piper_voices` yourself to use it; the pipeline raises a clear
  `TTSBackendError` if you select `piper` without that setup, rather than
  silently falling back to `sapi` or failing unhelpfully.

Sample counts (`positive.n_samples`, `positive.n_samples_val`) are config-
driven, not hardcoded, and the shipped dev config uses a small (tens, not
thousands) count deliberately - see "Known limitations."

## Negative samples

Three categories, each with an **independently generated** (not post-hoc
split) train/val set, so augmentation never leaks a validation clip's
variant into training:

1. **Adversarial phrases** - phonetically similar to "hey void", generated
   via openWakeWord's own `openwakeword.data.generate_adversarial_texts`
   (reused directly, not reimplemented), plus explicit
   `negative.custom_negative_phrases` from config (e.g. "hey voice", "hey
   boy", "hey voided") - spoken via the same TTS backend as positives. The
   actual target phrase is explicitly filtered out of this set.
2. **Synthetic noise** - white and an approximate pink noise (numpy only, no
   license/download concerns, no claim of acoustic precision).
3. **Neutral spoken sentences** - a small, explicit stand-in for "ordinary
   speech" negatives (`negative.neutral_sentences` in config), spoken via the
   same TTS backend.

This is explicitly a development-scale negative set. OpenWakeWord's own
pretrained models are trained against roughly 30,000 hours of curated
negative audio; nothing this pipeline generates is a substitute for that.

## Augmentation

`training/augment.py` uses `audiomentations` (torch-free) directly: `Gain`,
`PitchShift`, `AddGaussianNoise`, `BandStopFilter`, `TanhDistortion`,
`RoomSimulator` (reverb) - covering the requested gain/pitch/noise/EQ/
distortion/reverb effect list without pulling in openWakeWord's own much
heavier `torch-audiomentations`/`speechbrain`-based augmentation machinery
for this project's own augmentation step (those two packages are still
installed, but only because they're needed transitively to *import*
`openwakeword.train`/`openwakeword.data` - see "Dependency discipline"
above). Effects whose class isn't available in the installed
`audiomentations` version are skipped with a logged warning, never silently
pretended to have run.

## Feature representation

**Never reinvented.** `training/extract_features.py` uses
`openwakeword.utils.AudioFeatures` - the exact melspectrogram + frozen
shared Google speech-embedding pipeline openWakeWord's own models (and
V.O.I.D's inference path) use. `training/extract_features.py`'s
`resolve_clip_seconds()` explicitly verifies (via
`AudioFeatures.get_embedding_shape()`, never a hardcoded formula) that the
configured clip length produces exactly 16 embedding frames - matching
`openwakeword.train.Model`'s `input_shape[0]` - searching nearby durations
if the configured value doesn't land exactly, and raising a clear
`FeatureShapeError` if none do. One clip = one training example.

The shared melspectrogram/embedding backbone models are downloaded once via
`openwakeword.utils.download_models()` (openWakeWord's own official, small,
shared assets - not training data) on first use; this needs network access.

## Model architecture

`openwakeword.train.Model` (`model_type: "dnn"`) - reused directly, not
reimplemented: `Flatten -> Linear -> ReLU -> LayerNorm -> [FCN block]*n_blocks
-> Linear -> Sigmoid`, matching every real openWakeWord model's architecture
family. `layer_dim`/`n_blocks`/`batch_size`/`training_steps`/`learning_rate`
are all config-driven (`config/hey_void.yaml`'s `model:` section) - the
shipped dev config uses small values to validate the pipeline quickly.

`training/train.py` writes its own small, transparent PyTorch training loop
around this model rather than reusing `openwakeword`'s own fit loop, which
is built around their `mmap_batch_generator`/production-scale on-disk batch
format - a reasonable boundary for a dev-scale, in-memory dataset, while
still training the identical network.

## ONNX export

`owwmodel.export_to_onnx(path, class_mapping=model_name)` - openWakeWord's
own method (`torch.onnx.export` under the hood), called directly, unmodified.

## How V.O.I.D loads the resulting model

Unchanged, per the prior wake-word diagnosis: `void/voice/wake.py`'s
`OpenWakeWordDetector` calls `Model(wakeword_models=[self._model_path])` with
no `inference_framework` argument, which defaults to `"tflite"`; since
`tflite_runtime` isn't installed on Windows, it falls back to ONNX **because
the path contains `.onnx`**. So: copy or point `voice.wake_model_path` (in
`config/local_config.yaml`) at `models/hey_void.onnx` - a config-only change
made by the V.O.I.D repository owner, not part of this project, and not done
automatically here. No V.O.I.D code changes are required or were made.

## Tests

```bash
.venv-training\Scripts\python.exe -m pytest                  # fast unit suite
.venv-training\Scripts\python.exe -m pytest -m integration    # + real backbone/model checks
```

Most tests use tiny synthetic fixtures (a handful of silent WAV clips,
random `(16, 96)` feature arrays) and fake TTS/feature backends - no real
dataset, no real TTS engine call, no network. Tests that need the real
openWakeWord feature backbone (one-time download) or V.O.I.D's own venv are
marked `integration` and skipped by default.

`tests/test_void_compatibility.py` is the project-required V.O.I.D
compatibility proof (see brief section 12): it shells out to
`C:\V.O.I.D\.venv\Scripts\python.exe` and asks **V.O.I.D's own,
already-installed** `openwakeword.model.Model` - not this project's copy -
to load and score `models/hey_void.onnx`, exactly as `void/voice/wake.py`
does. It skips cleanly if the model doesn't exist yet or that interpreter
isn't found; it never modifies any V.O.I.D file.

## Known limitations

- **This is a development-scale pipeline proof, not a production model.** A
  dev-scale run (tens of positive/negative samples, hundreds of training
  steps) proves every stage of the pipeline works end-to-end and produces a
  loadable, scorable ONNX model - it does not prove the model reliably
  detects "Hey V.O.I.D." or rejects everything else in real use.
- SAPI-generated speech has far less acoustic/speaker diversity than a real
  multi-voice TTS corpus (Piper) or real recordings.
- The synthetic noise and neutral-sentence negatives are a small, synthetic
  stand-in for the tens-of-thousands-of-hours negative corpus a production
  wake-word model needs; the false-positives-per-hour figure in the
  evaluation report is not meaningful at this data scale, and the report
  says so explicitly.
- Piper-based generation requires external assets (a `piper-sample-generator`
  checkout, Piper voice models) not present on this machine; wired correctly
  but not exercised in this session.
- No human has listened to a generated sample to confirm pronunciation
  matches expectations; only the text-level phonetic check was performed.
- `RoomSimulator` (reverb) and other `audiomentations` transforms depend on
  the installed `audiomentations` version; if unavailable, they're skipped
  with a warning rather than silently omitted without notice - check logs.

## What this is explicitly NOT

Not a new assistant, LLM, voice runtime, or wake-word inference engine; no
TFLite support; no cloud training service; no GUI/dashboard; no automatic
microphone recording; no production packaging. Purely: a reproducible custom
wake-word training pipeline for V.O.I.D's existing, unmodified voice stack.
