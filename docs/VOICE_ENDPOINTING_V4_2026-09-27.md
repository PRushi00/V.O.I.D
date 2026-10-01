# Endpointing V4: Silero VAD investigated, measured, and not adopted

Date: 2026-09-27 · Base commit 1b1af96 (working tree, uncommitted)

**Outcome: no production change.** Streaming Silero VAD was integrated, measured against the owner's own voice,
and rejected on evidence. The baseline was also found to be better than assumed. Two candidate improvements were
built and both were reverted. This document exists so the work is not repeated.

## 1. The current implementation

`_WakeEndpointer` in `void/voice/runtime.py` consumes the broker's 30 ms / 480-sample frames and closes a capture
when one of three bounded conditions holds: `wake_silence_timeout` (0.6 s) of trailing silence after speech,
`wake_no_speech_timeout` (4 s) with no speech at all, or `wake_max_capture_seconds` (15 s). "Speech" is an int16
RMS threshold (`wake_energy_threshold`, 300). A 0.4 s lead-in grace protects the gap between the wake word and the
command.

## 2. Root cause of the delay — and a correction to the premise

The brief states endpointing costs ~0.6 s. **0.6 s is the configured timeout, not the measured latency.**

Measured against an independent reference — faster-whisper's *offline* Silero VAD, which sees the whole recording
and is better informed than any streaming decision — the shipping endpointer closes the capture **0.45 s (p50)**
after the true end of speech on the owner's real recordings. It is lower than the timeout because the reference's
speech boundary includes trailing decay and breath that the amplitude gate already scores as silence.

So the real quantity to beat was 0.45 s, not 0.6 s.

## 3. Was Silero VAD already available?

**Yes, twice, and no dependency was added.**

* `faster_whisper/assets/silero_vad_v6.onnx` — used by `vad_filter=True`, batch API (`get_speech_timestamps`).
* `openwakeword/resources/models/silero_vad.onnx` — with a ready-made streaming wrapper,
  `openwakeword.vad.VAD`, exposing `predict(x, frame_size=480)` and `reset_states()`.

openwakeword is already a V.O.I.D dependency (it runs the wake word). Its wrapper takes exactly the 480-sample
frame the broker already delivers, normalises int16 internally, and caps its own thread count. Inference measured
at **0.107 ms per frame** — negligible, and it would only run during a capture window, not continuously.

Nothing needed installing, downloading or writing from scratch.

## 4. Why it was not adopted

Both endpointers were scored against the offline reference over the owner's recordings. Silero's streaming
thresholds were set from the measured distribution on this voice (speech-frame probability p50 0.593, p25 0.197;
real-recording peak p50 0.984, p10 0.684) rather than from the library default, giving hysteresis of
on = 0.30 / off = 0.20.

**Real microphone recordings (n=20)** — the corpus a shipping decision should rest on:

| configuration | latency p50 | p90 | max | truncated | never fired |
|---|---|---|---|---|---|
| **amplitude 0.6 s / gate 300 (shipping)** | **0.45 s** | 0.57 s | 1.14 s | **0** | **0** |
| amplitude 0.8 s / gate 500 (pre-V3) | 0.66 s | 0.72 s | 1.11 s | 0 | 2 |
| VAD 0.6 s | 0.54 s | 0.60 s | 1.05 s | 0 | 0 |
| VAD 0.5 s | 0.45 s | 0.51 s | 0.96 s | 0 | 0 |
| VAD 0.4 s | 0.36 s | 0.39 s | 0.42 s | **1** | 0 |
| VAD 0.35 s | 0.30 s | 0.33 s | 0.36 s | **2** | 0 |
| VAD 0.3 s | 0.24 s | 0.27 s | 0.30 s | **3** | 0 |
| VAD 0.25 s | 0.21 s | 0.24 s | 0.27 s | **4** | 0 |

**All recordings including degraded training variants (n=88)** — a robustness stress test:

| configuration | latency p50 | p90 | max | truncated | never fired |
|---|---|---|---|---|---|
| amplitude 0.6 s / gate 300 (shipping) | 0.45 s | 1.14 s | 1.26 s | **1** | **2** |
| VAD 0.6 s | 0.54 s | 0.84 s | 1.44 s | **5** | **4** |
| VAD 0.5 s | 0.45 s | 0.75 s | 1.35 s | 6 | 4 |
| VAD 0.3 s | 0.24 s | 0.51 s | 0.96 s | 15 | 4 |

The conclusion is not close:

* **VAD cannot beat the shipping endpointer's latency without truncating.** VAD 0.5 s exactly matches it (0.45 s
  p50) with no truncation; every faster setting starts cutting the owner off — 1 in 20 at 0.4 s, 3 in 20 at 0.3 s.
* **VAD is less robust.** On degraded audio it truncates 5 times where the amplitude gate truncates once, and
  fails to hear speech at all in 4 recordings against 2.
* The one genuine VAD advantage is a **tighter tail** (max 0.96 s vs 1.14 s at matched p50). A 0.18 s tail
  improvement does not justify putting a neural model in the capture loop and losing robustness.

Silero is simply not confident on this voice: the median speech frame scores 0.593, and the lower quartile 0.197 —
overlapping the silence distribution's p99 of 0.406. An amplitude threshold happens to suit this microphone better
than a model trained on conversational speech suits these short, quiet utterances.

**So ~0.45 s is close to the floor for reliably detecting end-of-speech here, by two materially different
methods.** V3 established it by shortening the amplitude timeout (truncation below 0.6 s); V4 establishes it again
with a different detector entirely.

## 5. A second candidate, also reverted

The Silero pass *inside* STT (`voice.stt_vad_filter`) was measured while investigating the above. It is free in
latency (**−4 ms**: trimming silence pays for itself) but it **discarded 3 of 23 recordings entirely** — empty
transcript with the filter, real words without. An empty transcript is not an exception, so the adapter's existing
retry-without-VAD never fired and the utterance was simply lost.

A retry-on-empty was implemented, gated on `audio_guard.has_enough_speech` so silence could not be re-decoded into
invented words. It worked: 2 of the 3 recovered, and two seconds of silence and a 12 ms click both still produced
`''`.

**It was reverted anyway**, because decoding the same audio three times gave:

```
['I worry.', 'I worry.', 'Good work.']
["I'm bored.", "Don't worry.", "I'm bored."]
```

The retry does not recover what was said — it recovers a *different guess each time*. Turning "no transcript" into
"an unreliable transcript" is the wrong direction for a system where a wrong action is worse than no action, and an
empty transcript already reaches the "didn't catch that" path. Shipping it would have looked like progress and
delivered noise.

## 6. Resource impact

None: no production code changed. For the record, had VAD shipped it would have added 0.107 ms per 30 ms frame
during capture only (~2 % of one core for ~2 s per command) and no GPU or memory cost — the constraint was
accuracy, never cost.

## 7. What did change

| file | change |
|---|---|
| `scripts/bench/endpoint_bench.py` | new — the reproducible amplitude-vs-VAD comparison above |
| `docs/VOICE_ENDPOINTING_V4_2026-09-27.md` | this document |

No production file, configuration value, dependency or test was modified. `void/voice/adapters.py` and
`void/voice/runtime.py` are byte-identical to before this milestone.

## 8. Validation

Full suite **2295 passed, 5 skipped, 2 xfailed, 0 failed**; collection guard ok; compile ok — unchanged from the
start of the milestone, as expected for a no-op change. The benchmark is re-runnable with
`python scripts/bench/endpoint_bench.py`.

## 9. Security

No production change, so no boundary moved. The investigation read the owner's recordings locally and copied,
persisted and transmitted nothing; `.gitignore` continues to exclude `wakeword-training/data/**/*.wav`. No
transcript or audio content is logged by anything added here. RiskGate, the kill switch, protected roots and the
application fast path are untouched, and the previous milestone's missing-application behaviour is unaffected.

## 10. Limitations

* The corpus is **wake-word utterances** ("Hey V.O.I.D.", speech span p50 0.48 s), not full commands, so pauses
  inside a long sentence are under-represented. A command corpus would need new recordings, which was out of scope.
* 88 scored clips derive from **23 originals**, so the effective sample is smaller than the count suggests; only
  20 are the genuine microphone path.
* Silero's thresholds were tuned on the same recordings they were then evaluated on. With a corpus this small
  that risks over-fitting in VAD's *favour* — and it still lost, which strengthens rather than weakens the
  conclusion.
* No live spoken validation: the endpointer was driven from recorded audio, not the microphone.

## 11. Next milestone

Endpointing is **measured out** as a source of easy latency. The remaining ~0.45 s is the cost of being sure
someone has stopped talking, and two different detectors agree on roughly that figure.

The honest next lever is not a faster detector but **overlapping the wait with the work**: begin decoding the
audio captured so far while the trailing-silence timer is still running, and commit only when the endpoint fires.
Speech-to-text is ~90 ms on the GPU, so a speculative decode started 0.3 s early could make the transcript
available essentially the moment the endpoint fires — turning 0.45 s + 0.09 s into ~0.45 s. That is a concurrency
change to the session, not a new model, and it must not execute anything on a partial transcript.
