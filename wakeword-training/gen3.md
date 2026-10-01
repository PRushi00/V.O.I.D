# V.O.I.D Wake Word — Gen 3 (Whisper-encoder detector)

Personal-MVP wake word for "Hey V.O.I.D." — optimized for the owner's voice
on the owner's laptop, **not** speaker-independent / product-grade.

## Why Gen 3

Gen 2 used openWakeWord's frozen 96-dim embedding + a small classifier. An
extensive investigation (see `docs/generalization_fix.md`) showed that
representation could not separate "Hey V.O.I.D." from `-oid/-oyd`
near-misses ("void", "lloyd", "hey boy", "hey voice"), plateauing at
~37% adversarial false-positives.

Gen 3 replaces the representation with the **frozen encoder of Whisper-small**
(via faster-whisper / CTranslate2 — already V.O.I.D's STT backend, so no new
model source and the weights are already cached locally). A small
conv + global-max-pool classifier is trained on those encoder features.

- Feature encoder: Whisper-small encoder, frozen, 16 kHz native (no
  resampling), int8 CPU. Output per 2 s window: `(T=101, 768)`.
- Classifier: `Conv1d(768→64, k=5) → ReLU → global-max-pool over time →
  Linear(64→32) → ReLU → Dropout → Linear(32→1)` → sigmoid.
  Global-max-pool makes the head **structurally position-invariant** (asks
  "does the wake pattern appear anywhere in the window", not "at a fixed
  offset") — this permanently closes the Gen 2 position-sensitivity bug.
- Inference is **torch-free**: encoder = CTranslate2, classifier =
  onnxruntime. Both import under Windows Smart App Control in V.O.I.D's
  production venv (verified). Training uses torch (WSL2 + RTX 5070).

## What changed from Gen 2 → Gen 3 Exp02

1. Representation: openWakeWord embedding → frozen Whisper-small encoder.
2. Classifier head: flatten-based → conv + **global max pool** (position
   invariant by construction).
3. Positive data (Exp01 → Exp02): added **speed-perturbation augmentation**
   (Ko et al. 2015) to the positive class — 200 clean SAPI clips ×
   {0.90, 0.95, 1.05, 1.10} = 800 synthetic-speaker variants, unioned with
   the existing 600 → 1400 positives. This broadens the narrow 2-voice
   synthetic positive distribution toward real-voice vocal-tract / F0 space,
   which is what lifted the owner's real recordings.
4. Reproducibility fix: deterministic per-category jitter seed
   (`zlib.crc32`) instead of process-randomized `hash()`.

## Results (streaming eval; 23 genuine owner recordings are EVAL-ONLY)

Operating-point sweep (Exp02), streaming/content-influenced scoring:

| threshold | owner recall | adversarial FP | speech FP | noise FP |
|-----------|--------------|----------------|-----------|----------|
| 0.30 | 95.7% (22/23) | 18.7% | 6.7% | 2.0% |
| **0.34** | **95.7% (22/23)** | **16.7%** | **4.0%** | **2.0%** |
| 0.36 | 91.3% | 16.7% | 4.0% | 2.0% |
| 0.50 | 87.0% | 16.7% | 4.0% | 2.0% |

**Recommended threshold: 0.34.** It strictly dominates higher thresholds —
0.50→0.34 gains recall (87%→95.7%) at zero cost in speech/noise/adversarial
FP. Below 0.34, FP rises with no recall gain.

Gen 2 → Gen 3 Exp02 (both at their best personal operating point):

| metric | Gen2 prod (streaming @0.6) | Gen3 Exp01 @0.50 | Gen3 Exp02 @0.34 |
|--------|----------------------------|------------------|------------------|
| owner recall | ~low* | 91.3% | **95.7%** |
| adversarial FP | 84% | 25.3% | **16.7%** |
| speech FP | 89% | 10.7% | **4.0%** |
| noise FP | 20% | 5.3% | **2.0%** |

\* Gen2 production model false-fires on almost everything through the real
streaming path (see prior session); it is not usable.

Real recordings are sharply bimodal under Exp02: 20/23 score ≥0.83 (17 at
≥0.997). The one persistent miss (`personal_voice__Recording (2)`, ~0.02)
is a hard utterance also nearly missed by Exp01.

## Adapter streaming validation (real detector algorithm, offline)

Driving the actual rolling-buffer + hop-inference + edge-trigger logic with
30 ms broker-style frames at threshold 0.34:
- Owner positives: **22/23 fire** (only the known hard miss does not).
- 2.5 s silence: does not fire (peak 0.001).
- Sampled adversarial clips: do not fire.
- Per-inference latency: **mean ~215 ms, p95 ~390 ms** (WSL2 CPU, int8).

**Latency note:** at a 0.2 s hop this exceeds real-time on CPU. For
production use one of: hop 0.4–0.5 s (≈50% of one core; wake latency
≤ ~0.5 s + window — fine for personal use), or run the encoder on the
laptop GPU (`device="cuda"`). Real-mic testing on the production machine
must confirm real-time behavior and CPU load.

## Dataset

- positive_train 600 → **1400** (with speed-aug), positive_val 150
- adversarial_train 600, adversarial_val 150
- speech_train 300, speech_val 75
- noise_train 600, noise_val 150
- 23 genuine owner recordings: **EVAL ONLY — never in training**
- All audio validated 16 kHz / mono / 16-bit PCM.

Train/val independence: train and val generated separately; positive jitter
+ speed-aug applied to train only; val left-aligned/fixed and unperturbed.

## Reproduction (WSL2)

```bash
cd ~/void-gen3-training/wakeword-training
# 1. build the speed-augmented positive set (needs the existing augmented positives)
~/void-gen3-training/.venv/bin/python build_positive_speedaug.py
# 2. train + streaming-eval Exp02 (writes models/gen3_exp02.onnx + eval JSON)
~/void-gen3-training/.venv/bin/python run_gen3_experiment.py --name gen3_exp02 \
    --positive-train-dir data/gen3/augmented/positive_train_speedaug
# 3. fine operating-point sweep
~/void-gen3-training/.venv/bin/python analyze_exp02_operating_point.py
```

Deterministic given fixed data + seed (seed=1234; crc32 jitter seeds).

## Integration (PREPARED, NOT APPLIED — do this after the mic test)

Candidate artifacts (Gen 3 needs the classifier + the shared Whisper model):
- classifier: `models/hey_void_gen3.onnx` (+ `.onnx.data`), sha256
  `da396235a79ff15c1d90f6c478f3da746bc18364c36ed8753c4024fef7875d54`
- encoder: Whisper-small, loaded by faster-whisper from the local HF cache
  (already present from STT).
- adapter: `integration/whisper_gen3_wake.py`

Exact steps (all reversible; production `hey_void.onnx` is never touched):

1. Copy the classifier into the production project:
   `copy wakeword-training\models\hey_void_gen3.onnx  C:\V.O.I.D\wakeword-training\models\`
   `copy wakeword-training\models\hey_void_gen3.onnx.data  C:\V.O.I.D\wakeword-training\models\`
2. Copy the adapter: `integration/whisper_gen3_wake.py` →
   `C:\V.O.I.D\void\voice\whisper_gen3_wake.py`
3. In `void/voice/wake.py`, register the provider and add a factory branch
   (mirrors the openwakeword branch):
   ```python
   from void.voice.whisper_gen3_wake import WhisperGen3WakeDetector
   _PROVIDERS["whisper_gen3"] = WhisperGen3WakeDetector
   # in create_wake_detector(), before the final return:
   if name == "whisper_gen3":
       return factory(
           classifier_path=_cfg("voice.wake_model_path", "") or None,
           threshold=_cfg("voice.wake_threshold", 0.34),
           on_wake=on_wake,
       )
   ```
4. In `config/local_config.yaml`:
   ```yaml
   voice:
     wake_provider: whisper_gen3
     wake_model_path: C:/V.O.I.D/wakeword-training/models/hey_void_gen3.onnx
     wake_threshold: 0.34
   ```
5. Keep AudioCaptureBroker, VoiceController, generation/stale-callback
   guards, KillSwitch, RiskGate unchanged. The detector only emits
   WAKE_DETECTED; it authorizes nothing.

## Required real-microphone test (owner runs this before deploy)

With the candidate wired via config (`wake_provider: whisper_gen3`,
`wake_threshold: 0.34`):
- "Hey V.O.I.D." spoken normally → wakes (try ~10×; expect ≥9 wakes).
- Ordinary conversation near the mic for a few minutes → few/no false wakes.
- "hey boy" / "hey voice" / "avoid" → preferably no wake (weakest area).
- Background noise / music → no repeated wakes.
- Full path once: wake → speak a command → STT → Assistant → TTS.
- Tune `wake_threshold` up (fewer false wakes) or down (more sensitive) and
  `hop_seconds` for CPU/latency comfort.

## Known limitations

- Speaker diversity NOT established — 2 synthetic TTS voices in training +
  one real speaker (owner) in eval only. This is intentional (personal MVP).
- One owner utterance type is a persistent miss (~0.02).
- Adversarial `-oid/-oyd` near-misses are the weakest category (16.7% FP at
  0.34) but are rare in normal use and the wake only *activates* (never
  authorizes) — RiskGate/KillSwitch still gate all actions.
- CPU inference latency needs hop tuning or GPU for comfortable always-on
  use; must be confirmed on the production machine.
- Live-microphone validation has NOT been performed in the automated
  session; it is the final gate before deployment.
