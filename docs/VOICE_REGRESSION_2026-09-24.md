# The "application launches got slower" report — what was actually measured

Date: 2026-09-24 · Base commit 1b1af96 (working tree, uncommitted)

The owner reported that application launches had gone from "nearly instantaneous" to 2–3 s after the automatic
application-discovery work, that Opera GX was unreliable, that ChatGPT was slow or failed with *"The task failed.
Please check the command line."*, and that "Open Terminal" answered *"Command was not possible."*

## 1. The command path did not get slower — it got ~100× faster

The live runtime's own telemetry (`~/.void/perf/perf.jsonl`) records every stage of every real voice interaction.
Grouped by hour, across the whole discovery project:

| window | n | capture p50 | STT p50 | **Assistant p50** | Assistant max |
|---|---|---|---|---|---|
| 09-21 13h | 6 | 3.96 s | 1.50 s | **8.05 s** | 10.53 s |
| 09-21 20h | 8 | 4.00 s | 1.16 s | **42.17 s** | 69.52 s |
| 09-21 21h | 20 | 4.01 s | 0.01 s | **172.67 s** | 242.88 s |
| 09-23 21h | 25 | 4.00 s | 1.12 s | **22.69 s** | 53.43 s |
| **09-23 23h** | 21 | **2.21 s** | 1.17 s | **0.06 s** | 20.18 s |

`Assistant.run()` — everything from transcript to launch — went from **8–173 s to 0.06 s**. Capture went *down*
(4.00 s → 2.21 s). STT is flat at ~1.17 s throughout. Nothing regressed.

The 2–3 s the owner experiences is the voice pipeline around the command, which this work never touched:

```
capture (endpointing)   ~2.0 s      speech + 0.8 s trailing silence
speech-to-text          ~1.2 s      faster-whisper "small", greedy, int8, CPU
COMMAND EXECUTION       ~0.05 s     <-- the part the discovery work owns
text-to-speech           1.7-2.4 s  duration of the spoken reply
```

So "~1 second" was never an end-to-end voice figure: it was the *command* figure, and the command is now 20× faster
than the 1.1 s that earned that name. **Getting the spoken turn near 1 s is an STT/endpointing problem, not an
application-discovery one.**

## 2. What was actually failing

The task store holds the real transcripts of the owner's 23:04–23:11 session. Every correctly transcribed
application command fast-pathed and completed in 7–99 ms. Every failure was one of two things:

| transcript | what was said | outcome |
|---|---|---|
| `Open Opera GX` ✓ | Opera GX | completed |
| `Open our projects.` ✗ | Opera GX | **misheard** → Gemini → 503 |
| `Open not pad` ✗ | Notepad | **misheard** → Gemini → 503 |
| `Open short GPD.` ✗ | ChatGPT | **misheard** → Gemini → 503 |
| `Open chat GPT` ✓ | ChatGPT | completed, "Opening ChatGPT." |
| `open to do with these.` ✗ | — | **misheard** → Gemini → 503 |
| `open windows terminal` ✓ | Windows Terminal | **refused by policy** → Gemini → 503 |

* **"The task failed. Please check the command line."** is a Gemini **503** after a fast-path miss — confirmed in
  the task store (`ServerError: 503 UNAVAILABLE`). Not a launch failure, not a catalog failure.
* **Opera GX "unreliable" and ChatGPT "slow"** are the *same* cause: speech-to-text. When the name is transcribed
  correctly both open with zero model calls in tens of milliseconds. When it is misheard, the sentence is not an
  application command any more and costs a 14–16 s failed model round trip.
* **"Open Terminal"** was the only genuine code defect (§3).

5 of 14 spoken commands were misheard. That is the dominant remaining problem, and it is an STT problem.

## 3. Fixes

### 3.1 A terminal window is an ordinary application

`_SHELL_WORDS` refused to fast-path anything matching `terminal`, `console`, `shell`, `cmd`, `powershell`, `wsl`,
`python`… The exclusion guarded against V.O.I.D *executing a command through* an interpreter. It cannot:
`launch_app` starts a catalog-chosen identity and supplies **no arguments of its own**, so opening a terminal shows
a prompt the owner still has to type into — exactly as consequential as opening any other application.

The line is now drawn at what a program *does when it starts*: administration consoles that edit the machine's
configuration (`regedit`, `gpedit`, `secpol`, `diskpart`, `bcdedit`, `mmc`, task scheduler) and the script
hosts/LOLBins whose purpose is to run something else (`mshta`, `wscript`, `cscript`, `rundll32`, `msiexec`) stay
refused. A new test pins the property that justifies the change: every call the fast path emits is `launch_app`
with a single engine-chosen identity and no other argument.

This is a deliberate **policy** relaxation, not a bug fix, and is called out as such.

### 3.2 A vendor word may be on either side

"Open Windows Terminal" then still missed: the installed Store app is called just `Terminal`. Resolution already
dropped a vendor word the *installed name* carries (`teams` → "Microsoft Teams"); it now also drops one the
*owner adds* that the installed name omits (`windows terminal` → "Terminal"). Still the last tier, so
`open windows security` resolves to "Windows Security" on its exact name, and `open security` still matches
nothing.

### 3.3 A misheard phrase no longer pays for a rediscovery

Speech-to-text mishears constantly, so "nothing matched that name" is a **common** event — 5 of 14 commands. Each
one triggered a full ~430 ms rediscovery that could not possibly help. The cheap change signal is now consulted
first (~6 ms), and the blind rediscovery that exists only to notice a newly installed *Store* app is left to a much
slower interval (300 s instead of 15 s). Measured: **433 ms → 5.7 ms** per miss, with the Store case still caught.

This also fixed a latent bug in that gate: freshness-checking and content-age were sharing one timestamp, so
confirming "nothing changed" kept resetting the clock and the Store catch-up could never fire. They are now
separate (`_built_at` vs `_checked_at`).

### 3.4 The catalog is warmed at start-up, like the speech model

The first "open <app>" after a restart paid **473 ms** for the initial discovery — the single largest term in the
command path, and pure start-up cost. It is now built on a daemon thread at `start()`, next to the existing STT
warm-up. First decide after prewarm: **473 ms → 0.81 ms**.

## 4. What was tried and rejected: biasing speech-to-text with application names

faster-whisper 1.2.1 accepts `hotwords`. Since the dominant problem is misrecognised application names, telling the
decoder which names to expect is the obvious remedy. It was measured on the same audio and the same model
(SAPI-synthesised commands plus three control sentences, degraded with Gaussian noise):

| hotword source | exact transcripts | decode time |
|---|---|---|
| none (baseline, σ=0.05) | 6/10 | 1185 ms |
| 7 hand-written **spoken** forms | **9/10** | 1180 ms |
| 136 catalog names, filtered | 8/10 | 1357 ms |
| all 195 catalog names | 8/10, and it **corrupted a control sentence** | ~1390 ms |

The curated list is excellent — and it is exactly the hand-maintained application list that automatic discovery
exists to abolish. So the implementation derived the list automatically instead, from the applications the owner had
actually launched (a bounded most-recently-used list, fed by `launch_app`, read by the decoder).

**That version was then measured end to end and was worse than no biasing at all:**

| spoken | plain decoder | biased by the learned list |
|---|---|---|
| Open ChatGPT | `Open Chat GPP.` | `OpenChat GPD` |
| Open VS Code | `open BS code.` | `OpenBS Code` |
| Open Windows Terminal | `open windows terminal.` ✓ | `OpenWindows Terminal.` ✗ |
| **reached the fast path** | **5/7** | **4/7** |

Biasing toward *catalog display names* ("Opera GX Browser", "Visual Studio Code", "Terminal") is not the same as
biasing toward what people say, and it damages word segmentation — it glues the verb to the name, which the launch
grammar cannot parse at all. Two materially different automatic sources (whole catalog, learned display names) both
made things worse; only the hand-curated spoken forms helped, and only on synthetic speech.

**The entire feature was therefore reverted** — `RecentApps`, the launch bookkeeping, the decoder parameter, the
config key and its tests. Nothing the system can derive on its own helps, so none of it ships. The rejection and its
numbers are recorded in `FasterWhisperSTT`'s docstring so the next person does not repeat it.

## 5. Measurements after the fixes

Real machine, 202 applications, through the real `Assistant`, with a provider that **raises if touched**:

| command | status | latency | model calls |
|---|---|---|---|
| Hey V.O.I.D., open WhatsApp | completed | 22 ms | 0 |
| Hey V.O.I.D., open Opera GX | completed | 218 ms | 0 |
| Hey V.O.I.D., open ChatGPT | completed | 28 ms | 0 |
| Hey V.O.I.D., open File Explorer | completed | 59 ms | 0 |
| Hey V.O.I.D., open VS Code | completed | 80 ms | 0 |
| Hey V.O.I.D., open Terminal | completed | 17 ms | 0 |
| Open Windows Terminal | completed | 10 ms | 0 |
| Open Netflix | completed | 14 ms | 0 |

Identical under **Gemini 503** and **Gemini 429**. Only genuinely unknown names (Spotify — not installed; "Editor" —
not installed; "our projects" — misheard) reach the model, and they are the only failures.

Stage costs: cold discovery 433 ms (now off the command path), change signal 5–6 ms, warm resolution 0.017–0.019 ms,
whole fast-path decision 0.026–0.049 ms, shortcut validation ~1.3 ms warm, miss 5.7 ms.

## 6. Still open

* **Speech-to-text accuracy is the dominant remaining problem** — 5 of 14 real commands misheard, each costing a
  14–16 s failed model round trip. Biasing the decoder was measured and rejected (§4). Realistic next candidates,
  none attempted here: a larger or GPU model (accuracy vs the 1.17 s decode), an n-best/alternatives pass that lets
  the catalog pick among candidate transcripts, or wake-word-scoped grammar constraints.
* **A fast-path miss costs 14–16 s and a hard failure** whenever Gemini is 503ing, because a 503 is retried three
  times on the same provider and never triggers failover to the local model. Unchanged and out of scope here; it is
  the same milestone flagged on 2026-09-23.
* **Endpointing and TTS** (~2.0 s capture, 1.7–2.4 s of speech) dominate the spoken turn and were not touched.
* **Runtime CPU measured 0.58 cores** after this work, against 0.30–0.38 on 2026-09-23. Nothing added here runs
  continuously (the prewarm is a one-shot thread), the wake fix is still in place, and the system was otherwise
  idle at 5.8%. The difference is **unexplained and was not investigated** — recorded rather than dismissed.
