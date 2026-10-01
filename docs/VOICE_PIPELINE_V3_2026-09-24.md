# Voice pipeline V3 — endpointing

Date: 2026-09-24 · Base commit 1b1af96 (working tree, uncommitted)

After V2 the command path was ~101 ms and speech-to-text ~90 ms, leaving endpointing as essentially the whole
remaining wait. This milestone retuned it against the owner's own recorded voice.

## 1. What "2 seconds of endpointing" actually was

Capture p50 was 2.71 s, but most of that is the owner **talking** — not latency. The endpointer
(`_WakeEndpointer`) ends a capture one `wake_silence_timeout` after the last frame it scored as speech, so the
wait the owner actually feels after finishing a command is:

```
wake_silence_timeout (0.8 s)  +  one 30 ms frame of quantisation
```

Everything else in `capture_s` is speech. **The optimisable term was 0.8 s, not 2.0 s.** Getting this wrong would
have meant chasing the wrong layer.

## 2. Method

`_WakeEndpointer` is fed the real 30 ms frames the broker delivers, so replaying recorded audio through it
reproduces exactly what the runtime would do. The corpus is **the owner's own voice** — 95 recordings from
`wakeword-training/data/personal_*` (23 originals plus speed and noise variants), their microphone, their room.
No synthetic speech was used for the endpointing decision, and no audio left the machine.

Speed variants matter most: slowing speech lengthens the pauses inside it, which is the exact condition that turns
a short timeout into a truncated command.

Two numbers per configuration: **endpoint latency** (frames between the last voiced frame and the capture
closing) and **truncation** (the capture closing while speech was still to come).

## 3. Experiments

| # | silence | gate | heard | endpoint latency | truncated | dismissed as "no speech" |
|---|---|---|---|---|---|---|
| A | 0.8 s | 500 | 81/95 | 0.81 s | 1 | **14** |
| B | 0.6 s | 500 | 81/95 | 0.60 s | 4 | 14 |
| C | 0.4 s | 500 | 81/95 | 0.42 s | 5 | 14 |
| D | 0.8 s | 300 | 92/95 | 0.81 s | 0 | 3 |
| **E** | **0.6 s** | **300** | **92/95** | **0.60 s** | **0** | **3** |
| F | 0.5 s | 300 | 92/95 | 0.51 s | 3 | 3 |
| G | 0.4 s | 300 | 92/95 | 0.42 s | 3 | 3 |
| H | 0.4 s | 200 | 94/95 | 0.42 s | 3 | 1 |
| I | 0.3 s | 300 | 92/95 | 0.30 s | 4 | 3 |

### The energy gate was the real problem

Lowering the timeout alone (B, C) made things **worse**: truncation rose from 1 to 4–5. The cause is the gate, not
the timer. The owner's peak level across recordings ranges from 465 to 10331 RMS — a 22× spread — and at a
threshold of 500:

* **14 of 95 recordings never registered as speech at all.** Those become a 4 s wait and no result, which matches
  the live telemetry: 141 captures ended on `no_speech` against 95 on `silence`.
* quiet frames *inside* a command scored as silence, so the trailing-silence timer started while the owner was
  still talking — which is why shortening the timeout truncated more.

Their room floor is ~14 RMS, so 300 keeps a wide margin, and the negative corpus shows no increase in noise
holding a capture open (123/220 clips >50 % voiced at 300, against 121/220 at 500).

## 4. Selected configuration

**E — `wake_silence_timeout: 0.6`, `wake_energy_threshold: 300`.**

Chosen because it is **strictly better than the baseline on every axis** — not because it was fastest:

| | baseline | selected |
|---|---|---|
| endpoint latency | 0.81 s | **0.60 s** |
| truncated commands | 1 | **0** |
| utterances lost entirely | 14 | **3** |

0.6 s is also the floor: every shorter setting truncated the owner's real speech (3 at 0.5 s, 4 at 0.3 s). The
fastest configuration was **not** selected, exactly as required — I and H are quicker and both cut people off.

The larger practical win is arguably the gate: roughly four out of five previously-lost utterances are now heard,
and each of those had cost a 4 s wait and no action.

## 5. End-to-end

| stage | before V3 | after V3 |
|---|---|---|
| endpointing (wait after the last word) | 0.81 s | **0.60 s** |
| speech-to-text (CUDA) | ~0.09 s | ~0.09 s (unchanged) |
| candidate resolution | ~0.02 ms | unchanged |
| application execution | 0.01–0.10 s | 0.01–0.10 s |
| **total after the owner stops speaking** | **~0.91 s** | **~0.70 s** |

Live, real catalog, provider that raises if touched: ChatGPT 7.8 ms · WhatsApp 7.8 ms · Opera GX 102 ms ·
VS Code 30 ms · File Explorer 11 ms · Windows Terminal 8.8 ms · Notepad 33 ms — **7/7 local, 0 model calls.**

## 6. Partial / streaming transcription — investigated, rejected

faster-whisper exposes no streaming API, and the architecture forbids executing on an incomplete transcript. More
importantly the arithmetic rules it out: decoding is **~90 ms**. Even a perfect incremental decoder could save at
most a fraction of that, against a 600 ms endpointing term. It would be complexity aimed at the wrong layer.

## 7. Everything else, re-verified

* **Application fast path** — all seven commands local, zero Gemini and zero Ollama calls, RiskGate enforced.
* **Phonetic resolution** — `chat gpd` → ChatGPT, `whats up` → WhatsApp, `not pad` → Notepad, all 0 model calls;
  `our projects`, `short gpd` and `skill once` still launch nothing. Sound-key threshold unchanged.
* **CUDA speech-to-text** — still `cuda`/`float16`, no `STT_DEVICE_UNAVAILABLE` on restart, CPU fallback tests
  green, no credential involved.
* **Gemini → Ollama** — 503 → `qwen3:8b` answered in 10.4 s cold after 2 bounded attempts; 33 failover tests green.

## 8. Security

The endpointer takes audio frames and returns one of three reasons. It holds no transcript, no catalog and no
tools, and a test asserts its source contains no launch, subprocess, Assistant, transcribe, RiskGate or catalog
reference — it cannot execute anything, and lowering a timeout cannot change that. No partial transcript can
trigger execution because no partial transcript exists. No secrets are logged.

**Voice recordings**: the 14,381 personal recordings under `wakeword-training/data/` were read locally and never
copied, moved or uploaded. They were untracked but **not ignored**, so a single `git add -A` would have written
the owner's biometric voice data into git history permanently. `.gitignore` now excludes
`wakeword-training/data/**/*.wav` and `*.npy` while leaving the scripts and config trackable.

## 9. Remaining bottleneck

Endpointing is still the largest single term at 0.6 s, and it is now **evidence-bound**: every shorter setting
truncated the owner's real speech. Further reduction needs a better speech/silence decision than RMS energy, not a
smaller number — for example the Silero VAD that faster-whisper already bundles, which would distinguish a pause
from the end of a sentence far better than an amplitude threshold. That is the natural next milestone.

Two caveats on this evidence: the corpus is **wake-word utterances** (speech span p50 0.48 s), not full sentences,
so pauses inside a long command are under-represented; and the 95 recordings derive from 23 originals, so the
effective sample is smaller than the count suggests.
