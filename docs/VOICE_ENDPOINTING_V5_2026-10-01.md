# Endpointing V5: the budget adapts, and the wait drops a third

Date: 2026-10-01 · Base commit 1b1af96 (working tree, uncommitted)

**Outcome: shipped.** Endpoint latency on the owner's own recordings falls from **0.45 s to 0.30 s at p50** and
0.57 s to 0.39 s at p90, with **zero truncations** — where the same 0.40 s applied unconditionally truncates 2 of 20.
Total latency after the owner stops speaking falls from **600 ms to 455 ms**.

Two previous milestones concluded 0.6 s was the floor. Both were right about what they measured: a **fixed** budget.
Neither asked *which* utterances need the long one.

Reproduce with `scripts/bench/endpoint_adaptive_bench.py` and `scripts/bench/voice_path_bench.py`.

## 1. Previous behaviour

`_WakeEndpointer` closed a wake capture on one of three bounded conditions: `wake_silence_timeout` (0.6 s) of
trailing silence after speech, `wake_no_speech_timeout` (4 s) with no speech at all, or `wake_max_capture_seconds`
(15 s). "Speech" is an int16 RMS gate (`wake_energy_threshold`, 300) over the broker's 30 ms frames, with a 0.4 s
lead-in grace protecting the gap between the wake word and the command.

V3 shortened the fixed timeout and found 0.5 s truncated 3 of 23 recordings. V4 replaced the detector with streaming
Silero VAD and found no setting that was both faster and non-truncating. Both stopped there.

## 2. Baseline

Measured against faster-whisper's **offline** Silero VAD, which sees the whole recording and is better informed than
any streaming decision. The harness reproduces V4's published figures exactly (0.45 / 0.57 / 1.14 s, 0 truncated),
which is what validates it.

| corpus | n | p50 | p90 | p95 | max | truncated | never fired |
|---|---|---|---|---|---|---|---|
| **real** (owner's microphone) | 20 | **0.45 s** | 0.57 | 1.14 | 1.14 | **0** | 0 |
| degraded variants | 68 | 0.48 s | 1.14 | 1.20 | 1.26 | 1 | 2 |
| synthesised command list | 13 | 0.51 s | 0.54 | 0.54 | 0.54 | 0 | 0 |

And the whole path after speech ends, in one process, through the real broker, session, endpointer, speech-to-text
and assistant, frames fed at real time:

| stage | p50 | p90 | max |
|---|---|---|---|
| A speech end → endpoint decision | **510 ms** | 540 | 540 |
| B endpoint → speech-to-text starts | **0.3 ms** | 0.4 | 0.5 |
| C speech-to-text | 73.5 ms | 84.8 | 132.8 |
| D dispatch (resolve + RiskGate + launch) | 12.0 ms | 16.9 | 20.0 |
| **E total after speech ends** | **600.5 ms** | 635.5 | 685.1 |

## 3. The bottleneck

**Stage A is 85 % of the wait, and stage B — the thread hand-off — is 0.3 ms.** Queue scheduling, worker dispatch and
"STT readiness" were candidates in the brief; they are measured and they are not the problem. Neither is the frame
size (30 ms, so the decision granularity is 1.5 % of the budget) nor audio buffering.

What *is* the problem: **one utterance in twenty forces the budget for all of them.**

The longest silence *inside* an utterance, as the production gate sees it, over the owner's 20 real recordings:
p50 0.00 s, p90 0.27 s, **max 0.51 s** — and that maximum belongs to a single clip. Nineteen of twenty would be safe
at 0.30 s.

Then, over all 95 recordings (52 internal gaps), where those gaps sit:

| speech heard before the gap | n | gap p50 | gap max |
|---|---|---|---|
| < 0.30 s | 24 | 0.06 s | **0.51 s** |
| 0.30 – 0.60 s | 8 | 0.06 s | 0.15 s |
| 0.60 – 1.00 s | 10 | 0.09 s | 0.30 s |
| > 1.00 s | 10 | 0.06 s | 0.09 s |

**Long gaps are a start-of-utterance phenomenon** — hesitation, a false start, the tail of the wake word. Once the
speaker is under way the worst gap observed is 0.30 s. That is a signal the endpointer already has, for free.

## 4. The chosen solution

The trailing-silence budget adapts on accumulated **voiced** seconds:

| condition | budget |
|---|---|
| speech so far < `wake_fast_after_speech` (0.30 s) | **0.60 s** — still at the fragile beginning |
| 0.30 s ≤ speech so far < `wake_fast_until_speech` (1.80 s) | **0.40 s** — the length a command occupies |
| speech so far ≥ 1.80 s | **0.60 s** — this is a sentence, not a command |
| an internal gap ≥ `wake_pause_evidence` (0.20 s) was **survived** | **0.60 s** for the rest of the capture |

The budget is clamped to never exceed `wake_silence_timeout`, so the endpointer can only ever end a capture *earlier*
than before, never later. Setting `wake_fast_silence_timeout` ≥ `wake_silence_timeout` disables all of it.

Three details each earned their place by measurement:

**The ceiling (1.8 s).** The first version had none and truncated the long question *"What is the difference between
TCP and UDP, and when would I use each of them?"*. Its one long clause break is 0.45 s and arrives after **2.58 s**
of speech, while every short command in the list finishes inside 1.65 s with no gap above 0.06 s. Long gaps belong to
sentences — and sentences are exactly where a faster endpoint does not matter, because an informational request is
followed by a model call measured at ~6.9 s p50. The optimisation is confined to the only case where endpointing *is*
the whole wait.

**The hesitation latch.** On spliced real speech with two pauses — a 250 ms hesitation, then a second gap — the latch
is the difference between matching the old behaviour and being much worse than it:

| configuration | longest second pause survived |
|---|---|
| fixed 0.60 s (baseline) | 420 ms |
| **adaptive, with the latch** | **420 ms** |
| adaptive, latch removed | 240 ms |

**0.40 s rather than 0.35 s.** Both truncate nothing on the real corpus, and 0.35 s is 60 ms faster — but it leaves
only 50 ms over the worst gap measured once an utterance is under way (0.30 s), against 100 ms for 0.40 s, and it
survives a 60 ms shorter mid-utterance pause. Correctness over the round number.

## 5. Rejected alternatives

| idea | why it was rejected |
|---|---|
| **Silence *depth* as the signal** (require less trailing silence when the room is deeply quiet) | Measured first, and it is **backwards** on this voice: internal pauses are p50 RMS **6**, *quieter* than the trailing room floor at p50 13. 100 % of internal-pause frames fall below every threshold tried. The deepest silence is inside the utterance. |
| **A fixed 0.40 s** | Truncates 2 of 20 real recordings, 5 of 68 degraded. This is what the condition exists to avoid. |
| **A fixed 0.30 s** | 3 of 20 and 9 of 68. |
| **A predictive latch** (catch the small gap that precedes a clause break) | The gaps preceding those long breaks are 0.03–0.06 s — the same length as ordinary gaps between words in commands with no pause at all. No threshold can catch one without latching on everything; the sweep's `evidence 0.09` row shows p90 returning to the baseline's 0.57 s. |
| **A transcript-based endpoint** ("stop when the words already form a complete command") | Requires speech-to-text mid-capture. Measured and rejected in `docs/SPECULATIVE_STT_2026-09-27.md`: a prefix decodes differently from the whole in 4 of 11 cases, and verifying costs the decode being avoided. |
| **Streaming Silero VAD** | Measured and rejected in V4: no setting is both faster and non-truncating, and it is less robust on degraded audio. |
| **An LLM deciding whether the owner finished** | Out of the question — the decision must stay local and free. It stays two float comparisons on the broker thread. |

## 6. Measured improvement

### Endpointing

| metric | before | after |
|---|---|---|
| **Endpoint p50** (real corpus) | 0.45 s | **0.30 s** (−33 %) |
| **Endpoint p90** | 0.57 s | **0.39 s** (−32 %) |
| Endpoint p95 | 1.14 s | 1.14 s |
| Endpoint max | 1.14 s | 1.14 s |
| **Premature endpoints / truncated commands (real)** | **0** | **0** |
| truncated, degraded corpus | 1 | 1 |
| truncated, synthesised list | 0 | **1** (see §7) |
| longest single mid-utterance pause survived | 540 ms | 360 ms |
| longest *second* pause survived (after one hesitation) | 420 ms | **420 ms** |

### The whole path

| stage | before | after |
|---|---|---|
| A speech end → endpoint | 510 ms | **360 ms** |
| B endpoint → STT start | 0.3 ms | 0.5 ms |
| C speech-to-text p50 / p90 | 73.5 / 84.8 ms | 79.8 / 117.2 ms |
| D local resolution p50 | 12.0 ms | 17.5 ms |
| **E total after speech ends** | **600.5 ms** | **455.1 ms** (−24 %) |

C and D are unchanged code; the differences are run-to-run spread on a shared GPU and are reported rather than
explained away. Per phrase, 9 of 13 commands end **180 ms** sooner, 3 are unchanged (the safety rules engage), 1 is
truncated.

**This is not a "1-second system" claim.** 455 ms is the wait *after the owner stops speaking*, for a deterministic
local command, with 0 model calls. An informational request adds a Gemini round trip measured separately at ~6.9 s
p50; TTS is additional again.

## 7. Correctness impact

One real cost, stated plainly: **the longest mid-utterance pause a command survives drops from 540 ms to 360 ms**
(until the owner hesitates once, after which it is 420 ms as before). The synthesised phrase
`"Open VS Code <450 ms pause> and WhatsApp"` — whose gap measures 510 ms — is now cut after "Open VS Code", where the
old budget kept it.

Why that is an acceptable trade, and the evidence for it:

* **No recording of the owner's real speech contains such a gap.** Over 95 recordings the longest internal gap once
  an utterance is under way is 0.30 s; 0.40 s leaves 100 ms of margin. The 450 ms figure is a number inserted into a
  speech synthesiser, not an observation of this speaker.
* **That phrasing was already fragile**: at 510 ms it cleared the old 600 ms budget by 90 ms. A slightly longer pause
  was always cut.
* **A single hesitation restores the old behaviour exactly**, which is the realistic protection for someone who
  genuinely pauses.
* **Degraded audio is unaffected**: 1 truncation before, 1 after.

An existing test asserted that a 0.45 s mid-command pause survives. It has been updated to the measured 0.30 s gap
and **extended** with a case proving the latch covers a 0.45 s *second* pause — the guard is more precise than it
was, not weaker. The test that asserted the wait is identical at every command length now pins the adaptive shape at
six lengths instead.

## 8. What changed

| file | change |
|---|---|
| `void/voice/runtime.py` | `_WakePolicy` gains `fast_silence_s`, `fast_after_speech_s`, `fast_until_speech_s`, `pause_evidence_s`; `_WakeEndpointer` accumulates voiced seconds, latches a survived pause, and selects the budget in `_silence_budget()`; the capture-start log reports the live policy; the endpointer reports `fired_budget_s` |
| `void/voice/session.py` | `note_endpoint_reason` carries `budget_s` through to the `endpoint` event |
| `void/perf/schema.py` | `endpoint.budget_s` (a float) |
| `config/default_config.yaml` | the four new keys, with the measurements that justify them |
| `tests/test_endpointing_adaptive.py` | new — 42 tests |
| `tests/test_endpointing.py` | three tests updated to the new design, one added |
| `scripts/bench/endpoint_adaptive_bench.py` | new — the comparison, four corpora, and the parameter sweep |
| `scripts/bench/voice_path_bench.py` | new — the stage-by-stage in-process measurement |

No new dependency. No new thread. No model. No network. `_silence_budget` is two comparisons and a `min`.

## 9. Validation

* **Full suite: 2504 passed, 5 skipped, 2 xfailed, 0 failed.** Collection guard floor 2470 → 2511; compile ok.
* **Mutation testing: 13 deliberate breakages, 13 caught, 0 escaped** — including the latch being checked after the
  counter is reset (which would silently disable it), accumulated speech counting silence, and each shipped config
  value being lowered past what was validated.
* **Regression, through the real assistant:** WhatsApp, VS Code, File Explorer, Windows Terminal, ChatGPT, Opera GX
  and a folder all open silently with **0 model calls**; Notepad++ and an unknown name are spoken failures with 0
  model calls; `open vs code and whatsapp` opens both silently; `open vs code, whatsapp and notepad++` opens two and
  speaks only the failure; *explain ARP*, *what is DNS* and *explain the TCP handshake* each make exactly one Gemini
  call and are spoken. Ollama: 0. RiskGate and the kill switch active.
* **Security:** no new import in `runtime.py`; no shell, `eval`, `exec`, subprocess or network anywhere in the
  change; audio never leaves the machine and the endpointer still never buffers or persists it (pinned by a test).
  Telemetry gained one float. A malformed config value is clamped to a safe default, and the fast budget is clamped
  to the safe one, so no configuration can make V.O.I.D wait longer or cut instantly. 108 risk / protected-root /
  kill-switch / audio-guard tests pass.
* **Live runtime** restarted and armed, reporting the new policy in its capture log.

## 10. Remaining limitations

* **One speaker, one microphone, one room.** The gap distribution that the whole design rests on is this owner's.
  A different speaker with slower cadence would want a larger `wake_fast_silence_timeout`; the knob is in the config
  with the measurement beside it.
* **The real corpus is wake-phrase recordings.** There is no recording of the owner speaking a *command*, so the
  truncation evidence comes from "Hey V.O.I.D." clips (one word boundary, like a two-word command), spliced real
  speech with controlled gaps, and synthesised commands. This limitation is inherited from V3 and V4, which measured
  the same corpus.
* **Synthesised speech is weak evidence about acoustics.** It is used for phrase *coverage* — a long sentence, a
  multi-application command, deliberate pauses — not for the truncation threshold.
* **The ceiling is a proxy.** "Speech longer than 1.8 s is a sentence" is true of this command set; a genuinely long
  deterministic command would lose the benefit (not correctness).
* **n is small** for the tail: 20 real recordings, 13 phrases, 9 spliced gaps.

## 11. Next bottleneck

Endpointing is no longer the largest term it was, but it is still 79 % of the 455 ms (360 of 455). The remaining
floor is detecting end-of-speech at all, and three materially different methods have now been measured against it.

The next bottleneck worth attention is **not latency**:

1. **A narrow lost-wake race, found while building the bench.** `_on_wake_detected` sets `_wake_capture_active` on
   the broker thread and hands the blocking work to the worker; if the monitor's reconciler ticks in between, it sees
   a capture whose session has not reached `LISTENING` and cancels it. The window is the worker-queue latency
   (microseconds) against a 100 ms poll, so it is rare — but it silently drops a wake, and it is a correctness bug
   rather than a tuning question. Out of scope here (this milestone is endpointing only) and left untouched.
2. **`endpoint.budget_s` is now recorded.** The fast-vs-safe split over the owner's real use, and any truncation it
   causes, can be read straight from `perf.jsonl` after a week — which is how this milestone should be judged,
   rather than by rebuilding the conditions again.
