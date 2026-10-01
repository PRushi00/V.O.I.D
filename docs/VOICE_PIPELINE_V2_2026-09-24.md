# Voice pipeline V2 — candidate resolution and provider failover

Date: 2026-09-24 · Base commit 1b1af96 (working tree, uncommitted)

Two problems dominated the spoken experience: speech-to-text mishearing application names (5 of 14 real commands),
and a transient Gemini failure costing 14–16 s and then failing outright. Both are now addressed, and the route
taken to each was decided by measurement — including two approaches that were built, measured and thrown away.

## 1. Speech-to-text: what the errors actually are

The failures are not random noise. They are **phonetically faithful and orthographically wrong**:

| said | transcribed |
|---|---|
| Open ChatGPT | `Open short GPD.` / `open chat gpd` |
| Open WhatsApp | `open what's up.` |
| Open Notepad | `Open not pad` |
| Open Opera GX | `Open our projects.` |

That shape is what decided the design: the fix is not *more transcripts*, it is comparing how the transcript
**sounds** against how the installed names sound.

### Rejected: the decoder's own n-best (approach A)

faster-whisper asks CTranslate2 for `beam_size` hypotheses and reads only `sequences_ids[0]`. A thin proxy around
the ct2 model can ask for `num_hypotheses=beam_size` and keep the rest — no new dependency, no second decode. It
was built and measured:

* **the beam is not lexically diverse.** All five hypotheses for "Open ChatGPT" were `open chat gpp`,
  `open chat gpd`, `Open Chat GPP.` … the true name never appeared. For "Open WhatsApp" all five were
  `open what's up.`
* **beam_size ≥ 3 costs 2.4–2.5 s against 1.2 s greedy** — double the decode time for alternatives that do not
  contain the answer.

Rejected on both counts.

### Rejected: phonetic similarity with a threshold

Reducing both sides to a phonetic key and scoring similarity recovers the failures — and also matches things it
must not:

| non-command | closest application | similarity |
|---|---|---|
| "play some music" | Superhuman Go | 0.77 |
| "skill once" | calc | 0.75 |
| "how do i get there" | Weather | 0.75 |
| "our projects" | Perplexity | 0.71 |

Genuine matches went as low as 0.71, so **the distributions overlap and no cut separates them**. The scheme also
collapses distinct installed applications (`Microsoft News` ≡ `Microsoft Teams`, `calc` ≡ `Clock`). Rejected.

### Shipped: exact phonetic-key equality

Equality is a different proposition from a distance — it is the same kind of rule as the existing *spacing* tier,
which already says "Whats App" and "WhatsApp" are one name. A query resolves only when it sounds **exactly** like
**exactly one** installed application, and only when the key is long enough to identify a program at all.

The minimum key length was swept against 11 real failures and 69 ordinary phrases and common words:

| min key length | recovered | false positives |
|---|---|---|
| 0 | 10/11 | **12** — `work`→olk, `could`→Claude, `would`→Word, `other`→Weather, `the`→wt, `get`→code … |
| 3 | 10/11 | **4** — `work`, `could`, `would`, `other` |
| **4** | **10/11** | **0** |
| 5 | 5/11 | 0 |

`MIN_SOUND_KEY = 4` is the operating point: every dangerous collision has a 3-character key, every genuine
recovery has ≥ 4.

It is the **last** tier, after exact / spacing / prefix / publisher, so it can never override a name that actually
matches. A key shared by several installed applications returns all of them, and the fast path asks instead of
guessing.

### Confidence, honestly

The brief asked for thresholds like "ChatGPT confidence 0.96". No calibrated probability is available here — the
decoder's `avg_logprob` measures how sure Whisper was about its *own* text, not whether that text names an
application. What is used instead is **tier plus uniqueness**, which is checkable and testable:

| outcome | meaning | behaviour |
|---|---|---|
| HIGH | a deterministic tier, or a unique sound key ≥ 4 | execute through RiskGate |
| AMBIGUOUS | several applications matched at the same tier | ask the owner, launch nothing |
| NONE | nothing matched | the ordinary model path |

## 2. Gemini → Ollama failover

### The fallback brain was dead

`llm.local.model` named `llama3.1:8b`. Ollama on this machine has **`qwen3:8b`** and nothing else, so
`LocalProvider.available()` returned False and the local model was never selectable — the "fallback" in
`llm.fallback: ["local"]` had never once been usable. Corrected to `qwen3:8b` (measured 2.65 s warm standalone,
~4.7 s through the agent with tool schemas).

### Failure is now classified before it is acted on

`void/providers/failures.py` maps an exception to a category and a category to a policy:

| category | attempts | fails over |
|---|---|---|
| server (5xx), timeout, network, other | 2 | yes |
| rate_limit, quota, auth, unsupported_model, unavailable | 1 | yes |
| invalid_request | 1 | **no** — another provider would reject it identically |

Backoff is 1 s, bounded at 2 s: waiting is the alternative to switching, and switching is faster. On the **last**
provider in the chain the full retry budget is restored, so a single-provider install is no less resilient than
before.

Measured, with deterministic fake providers:

| case | before | after |
|---|---|---|
| Gemini success | — | 0.01 s |
| Gemini 503, no fallback | 3 attempts, ~3 s, **failed** | 3 attempts, 1.0 s, failed |
| **Gemini 503 → Ollama** | **failed after 14–16 s** | **completed, 1.0 s** (fake) / **5.6–5.9 s** (real, warm) |
| Gemini timeout → Ollama | failed | completed, 1.0 s |
| Gemini 429 / quota / auth → Ollama | failed | completed, 0.01 s, no pointless retries |
| invalid request | failed | failed immediately, **not** re-sent elsewhere |
| both providers down | — | failed, bounded, ≤ 5 calls total |

### Failing over cannot widen memory exposure

Memory is filtered per destination, and that filter used to be decided from the *first* provider. A run that can
reach a cloud provider is now built under cloud rules from the start, so a fallback can never receive memory that
was only cleared for the local model. Four tests pin the direction that matters.

## 2b. Speech-to-text on the GPU

The machine has an RTX 5070 and CTranslate2 supports CUDA, but `device="cuda"` failed with
*"Library cublas64_12.dll is not found or cannot be loaded"*: CTranslate2 bundles cuDNN but **not** cuBLAS.
Installing `nvidia-cublas-cu12` supplies it. `os.add_dll_directory` is not enough — CTranslate2's native loader
resolves these through **PATH** — which is what `void/voice/cuda.py` now sets before the model is built.

Same audio, same decoding options, only the backend changed:

| configuration | model load | decode p50 | decode p90 | VRAM | opened the right application |
|---|---|---|---|---|---|
| `cpu` / `int8` (previous default) | 1.4 s | **1115 ms** | 1136 ms | — | **11/14** |
| `cuda` / `float16` | 1.2 s | **74 ms** | 140 ms | 806 MB | **11/14** |
| `cuda` / `int8_float16` | 1.1 s | 83 ms | 254 ms | 355 MB | 11/14 |

**9–15× faster at identical end-to-end accuracy.** Raw transcript equality is slightly *worse* on the GPU (it
returns `open what's up` for WhatsApp) — but that transcript is exactly what the sound tier resolves, so the
metric that matters is unchanged. Judging the backends on raw transcripts would have rejected a change that costs
nothing and saves a second.

`voice.stt_device` is now `cuda` with `voice.stt_compute_type: float16`. A machine with no GPU, no CUDA runtime or
a full GPU **falls back to `cpu`/`int8` automatically** with a warning, so the default is safe everywhere;
`nvidia-cublas-cu12` is listed as optional in `requirements-voice.txt`.

### The command half of a voice turn, end to end

Production adapters, real catalog, provider that raises if touched:

| spoken | transcript | STT | command | total |
|---|---|---|---|---|
| Open Opera GX | `Open Opera GX` | 106 ms | 65 ms | **171 ms** |
| Open ChatGPT | `Open chat GPT.` | 87 ms | 12 ms | **99 ms** |
| Open WhatsApp | `open whatsapp` | 72 ms | 10 ms | **82 ms** |
| Open VS Code | `Open VS Code.` | 73 ms | 30 ms | **103 ms** |
| Open File Explorer | `Open file explorer.` | 88 ms | 13 ms | **101 ms** |
| Open Windows Terminal | `open Windows terminal.` | 86 ms | 9 ms | **95 ms** |
| Open Notepad | `Open Notepad.` | 92 ms | 50 ms | **142 ms** |

**7/7 correct, 0 model calls, p50 101 ms** for everything between the owner finishing the sentence and the
application opening. That number was ~1.15 s before this milestone.

## 3. Endpointing

Measured from the live telemetry: of 72 interactions that produced a real transcript, **60 ended on `silence`**,
capture p50 **2.71 s**. Capture is speech + `wake_silence_timeout` (0.8 s) + a 0.4 s lead-in grace.

**Not changed.** The trailing silence is what protects against cutting the owner off mid-command, and the only
honest way to lower it is to measure truncation against *their* voice, which cannot be done without recording
them. It is a UX trade for the owner to make, not a number to guess at — the knob is `voice.wake_silence_timeout`.

Also visible: 141 captures ended on `no_speech`, i.e. wake fired with nothing following. Those cost the owner
nothing (no command was spoken) but they are the bulk of all captures.

## 4. Text-to-speech

Separated properly, and the headline is that **TTS does not delay the action at all**:

| | |
|---|---|
| `speak()` returns in | **0–40 ms** (SAPI `SVSF_ASYNC`) |
| playback duration, short confirmation ("Opening Notepad.", 16 chars) | 1.7–2.2 s |
| playback duration, p50 across all replies | 4.54 s |
| implied fixed startup inside playback | ~0.8–1.0 s (108 ms/char short vs 58 ms/char long) |

The application is already open before the sentence finishes; the spoken confirmation is a tail, not a wait.
Replies for launches are already minimal ("Opening ChatGPT."). `voice.tts_rate` (currently 1) shortens playback
further if the owner wants it.

## 5. Resource use — and the previously unexplained CPU

Steady state, wake active and confirmed (`subscribers=1`, 18 inferences per 5 s):

| | |
|---|---|
| CPU | **0.29 cores** |
| memory | 807 MB RSS (Whisper `small` STT + the Gen-3 wake encoder) |
| threads | 46 |
| system total | 3.4 % |

**The 0.58 cores reported on 2026-09-23 was a measurement artefact.** The Gen-3 wake encoder takes about **90
seconds** to load after a restart; during that window the process burns markedly more CPU and the broker reports
`subscribers=0`. Samples taken at different offsets after a restart therefore land in different phases. Measured
after the wake detector confirmed active, CPU is 0.29 cores — the same range as before, and the 2026-09-23 wake
CPU fix is intact (`voice.wake_cpu_threads: 1`, applied to both the ONNX session and CTranslate2).

## 6. End-to-end, live on this machine

Real `Assistant`, real catalog, with a provider that **raises if touched**:

| command | status | latency | model calls |
|---|---|---|---|
| Hey V.O.I.D., open ChatGPT | completed | 10 ms | 0 |
| Hey V.O.I.D., open Opera GX | completed | 451 ms | 0 |
| Hey V.O.I.D., open WhatsApp | completed | 8 ms | 0 |
| Hey V.O.I.D., open VS Code | completed | 42 ms | 0 |
| Hey V.O.I.D., open Terminal | completed | 10 ms | 0 |
| Hey V.O.I.D., open Notepad | completed | 72 ms | 0 |
| Hey V.O.I.D., open File Explorer | completed | 12 ms | 0 |
| **`open chat gpd`** (misheard) | completed | **7 ms** | **0** |
| **`open whats up`** (misheard) | completed | **8 ms** | **0** |
| **`open not pad`** (misheard) | completed | **22 ms** | **0** |
| **`open net flix`** (misheard) | completed | **8 ms** | **0** |
| `open our projects` | no launch → model | — | 1 |
| `open short gpd` | no launch → model | — | 1 |
| `open skill once` | no launch → model | — | 1 |
| explain ARP, Gemini 503 → Ollama | **completed** | 11.3 s cold / 5.6–5.9 s warm | 2 failed + 1 local |

The four misheard forms used to cost 14–16 s each and fail.

## 7. Where the spoken turn's time goes now

```
capture / endpointing   ~2.0-2.7 s     unchanged (0.8 s trailing silence is the owner's trade)
speech-to-text          ~0.09 s        small on the GPU (was ~1.2 s on the CPU)
candidate resolution    ~0.02 ms       new, and free
command execution       ~0.01-0.07 s
--------------------------------------------------------------------
action complete         ~2.1-2.9 s
text-to-speech           0-40 ms to dispatch; 1.7-2.2 s of speech afterwards
```

**Speech-to-text and endpointing are now the whole budget.** Nothing in the application path is a meaningful term
any more.

## 8. Still open

* **Endpointing (~2.0 s) is now the entire remaining budget** - it is roughly 95 % of the wait. Speech-to-text is
  no longer a meaningful term. Lowering `voice.wake_silence_timeout` below 0.8 s is the only lever left, and it
  trades directly against cutting the owner off mid-command.
* **A larger model on the GPU was not measured.** `medium` would need a ~1.5 GB download; with `small` at 74 ms
  there is headroom to trade latency for accuracy, and that is the obvious next experiment.
* **GPU contention with Ollama was not stress-tested.** Whisper takes ~0.8 GB and qwen3:8b ~5 GB of the 8 GB
  card; they fit, but the failover path has not been run under a loaded GPU.
* **A mishearing that is not phonetically faithful still goes to the model** — "Open Opera GX" → "Open our
  projects." is recovered by nothing here, and must not be: recovering it would mean guessing from an unrelated
  sentence.
* **`Opera GX` resolves to two catalog entries** (`opera` the executable and `Opera GX Browser` the shortcut).
  Both open Opera GX, so it is not user-visible, but they are not de-duplicated because their display names differ.
* Validation used synthetic (SAPI) speech with added noise plus the owner's recorded transcripts. **No testing
  against the owner's own voice was possible.**
