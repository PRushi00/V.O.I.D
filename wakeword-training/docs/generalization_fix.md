# Wake-word generalization-fix engineering pass

Follow-on to the 16kHz sample-rate fix and the hard-negative FP-reduction
pass. Objective: get "Hey V.O.I.D." adversarial false-positive rate from
~36.67% down to the deployment gate (human recall >=95%, adversarial FP
<=5%, speech FP <=2%, noise FP <=1%), without repeating the previously
rejected approaches (hard-negative expansion/oversampling, longer training
alone, capacity increase alone, temporal filtering, threshold tuning).

## Root cause 1 (confirmed, fixed): position-sensitivity

`training/extract_features.py`'s `_fit_to_length` always left-aligned a
clip's real audio within the fixed-length training window (silence
trailing at the end for short clips) - true for every training example,
positive and negative alike.

Direct diagnostic: the identical "hey void" audio, embedded via
`AudioFeatures.embed_clips()` and scored by `hey_void_16khz_fixed_500`'s
classifier, scored:

| offset into window | score |
|---|---|
| 0ms (the only placement ever produced) | 0.9104 |
| 100ms | 0.6949 |
| 200ms | 0.0155 |
| 300-600ms | ~0.01 |

A 200ms shift (3% of the 2.06s window) collapsed recognition of identical
content. The classifier had learned a position-dependent template, not
phonetic content.

**Fix**: `_fit_to_length(samples, n_samples, rng=None)` gained an optional
`rng` parameter - when given, places the real audio at a random offset
within the window (or a random sub-window for long clips) instead of
always left-aligned. `extract_all_features(..., jitter_seed=...)` applies
this to every `*_train` category (positive included - the diagnostic was
about positive recognition specifically); `*_val` categories are always
left unjittered so offline validation stays a stable, comparable metric.
`rng=None` (the default) is bit-identical to the pre-existing behavior.

Re-running the same diagnostic against a jitter-trained model confirmed
the fix: scores stayed in the 0.91-0.94 range across all offsets 0-600ms.

## Root cause 2 (confirmed, fixed): training instability

A controlled, single-variable training-step sweep (500/750/.../2000 steps,
identical seed/data/config, trained from one fixed, already-extracted
feature set - closing an earlier confound where each run's SAPI TTS
regeneration wasn't bit-identical) found the accuracy trajectory does NOT
converge smoothly - it oscillates, sometimes wildly:

| steps | adversarial FP @ 0.6 |
|---|---|
| 500 | 50.7% |
| 750 | 46.7% |
| 1000 | **98.7%** (near-total collapse) |
| 1250 | 86.7% |
| 1500 | 38.0% |
| 1750 | 68.0% |
| 2000 | 78.7% |

Picking a fixed step count and trusting it to land on a good point in this
oscillation is not reliable engineering - it is a gamble on where an
unstable trajectory happens to stop.

**Fix**: `training/train.py`'s `train_model()` gained an optional
`val_features` parameter. When given, every `val_every_n_steps` the current
weights are scored offline (`_val_score` - recall minus 2x adversarial FP
minus speech FP minus noise FP, weighting adversarial FP most heavily per
this project's stated priority) and the BEST-scoring checkpoint's weights
are restored before export, instead of unconditionally exporting whatever
the final step produced. `train_void.stage_train()` enables this by default
whenever a non-empty `positive_val` array is available; `val_features=None`
(direct `train_model()` calls without it) is unchanged from before.

Verified: training for 4000 steps with checkpoint selection (161 candidate
checkpoints evaluated) reliably reproduced the same quality region found by
the lucky "1500 steps" sweep point (human recall 95.7%, adversarial FP
~38-40% at threshold 0.5-0.65) - systematically, not by chance.

## Remaining gap: ~35-40% adversarial FP ceiling

With both fixes in place, the single-classifier adversarial FP rate
plateaus around 35-40% - well above the <=5% target. Three further,
materially different approaches were tried and did not break through:

1. **Larger capacity** (layer_dim 32->64): no material improvement,
   recall became more threshold-sensitive.
2. **Specialist Stage-2 verifier**: a separate, larger (layer_dim=128,
   n_blocks=2) classifier trained ONLY on the confirmed "-oid/-oyd"
   confound phrases (void, lloyd, avoid, hey voice, hey boy, etc. - see
   `build_stage2_verifier.py`) as its entire negative signal, using the
   SAME checkpoint-selection mechanism. Did not achieve strong
   discrimination even on its own specialized task (confound FP 54% at
   threshold 0.6) and its recall on genuine human speech was MORE fragile
   than Stage 1's.
3. **AND-cascade** (both stages must fire): worse than either stage alone
   at every threshold pair tested - best achievable joint recall was 78.3%
   (well below the 95% target), with confound FP still ~57-58%.

This convergent evidence across four independent architectural strategies
(jitter alone, checkpoint-selected training, larger/specialist classifier,
two-stage cascade) - all using the same frozen openWakeWord embedding
backbone - points to the feature representation itself, not the classifier
head, as the bottleneck: the shared embedding may not preserve enough
fine-grained phonetic detail to reliably separate "void" from its close
phonetic neighbors. See the final engineering report for the full
verdict and what would be required to close this gap.
