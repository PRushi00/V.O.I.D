# Speculative / overlapped STT: measured, and not implemented

Date: 2026-09-27 · Base commit 1b1af96 (working tree, uncommitted)

**Outcome: no production change.** Speculative STT was investigated to the point of deciding, and rejected for two
independent reasons: the window it could recover is ~10 ms in the common case, and a speculative transcript is not
reusable because Whisper is not an incremental decoder. This document exists so the work is not repeated.

## 1. Root cause of the remaining STT latency — and a corrected premise

The milestone assumed the decode is a meaningful part of the wait. Measured from the live runtime's own telemetry
since the GPU switch (n = 192 real interactions):

| | |
|---|---|
| decodes ≤ 0.3 s | **169 / 192** |
| decode p50 for those | **10 ms** |
| decode p50, all samples | 29 ms |
| decode p90 | 344 ms |
| decode max | 3.63 s |

And the whole post-endpoint sequence, driven through the real path on ~2 s clips:

| stage | p50 |
|---|---|
| `capture.stop()` (frame join, int16 → float32) | **0.1 ms** |
| `audio_guard.has_enough_speech` | **0.2 ms** |
| **speech-to-text** | **88 ms** |
| `Assistant.run()` (resolve + launch) | 13.7 ms |
| **endpoint → action** | **121 ms** |

So the decode is indeed the only stage worth overlapping — 72 % of the post-endpoint path on 2 s clips. But on the
**real** commands the owner actually speaks it is 10 ms at p50, because those utterances are short. The window
speculation could recover is therefore ~10 ms typically and ~88 ms for a long utterance, against a post-speech
budget of roughly 0.45 s (endpoint) + 0.12 s = **0.57 s**. That is 2–15 %.

The tail is real but is not something speculation fixes: the 13 decodes above 0.5 s do not scale with audio length
(a 2.34 s clip took 2.45 s while 4.02 s clips took 10–552 ms) and 7 of them fall inside one period on 2026-09-26
with 11–46 s between them. That is the GPU being busy with something else — a speculative decode during the same
window would be just as slow, and would be competing for the same device.

## 2. Why no architecture was selected

Speculation requires one property: **a prefix of the audio must decode to the same text as the whole.** Otherwise
the speculative transcript cannot be substituted for the authoritative one, and checking whether it may be
substituted requires the final decode — the exact cost speculation exists to avoid. It is a circular dependency,
not an implementation difficulty.

That property was tested directly (`scripts/bench/speculative_stt_probe.py`). For each of the owner's recordings
the audio was truncated at the point the production endpointer would have reached 0.30 s of trailing silence — the
instant a speculative decode would fire — and at the shipping 0.60 s endpoint, and both were decoded:

| clip | snapshot at 0.30 s | authoritative at 0.60 s | same? |
|---|---|---|---|
| Recording (3) | `'Hey, void'` | `'Hey, void'` | yes |
| Recording (7) | `'Hey, Lord'` | `'Hey, Lord'` | yes |
| Recording (9) | `'Hey, worried'` | `'Hey, worried'` | yes |
| **Recording (5)** | **`'Hey'`** | **`'Hey white'`** | **no — a word was missing** |
| **Recording (8)** | **`'Hey, boy'`** | **`'Hey, Void.'`** | **no** |
| **Recording (2)** | **`'Hey boy'`** | **`'Hey, warrant'`** | **no** |
| **Recording (14)** | **`'Eh, Lord.'`** | **`'Yeah, Lord.'`** | **no** |

**Identical in 7 of 11; different in 4 of 11 (36 %).** Whisper attends over the whole input window, so 0.3 s more
trailing audio changes which tokens it emits — sometimes emitting a word the snapshot omitted entirely. It is a
sequence-to-sequence model over a fixed window, not a streaming decoder with reusable state.

Four variants were considered against that finding and all fail for the same underlying reason:

* **decode a snapshot and reuse it** — unsafe, 36 % mismatch, and verifying costs the decode.
* **decode a snapshot to warm the pipeline, discard the text** — the model is already resident and each decode is
  independent; there is no cross-call state to warm. `warmup()` already covers cold start.
* **speculatively resolve intent from the snapshot** — catalog resolution is **0.02 ms**. There is nothing to save.
* **overlap `Assistant.run()` with the decode** — it needs the transcript; there is nothing to overlap.

Every stage that could be overlapped is either free (0.02–0.2 ms) or cannot start without the final transcript.

## 3. What was NOT built, deliberately

No speculative worker, no audio snapshot, no cancellation, no staleness tracking, no second decode. Adding a
concurrent decode on every command would have doubled GPU decode work, introduced contention with both the wake
model and Ollama (which holds 6.19 GB of this 8.15 GB card), and added race conditions and shutdown paths — to
recover ~10 ms that cannot be safely reused.

The non-negotiable safety rule was never at risk, because nothing speculative exists: the only transcript in the
system is still the authoritative one, and it still travels
`transcript → intent → validation → RiskGate → execution` unchanged.

## 4. Files changed

| file | change |
|---|---|
| `scripts/bench/speculative_stt_probe.py` | new — the prefix-equivalence experiment above, re-runnable |
| `docs/SPECULATIVE_STT_2026-09-27.md` | this document |

No production file, configuration value, dependency or test was modified. No new dependency.

## 5. Validation

Full suite **2295 passed, 5 skipped, 2 xfailed, 0 failed**; collection guard ok; compile ok — identical to the
start of the milestone, as a no-op should be. `grep` confirms no speculative code in `void/voice/` or `void/core/`.

The application fast path was exercised through the real `Assistant` while measuring the post-endpoint sequence:
every application command resolved locally with **zero model calls**, and only the genuine knowledge requests
("Explain ARP to me", and two conversational clips) consumed one each. The missing-application fast path from the
previous milestone is untouched.

## 6. Resource usage

Unchanged — nothing was added. For the record, a speculative decode would have cost one extra GPU decode per
command (~10–88 ms of device time) plus a worker thread per capture.

## 7. Security

No production change, so no boundary moved. RiskGate, the kill switch, protected roots, credential handling and
the application fast path are untouched. The investigation read the owner's recordings locally and copied,
persisted and transmitted nothing; `.gitignore` continues to exclude `wakeword-training/data/**/*.wav`. No
transcript or audio content is logged by anything added here.

## 8. Limitations and what could not be validated

* The equivalence test used **11 usable recordings** — the owner's real wake-word utterances. 12 clips were
  skipped because the endpointer never reaches its silence threshold inside the file (the recordings end at the
  speech). A command corpus with natural trailing silence would test this better, and recording one was out of
  scope.
* Those recordings are **wake-word utterances**, not commands, so the mismatch rate on real commands is unmeasured.
  It would have to be far below 36 % for the conclusion to change, and the failure mode observed (a whole word
  appearing only in the longer audio) is exactly what matters for a command.
* No live spoken validation was performed, because there is nothing new to validate.
* The multi-second decode tail was attributed to GPU contention from its time clustering and its independence from
  audio length. That was **not** confirmed by correlating against GPU utilisation at those moments — the
  telemetry does not record it.

## 9. Next milestone

The voice path is now measured end to end and the remaining budget is roughly:

```
endpointing      ~0.45 s      evidence-bound: two different detectors agree (V3, V4)
speech-to-text   ~0.01-0.09 s measured out this milestone
resolution       ~0.02 ms
execution        ~0.01-0.12 s mostly the OS starting the application
```

There is no large latency win left in this pipeline. The honest recommendation is to **stop optimising voice
latency** and pick a milestone that adds capability rather than shaving milliseconds — the two candidates the
measurements point at are:

1. **The decode tail.** 13 of 192 decodes took 0.5–3.6 s, apparently from GPU contention. Recording GPU
   utilisation in the telemetry would confirm it, and a policy for when Ollama holds most of the card is a real
   robustness question rather than a latency one.
2. **Transcript accuracy on quiet speech**, which remains the largest source of *failed* commands and is
   untouched: the V4 work showed Silero discards whole utterances on this voice, and the phonetic resolver only
   rescues errors that are phonetically faithful.
