# Voice latency: "Open Opera GX" — 83 s → 1.1 s

Date: 2026-09-23 · Base commit 1b1af96 (working tree, uncommitted) · Target interaction: **"Hey V.O.I.D., open Opera GX."**

## 1. Original latency (MEASURED, real physical interaction)

Not a reconstruction: V.O.I.D's own telemetry (`~/.void/perf/perf.jsonl`) and task store recorded the real interaction on
2026-09-21 17:11:15. The stored goal was exactly `'Open Opera GX'` — a clean transcript, so the wake phrase was **not**
contaminating it.

| t (s) | stage | detail |
|---:|---|---|
| 0.00 | activation | wake |
| 2.23 | endpoint | `capture_s=2.223` — **user speech, not system latency** |
| 3.39 | STT | `decode_s=1.164` |
| 3.40 | route | gemini |
| 14.49 | **LLM #1** | 11.08 s → proposed `find_app('*Opera*')` |
| 14.53 | tool | `find_app` 0.036 s → **2 matches, ambiguous** |
| 36.27 | **LLM #2** | 21.74 s → proposed `find_app('Opera GX Browser')` |
| 36.27 | tool | `find_app` 0.000 s → 1 match |
| 43.16 | **LLM #3** | 6.88 s → proposed `launch_app(app-41b5…)` |
| 43.25 | tool | `launch_app` 0.093 s → **the browser actually opened here** |
| 80.42 | **LLM #4** | 37.12 s → final sentence "I have launched Opera GX Browser." |
| 80.43 | complete | `total_s=77.031`, `steps=4` |
| 83.15 | speak | 2.73 s of audio |

**Measured bottleneck: the LLM, 76.8 s of 83.1 s (92.4 %)**, across four round trips. Everything V.O.I.D itself does —
intent routing, three tool executions, formatting — totalled **0.13 s**. The user waited ~40 s *after* the browser was
already open, purely for Gemini to compose one sentence.

Other interactions in the same log (n=21 reaching `complete`) show the same shape: LLM 3.5–102 s, tools ≤0.1 s except
two genuine filesystem searches, STT 1.0–3.9 s.

## 2. Root cause

`AppCatalog.find` is exact-name-only by design. The installed Start-Menu entry is **"Opera GX Browser"**; the owner says
**"Opera GX"**. So:

```
transcript "Open Opera GX"  ->  fast-path grammar OK, phrase = "opera gx"
                            ->  AppCatalog.find("opera gx") = []          <- exact match fails
                            ->  Decision(why="unknown")  ->  falls through to the Agent
                            ->  Gemini: find_app('*Opera*') -> ambiguous -> find_app(exact) -> launch_app -> summarise
```

The deterministic fast path was working exactly as written — it simply could not resolve a spoken name that omits an
installed suffix. Verified directly against the real catalog before the change:

```
'Open Opera GX'          plan=no  why='unknown'      <- 83 s via Gemini
'Open Opera GX Browser'  plan=YES                    <- 0.03 ms (the exact installed name)
```

A second, independent problem was found while profiling (§4).

## 3. Change A — unique whole-word prefix resolution

`AppCatalog.find_name_prefix()` (new) matches entries whose name **starts with the spoken whole words, in order**.
`AppCatalog.find()` is **unchanged**, so the LLM-facing `find_app` tool keeps its exact/glob contract. The fast path
calls the new method **only when exact matching found nothing**, and accepts the result **only when it is unique**.

Safety boundaries, all tested:

| Spoken | Installed | Result |
|---|---|---|
| "opera gx" | "Opera GX Browser" | ✅ resolves (unique) |
| "opera" | "opera" + "Opera GX Browser" | ✅ **exact wins** — opens the exe, not the browser |
| "opera gx" | "Opera GX Browser" + "Opera GX Developer" | ⛔ ambiguous → agent asks the owner |
| "oper", "opera g", "operagx" | "Opera GX Browser" | ⛔ partial word never matches |
| "studio" | "Visual Studio Enterprise" | ⛔ substring never matches (must start at word 1) |
| "gx opera", "opera browser" | "Opera GX Browser" | ⛔ reordering/gaps never match |
| "powershell 7" | "PowerShell 7 (x64)" | ⛔ shell/console exclusion still applies |

The tool argument is still an engine-chosen `app_id` — never speech — and still passes through `launch_app`
revalidation, the protected-location check, and RiskGate.

**One existing test changed meaning.** `test_a_substring_is_not_a_match` asserted that a non-exact name never resolves.
Its real safety content (no substring, no partial word, no reordering, never guess) is now asserted by five sharper
tests; the one case that intentionally changed — a unique whole-word prefix — is asserted as supported. This is a
deliberate behaviour change, not a weakened assertion.

## 4. Change B — the always-on wake detector was burning ~19 of 24 cores

Found while profiling STT: the live runtime (`VOID_VoiceRuntime`, `pythonw voice_startup.py`) sat at **1926 % of one
core (≈19.3 of 24) continuously while merely listening**. That is also why STT decoded 0.93 s of audio in 3.84 s.

Isolating the cost per stage (2 s window every 200 ms, the detector's real cadence):

| stage | CPU (idle machine) |
|---|---:|
| idle loop | 0.00 cores |
| mel feature extraction | 0.00 cores |
| mel + CTranslate2 encode (`cpu_threads=1`) | 0.26 cores |
| **+ ONNX classifier session (defaults)** | **20.64 cores** |
| + ONNX classifier session (`intra/inter_op_num_threads=1`) | **0.30 cores** |

The dominant consumer was **ONNX Runtime's thread pool**, not the Whisper encoder: the classifier is trivial, but ORT
sizes its intra-op pool to every core and **spin-waits between runs**. Per-inference time was unchanged (77 ms → 85 ms,
within noise), so this was pure waste.

Separately, CTranslate2's default (all cores) was measured **slower and far more erratic** than one thread for the
encode (2 s window, idle, p50/p95 ms): default 168/403, 8 threads 191/220, 4 threads 110/276, 2 threads 77/108,
**1 thread 88/92**. Only 1–2 threads stay under the 200 ms hop at p95 — the default was overrunning its own hop.

Both engines are now bounded by one knob, `voice.wake_cpu_threads` (default **1**).

**Verified on the live scheduled task**, not just in a benchmark:

| | before | after |
|---|---:|---:|
| runtime CPU while idle-listening | **19.3 cores** (1926 %) | **0.39 cores** (39 %) |
| wake inference cadence | 16–18 per 5.0–5.2 s | 17–18 per 5.0–5.2 s (**unchanged**) |
| frames received | 167 / 5 s | 167 / 5 s (unchanged) |

Wake detection is fully functional at identical cadence for ~1/50th of the CPU.

## 5. After — measured

Synthetic end-to-end (SAPI-spoken utterance → **real** `FasterWhisperSTT` → **real** `Assistant` → real fast path,
RiskGate and `launch_app`; launcher stubbed so no app is started; live runtime running concurrently; n=3, p50):

| Stage | Before (measured, real interaction) | After (measured, synthetic pipeline) |
|---|---:|---:|
| Wake → capture | not instrumented separately | not instrumented separately |
| User speech (not system latency) | 2.23 s | 2.13 s (utterance length) |
| **STT** | **1.16 s** (3.13–3.84 s when the CPU was saturated) | **1.13 s** |
| **Intent routing** | ~0 | **<0.001 s** |
| **LLM** | **76.84 s** (4 calls) | **0 s (0 calls)** |
| **Tool execution** | 0.13 s | 0.004 s |
| Response formatting | ~0 | ~0 (engine-owned phrase) |
| TTS (audio duration, not latency) | 2.73 s | not re-measured |
| **System total (STT→reply, excl. speech & TTS)** | **78.0 s** | **1.14 s** |
| Wall clock to spoken reply | **83.1 s** | ~1.1 s + TTS |

Transcript was `"Open Opera GX."`; reply `"Opening Opera GX Browser."`; `steps=1`; **LLM calls = 0**.

**It also now works when Gemini is down.** During the very same measurement run, `"Explain how ARP works."` and
`"What files are in my project directory?"` both failed after ~17 s with Gemini `503 UNAVAILABLE — "This model is
currently experiencing high demand"` (the known provider issue, see `docs/GEMINI_3_8_FLASH.md`), while the launch
command completed in 1.13 s. Launch commands no longer depend on the cloud at all.

## 6. Routing after the change (verified against the real catalog)

| Command | Route |
|---|---|
| "Open Opera GX" | **fast path, 0 LLM** |
| "Open YouTube in Opera GX" | agent/LLM (compound — not a plain launch) |
| "Explain how ARP works" | agent/LLM |
| "What files are in my project directory?" | agent/LLM |
| "What do you remember about me?" | memory recall route |
| "Remember that I like dark mode" | memory command route |
| "Open PowerShell" | agent/LLM (`excluded` — shells are never fast-pathed) |
| "Open Opera GX and delete my files" | agent/LLM (compound) |

## 7. Remaining bottlenecks (measured, not fixed here)

1. **STT ≈ 1.13 s** is now the largest system component of a launch command. `small`/int8/CPU with greedy decoding;
   `base` or CUDA would cut it, but that trades accuracy or changes the local/native architecture — out of scope here.
2. **Cold start**: STT warm-up (model load + dummy decode) measured **2.57–2.80 s**, paid once at runtime start by the
   existing `warmup()`, not per command. Assistant construction 0.13–0.15 s.
3. **The LLM path itself** remains 3.5–102 s and currently unreliable (Gemini 503s). Anything that is not a
   deterministic launch still pays that. This is the next milestone, not a latency bug in V.O.I.D.
4. **TTS** is SAPI, synthesised before playback; time-to-first-audio was not separately instrumented.

## 8. What was NOT changed

Voice architecture, VoiceSession, endpointing/VAD parameters (`wake_silence_timeout` 0.8 s etc. were already snappy and
the measured capture times are the owner's own speech), STT engine/model, TTS provider, Agent, RiskGate,
CapabilityEngine, memory, provider selection, retry policy, and the scheduled task definition. The live task instance
was stopped and restarted only to take an uncontaminated measurement and to load the fix.

## 9. Method / reproduction

* Real interaction evidence: `~/.void/perf/perf.jsonl` (interaction `48e88132f416d041`) and `~/.void/tasks.sqlite`.
* Wake CPU attribution: replicate `WhisperGen3WakeDetector._score_current_window` at a 200 ms cadence, timing each
  stage with `psutil.Process().cpu_percent()`, toggling `SessionOptions.intra_op_num_threads` and
  `WhisperModel(cpu_threads=…)`.
* End-to-end: SAPI-synthesised 16 kHz mono WAV of the command → `FasterWhisperSTT.transcribe` → `Assistant.run`,
  launcher stubbed.
* Live verification: `schtasks /end|/run VOID_VoiceRuntime`, then process CPU over uptime and the runtime's own
  `WAKEWORD_PROCESSING_ACTIVE` cadence markers.
