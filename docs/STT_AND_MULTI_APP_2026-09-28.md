# Speech-to-text reliability, and several applications in one command

Date: 2026-09-28 · Base commit 1b1af96 (working tree, uncommitted)

Two pieces of work. The first was an investigation with a named suspect — GPU contention with Ollama — and the
suspect was **cleared**: contention is real as a mechanism (5.1× slower, demonstrated below) but the tail it was
supposed to explain happened when Ollama was not running at all. The second is a feature: `"open VS Code and
WhatsApp"`, executed deterministically, per-target, with **zero model calls**.

Reproduce with `scripts/bench/stt_bench.py`, `scripts/bench/stt_accuracy.py` and `scripts/bench/multi_app_bench.py`.

---

## 1. Speech-to-text baseline

From the live runtime's own telemetry, since the GPU switch (2026-09-25 onwards). The previous milestone quoted
"p50 10 ms", which was **wrong in a way worth naming**: it averaged in 74 events where `audio_guard` rejected the
capture before any decode ran (`backend=audio_guard`, `decode_s=0.0`) and 121 decodes that returned nothing.
Separating them gives the number the owner actually experiences.

| | n | p50 | p90 | p95 | max | ≥ 0.5 s |
|---|---|---|---|---|---|---|
| everything the old figure averaged | 237 | 8 ms | 332 | 498 | 3632 | 11 |
| **real decodes** (guard-rejected excluded) | 163 | 75 ms | 416 | 1523 | 3632 | 13 (8.0 %) |
| **decodes that produced a command** | **42** | **243 ms** | **1683** | **2206** | **2513** | **10 (24 %)** |

So roughly **one real command in four waited half a second or more in speech-to-text alone**, and the worst waited
2.5 s. That is the problem; 10 ms was never it.

For comparison, the same model decoding clean 1.6–4.1 s clips on an idle machine: **p50 184–198 ms, max 351 ms**.
Production is therefore 10–25× slower than the hardware can do, occasionally.

## 2. What was measured, and what each condition ruled in or out

`scripts/bench/stt_bench.py` runs the decode under controlled conditions and records, for every decode, the wall
clock, audio length, transcript, GPU utilisation / memory / **SM clock** / P-state sampled *during* the decode,
process CPU and RSS, and what Ollama was holding. GPU sampling is one long-lived `nvidia-smi --loop-ms` process:
invoking it per sample costs 138 ms here — more than a decode — and would have created the contention it was
meant to observe.

20 decodes per condition (4 repeats × 5 clips, 1.61–4.09 s), the live runtime stopped so nothing else held the GPU.

| condition | p50 | p90 | max | ms per audio-second | GPU util (max) | Ollama in VRAM |
|---|---|---|---|---|---|---|
| **A** alone | 189.3 ms | 289.2 | 293.9 | 75.8 | 73 % | 0 |
| **B** Ollama server up, no model resident | 184.0 ms | 260.1 | 270.2 | 74.0 | 76 % | 0 |
| **C** `qwen3:8b` **resident**, not generating | 198.3 ms | 261.8 | 278.3 | 77.2 | 74 % | 5.20 GB |
| **C2** Ollama **generating** | **958.8 ms** | **1479.3** | **1554.7** | 342.0 | 100 % | 5.20 GB |
| **D** 25 decodes back to back | 163.9 ms | 181.4 | 187.1 | 68.1 | 71 % | 0 |
| **F** warm (the shipping start-up state) | 187.5 ms | 271.8 | 351.4 | 78.2 | 60 % | 0 |
| **G** after 60 s of GPU idleness | 270.3 ms | 449.8 | 449.8 | 122.1 | 6 % | 0 |
| **G** immediately again | 164.7 ms | 165.1 | 165.1 | 59.8 | 28 % | 0 |
| **H** V.O.I.D's own wake encoder running | 105.4 ms | 144.2 | 144.5 | 39.9 | 50 % | 0 |
| **E** cold, fresh process, no `warmup()` | 920–2237 ms | — | — | — | 13 % | 0 |

### Confirmed causes

1. **Ollama actively generating: 5.1× (p50 189 → 959 ms, max 1555 ms).** Real, large, and reproducible. The GPU sat
   at 96–100 % utilisation throughout.
2. **A cold decode: 920–2237 ms**, on top of 2.5–4.2 s of model loading. This is the only measured effect big enough
   to explain the largest production values. It is *mostly* already handled — see §3.
3. **Device power state: +105 ms at p50, +285 ms at p90** for the first decode after 60 s of idleness (270 ms vs
   165 ms immediately after). The SM clock was 240–405 MHz during the idle decode against 720–855 MHz for the one
   that followed. The same effect shows up between benchmark runs: condition A measured 189 ms p50 with clocks
   peaking at 1260 MHz and 109 ms p50 in a later run at 2805 MHz — the *same work*, 1.7× apart, on clock state
   alone. n is small (3 idle pairs), so this is reported as a demonstrated direction with a measured magnitude, not
   a precise coefficient.
4. **Whisper's rising-temperature retry: 2.0×–7.2× on difficult audio.** See §4; this is the one that was also
   worth fixing.

### Rejected causes

| hypothesis | verdict | evidence |
|---|---|---|
| **GPU contention with Ollama caused the observed tail** | **rejected** | The mechanism exists (C2), but the telemetry says it was not present. There are **16 local-provider calls in the entire week of logs, 2 of them after the GPU switch.** Of the 11 slow decodes, 9 are **3 to 25 hours** from the nearest Ollama call and 2 are 7–14 minutes away — Ollama unloads after 5 minutes. `provider_call` events were never emitted at all, so the correlation was done on `llm` events. |
| Ollama merely **resident** in VRAM | rejected | C vs A: +9 ms (+5 %), inside run-to-run spread, with 5.20 GB of an 8.15 GB card occupied. |
| V.O.I.D's **own wake encoder** (a second Whisper-small in the same process, every 200 ms) | rejected | H vs A in the same run: **105 ms vs 109 ms**. This was the strongest remaining suspect and it costs nothing. |
| Repetition / drift / a leak | rejected | D: 25 consecutive decodes, 147–187 ms, no trend. |
| Audio length | partially | Cost scales with length (68–78 ms per audio-second) but the tail does **not**: a 0.96 s capture took 1746 ms while 4.02 s captures took 352–383 ms. |

### Still unexplained, and honestly so

Cold start plus the retry plus clock state account for the *shape* and much of the *size* of the tail, but not every
production value: a 2.5 s decode of 3.9 s audio is still larger than any condition here reproduced on a warm
process. The distinguishing fact the telemetry did not record was **what the decode produced** — a decode that
worked hard on difficult audio looks identical to a busy machine. The `stt` event now carries `chars` (a count,
never text), which will settle it from the owner's own next week of use rather than from a guess.

## 3. Is the cold cost already handled?

Mostly. `warmup()` decodes 0.1 s of silence at start-up on a background thread. Measured in a fresh process:

| | |
|---|---|
| model load | 1972 ms |
| `warmup()` as shipped | 461 ms |
| **first real decode after warmup** | **204 ms** |
| second and after | 80–109 ms |

So warmup absorbs almost all of it — the residue is ~100 ms. Condition E's 920–2237 ms is the *unwarmed* path, which
production only takes if a command arrives inside the first ~2.5 s of a runtime start. **No change was made**: the
existing prewarm is doing its job, and inventing a bigger warmup to fix a window that is already covered would be
optimising a measurement artefact.

## 4. The one speech-to-text change: a single deterministic decode

faster-whisper's default `temperature` is `[0, 0.2, 0.4, 0.6, 0.8, 1.0]`: when a segment's decode looks bad by the
compression-ratio or log-probability thresholds, it is **decoded again at a higher temperature**, up to six times.
Measured on the same warm model, alternating the two settings so neither benefits from running second:

| audio | default | `temperature=0` | ratio | same text? |
|---|---|---|---|---|
| clean command | 102 ms | 105 ms | 0.97× | yes |
| the same command buried in noise | 433 ms | 135 ms | **3.20×** | **no** |
| owner's recording (11) | 170 ms | 86 ms | **1.96×** | **no** |
| owner's recording (14) | 678 ms | 95 ms | **7.15×** | **no** |
| owner's recordings (10), (12), (13) | 5 / 75 / 69 ms | 7 / 75 / 71 ms | 0.78–1.00× | yes |

Two findings, not one:

* **It is free when it does not fire and up to 7.2× when it does** — and it fires precisely on the audio that is
  already hardest, which is where the owner is least willing to wait.
* **It is not deterministic.** Sampling above temperature 0 is stochastic, and repeated runs on the *same* audio
  returned different transcripts: `'Hey, what are you?'` / `'Hey, why are you?'`, and
  `'Welcome to the U.S.P.D. of Health.'` / `'Welcome to your studio, folks.'`. For a command system that is worse
  than a wrong answer — it is a wrong answer that cannot be reproduced, diagnosed or tested.

And it buys nothing measurable: over 46 synthesised commands in two voices, **resolution accuracy was identical
(91 %)** under both settings, and on the cases where the retry fired, neither transcript was correct.

`voice.stt_temperature` therefore defaults to `0.0`. `null` restores the previous behaviour.

## 5. Transcript accuracy on application names

`scripts/bench/stt_accuracy.py` synthesises commands for the applications **actually installed on this machine**
(197 in the catalog), decodes them, and then runs the transcript through `launch_phrases` + `AppCatalog.resolve_name`
exactly as production does. The measure is not word error rate but *did the right application resolve*.

| level | n | opened the right thing | p50 decode |
|---|---|---|---|
| normal | 46 | **91 %** | 90 ms |
| heavily degraded (gain 0.22 + noise) | 46 | 4 % (default) / 11 % (`temperature=0`) | 117 / 95 ms |

The heavy degradation turned out to be useless as a test — the transcripts are unrelated sentences, so it measures
whether Whisper can hear noise, not whether the resolver works. Sweeping the level found the region that is actually
informative, where the transcript is *close* but wrong:

| gain / noise | resolved | what failed |
|---|---|---|
| 1.00 / 0.000 | 11/12 | `'OpenChat GPT'` |
| 0.60 / 0.010 | 12/12 | — |
| 0.45 / 0.018 | 11/12 | `'Open BS code.'` |
| 0.35 / 0.026 | 9/12 | `+ 'Open chat GPG.'`, `'Open work.'` |
| 0.30 / 0.035 | 8/12 | `+ "Open what's out."`, `'open cursive.'` |

### The one cleanly fixable failure: the verb glued to the name

At **normal level** — clean audio, nothing degraded — 2 of 23 commands failed this way, in **both** voices:

* `"Open ChatGPT."` → `'OpenChat GPT'`
* `"Open Chrome."` → `'OpenCrow.'`

The launch grammar requires a space after the verb, so these are not launch commands at all and go to the model.
De-gluing is now offered as an **extra, lower-priority reading**: the glued spelling is tried first, so a program
genuinely called *OpenOffice*, *OpenVPN* or *OpenRGB* still matches itself, and only then is the verb split off.
`'OpenChat GPT'` now opens ChatGPT with no model call.

De-gluing requires the sentence to still contain a space. An existing guard refuses `"opennotepad"` as not a launch
command, and it is right to — one word is no evidence a verb was run into a name. The single-word case that *was*
measured (`'OpenCrow.'`) does not resolve after de-gluing either (`"crow"` is three characters of phonetic key, below
`MIN_SOUND_KEY`), so requiring a space loses nothing measured and keeps the grammar's narrowest form intact.

### What was deliberately NOT changed

* **No LLM in the resolution stage.** The existing candidate/phonetic architecture is untouched.
* **No second speech-to-text engine, no speculative decode, no duplicated GPU transcription.**
* **No n-best.** faster-whisper exposes no lexically diverse alternatives at `beam_size=1`, and beam search was
  measured and rejected in an earlier milestone (2.4 s vs 1.2 s, beam not diverse). **No n-best support is claimed.**
* **No loosening of the phonetic classes.** `'Open BS code.'` would need b/v merged into one sound class *and* the
  alias table sound-keyed; `'Open work.'` and `'Open fire.'` (Clock) are below `MIN_SOUND_KEY`, which was set at 4
  by measurement because 3 matched "could"→Claude and "would"→Word. Both changes widen what can be *mistakenly*
  launched, and neither was justified by the evidence here.

## 6. Several applications in one command

`"open VS Code and WhatsApp"`, `"open Chrome, VS Code and File Explorer"`, `"open Projects and Windows Terminal"`.

```
transcript
  -> launch_targets()          split on commas and "and", after the existing verb/prefix grammar
  -> per target:  alias  ->  AppCatalog.resolve_name  ->  FolderCatalog.resolve_name
  -> Target(label, alternatives)      alternatives = alias then catalog, tried in order
  -> Agent.run_direct_targets()       each target independent, every call through _run_call
  -> kill switch -> risk level -> RiskGate.authorize -> execute -> telemetry
```

Two rules carry the safety of the whole feature.

**Resolve first, split second.** `"open Command and Conquer"` must not become two targets. The unsplit phrase is
resolved first and the split is used only if the whole name matches nothing installed — so no list of names
containing "and" has to be maintained anywhere.

**A split fragment may not prefix-match on one word.** This was a *real false launch* found in testing: with the game
not installed, `"open Command and Conquer"` split, `"command"` is a whole-word prefix of `"Command Prompt"`, and
V.O.I.D opened a console and said it could not find "conquer". A fragment the engine cut out of a sentence was never
offered as a complete name, so one word may not claim the start of a longer one. Two words still may
(`"open Opera GX and Notepad"` → *Opera GX Browser*), because two leading words are evidence rather than a guess.
The identity tiers (exact, spacing, phonetic) and the vendor tier are unaffected, so
`"open WhatsApp and Windows Terminal"` works.

**An instruction is not a name.** A list item containing any of ~70 instruction verbs (`delete`, `send`, `install`,
`close`, `shutdown`, …) means the sentence is not a list of applications, and **the whole sentence goes to the
model** exactly as a compound request does today. Splitting must never turn `"open chrome and delete my files"` into
a launch plus an "application I couldn't find" — that would silently drop an instruction.

### Partial success, and what is spoken

| command | opened | spoken |
|---|---|---|
| `open vs code and whatsapp` | 2 | **nothing** (a successful local action is silent) |
| `open vs code, whatsapp and notepad++` | **2** | *"I can't find notepad++ on this machine. If it is installed under a different name, tell me that name."* |
| `open notepad++ and someunknownthing` | 0 | *"I can't find notepad++ or someunknownthing on this machine…"* — one sentence, however many names |
| one name ambiguous | **0** | the existing clarification question. Launching some of a list and *then* asking would leave the owner unable to tell what happened |

"Opened VS Code. Opened WhatsApp." is never said. The mechanism is the existing one: a run with any unopened target
is not reported as `local_action`, which is exactly what makes the voice session speak it — and speak only it.

One distinction matters and is easy to get wrong (a mutation that removed it passed every other test): a name that
**resolved to nothing** is answered here, because the ordinary path searches the same catalog and cannot do better;
a target that **resolved but whose launch failed** still falls through to the ordinary agent, which may know another
way — so a protected target or a missing alias executable behaves exactly as it did before.

### Folders

Folder targets are resolved by a new, deliberately small index (`void/actions/folders.py`). The existing directory
search was measured first and is unusable here: `FileActions.find_dir` walks the whole allowed tree and takes
**13.7–24.6 s** with `allowed_roots = ["C:\\"]`, returning *ambiguous* for "Projects", "workspace" and "Downloads"
alike.

| | |
|---|---|
| scan: home + allowed roots, 3 levels, budget 1500, noise/system/profile dirs skipped | **258 folders in 285 ms**, once per 600 s TTL, prewarmed at start-up |
| a cached resolve | **3 µs** |
| matching | **identity only** — the same name, or the same name spaced differently. No prefix, no phonetic, no substring, nothing fuzzy |
| two matches | asks, named by location: *"Downloads in nanda, Downloads in OneDrive"* — "Downloads, Downloads" is not a question anyone can answer |
| policy | the file layer's own `_confine` decides every candidate; `open_path` confines the result again |

Depth 3 rather than 2 because OneDrive redirects Desktop and Documents, putting the owner's `Projects` three levels
down; depth 4 cost 421 ms and resolved nothing extra. An installed application always wins its own name over a
folder. Two Windows artefacts had to be excluded — the `Default`, `All Users` and `Public` profile templates carry a
full set of Downloads/Documents/Desktop and made every one of those names falsely ambiguous.

Found and fixed while testing against the real machine rather than a fixture: the scan was depth-first with path
de-duplication, and with overlapping roots (home *and* a drive root) that **silently pruned a whole subtree** — the
owner's own `Projects` was invisible. It is breadth-first now, so a directory is always reached by its shallowest
path and the visit budget gives up the deepest level rather than an arbitrary branch.

## 7. Performance, and provider calls

`scripts/bench/multi_app_bench.py`, 10 repeats, launches recorded rather than performed (starting programs measures
Windows, not V.O.I.D).

| case | sentences | p50 | p90 | opened | model calls |
|---|---|---|---|---|---|
| one application | 1 | 14.30 ms | 15.87 | 1 | **0** |
| **two in one sentence** | 1 | **7.00 ms** | 7.29 | 2 | **0** |
| two, one sentence each | 2 | 9.24 ms | 10.90 | 2 | **0** |
| **three in one sentence** | 1 | **19.71 ms** | 20.54 | 3 | **0** |
| three, one sentence each | 3 | 25.20 ms | 25.96 | 3 | **0** |
| partial (one name missing) | 1 | 7.20 ms | 10.36 | 2 | **0** |
| all names missing | 1 | 0.14 ms | 0.25 | 0 | **0** |
| folder + application | 1 | 4.01 ms | 4.24 | 2 | **0** |
| glued verb (`openchat gpt`) | 1 | 2.38 ms | 2.73 | 1 | **0** |

**Gemini calls: 0. Ollama calls: 0.** Across every case above.

One sentence naming three applications is *cheaper* than three sentences (19.7 ms against 25.2 ms): the catalogs are
resolved once and reused, and there is one run, one telemetry interaction and one dispatch instead of three. The
single-application row is higher than the two-application row only because `"chrome"` goes through the fixed alias
map, which scans PATH (~7–20 ms), while `"vs code"` and `"whatsapp"` come from the catalog.

Start-up, once, on a background thread: application discovery 413 ms, folder scan 262 ms.

### End to end, through the real speech-to-text

Commands synthesised locally, decoded by the production `FasterWhisperSTT` on CUDA, dispatched through the real
`Assistant`:

| said | heard | opened | spoken |
|---|---|---|---|
| "open vs code and whatsapp" | `'Open VS Code and WhatsApp.'` | 2 | silent |
| "open vs code, whatsapp and notepad plus plus" | `'Open VS Code, WhatsApp and Notepad++'` | **2** | *"I can't find notepad++ on this machine…"* |
| "open workspace and terminal" | `'Open workspace and terminal.'` | 2 (a folder and an application) | silent |

0 Gemini calls, 0 Ollama calls.

## 8. Tests

**2463 passed, 5 skipped, 2 xfailed, 0 failed.** Guard floor ratcheted 2335 → 2470.

| file | n | covers |
|---|---|---|
| `tests/test_multi_app.py` | 50 | wording variants, all three verbs, commas and "and", the trailing noun, articles; resolve-first-split-second; the one-word prefix rule; instruction words; paths / scripts / shell strings inside a list; every target opened; partial success; all-missing; ambiguity; a launch that fails; the fallback distinction; RiskGate confirmation over the whole list; a denied target; the kill switch; telemetry counts with no text; folders, and an application winning its own name |
| `tests/test_folders.py` | 49 | deny-by-default without a confinement check; a refused path; a raising policy; identity matching; everything short of identity; multi-component paths; ambiguity and its cap; bad query types; breadth-first traversal; depth; the visit budget; noise/system/profile skips; an unreadable directory; the TTL; `invalidate`; a folder created later; concurrent scans; index publication order; roots |
| `tests/test_stt_reliability.py` | 36 | a single temperature is passed; the default; `null`; the other decode options; the VAD retry keeping determinism; the config knob including malformed values; the glued verb, its priority, the one-word refusal, and that it cannot express anything the grammar refuses; `chars` in the schema and in the session |

**Mutation testing: 17 deliberate breakages, 17 caught, 0 escaped.** Two escaped on the first pass, and both were
weaknesses in my tests rather than in the code — nothing opened plus a failed launch had no test, and the folder
clarification's wording had none. Both are now pinned.

## 9. Security

Nothing here weakens authorisation, and nothing was made faster by skipping a check.

* **Every call still goes through `Agent._run_call`** — kill switch, per-call risk level, `RiskGate.authorize`,
  tainting, telemetry. `run_direct_targets` adds a loop around that funnel, not a path past it.
* **Confirmation semantics are preserved, and checked *before* anything runs.** If any target's tool would need the
  owner's confirmation, the fast path executes **nothing** and the ordinary loop owns the confirmation — so a list is
  never half-done and then deferred. A denial is final and reported, never retried through another path.
* **Multiple actions never relax anything.** There is no "batch" authorisation: N targets are N authorisations.
* **No shell, ever.** The fast path can emit exactly two tools — `launch_app` and `open_path` — with arguments the
  *engine* chose: an alias key, a catalog `app_id`, or an absolute path from the folder index. Never transcript
  text. Verified structurally, not just by behaviour: `void/actions/folders.py` contains no `subprocess`, `Popen`,
  `startfile`, `shell=True` or authorisation path, and an existing guard proves `void/core/fast_path.py` imports no
  process, path or security module at all — which is why the folder question's location is extracted with a string
  split rather than `pathlib`.
* **The list cannot express more than a single command could.** Every item passes the same character, connector and
  extension rules, so a path, drive letter, URL, glob, redirection or shell metacharacter is refused in a list
  exactly as it is alone (8 parametrised cases).
* **Protected roots and allowed roots still decide.** The folder index holds no policy: it is handed the file
  layer's `_confine`, and with no confinement callable it indexes **nothing** — deny-by-default, pinned by a test.
  `open_path` confines the path a second time when it runs. Administration consoles and script hosts stay excluded,
  from any position in a list.
* **Nothing new is logged.** `route` gained two integer counts (`targets`, `missing`) and `stt` gained `chars` — all
  counts. The schema allowlist would drop a string, and a test asserts no application name, folder name or
  transcript reaches telemetry. No API key is used by any benchmark here; the accuracy work used locally synthesised
  audio and the owner's existing local recordings, which `.gitignore` already excludes. Nothing was uploaded.
* 353 security, risk, protected-root, kill-switch and fast-path tests pass.

## 10. Remaining bottlenecks

1. **Ollama generating while the owner speaks: 5.1×.** Not the cause of the observed tail, but a real hazard if the
   local model ever becomes the primary provider. It is a scheduling problem, not a speech-to-text one.
2. **The unexplained part of the tail.** Cold start, the retry and clock state do not fully account for a 2.5 s
   decode of 3.9 s audio on a warm process. `stt.chars` is now recorded so the next week of the owner's own use can
   settle whether those decodes produced long or looping transcripts.
3. **Device power state: +105–285 ms** on the first decode after idleness. Nothing in V.O.I.D can fix this without
   changing the machine's power settings, which is out of scope.
4. **Endpointing, ~0.45 s**, still the largest single component of the user-perceived wait, evidence-bound across
   two earlier milestones.

## 11. Limitations

* **One machine, one voice.** Accuracy is measured on SAPI-synthesised speech, which has its own artefacts:
  `"Open Chrome."` renders as something Whisper hears as `'Crow'` even at full volume, which a human speaker
  probably would not produce. Synthetic audio is a good proxy for *latency* and for *resolution logic*, and a weak
  one for real acoustics.
* **The degradation model is additive noise and attenuation**, not reverberation, distance or a real room.
* **The GPU clock finding rests on 3 idle pairs** plus a between-run comparison. The direction is clear and the
  magnitude is measured, but the coefficient is not.
* **The Ollama exoneration is an argument from absence**: the telemetry records no local-provider call near the slow
  decodes. If something *other* than V.O.I.D was using the GPU at those moments, the logs cannot show it — which is
  part of why GPU state is now worth recording, and why only `chars` was added rather than a guess.
* **Folder resolution is bounded by construction** and will miss a folder deeper than 3 levels, or one whose name
  the owner says differently from how it is spelled. It misses rather than guesses, and the model still handles what
  it cannot settle.
* `find_dir`'s 13.7–24.6 s was measured with `allowed_roots = ["C:\\"]`. A narrower root would be faster; the point
  stands for this machine as configured.

## 12. Recommended next milestone

**Endpointing is now the whole wait.** For a local command the engine's own work is 7–20 ms, speech-to-text is
~200 ms warm, and the remaining ~0.45 s is the endpointer waiting for silence. Two earlier milestones concluded
amplitude-based endpointing could not be beaten without truncating speech — but both measured a *fixed* timeout.
The unexplored option is making it **adaptive to what has already been heard**: a capture that has already resolved
to a complete, unambiguous command (`"open chrome"`) does not need the same trailing-silence budget as one that is
still mid-phrase. That is a decision the existing resolver can already make, deterministically, with no new model
and no speculative decode.

Second, smaller: **record GPU state in telemetry** (utilisation and SM clock, sampled cheaply, not per decode via
`nvidia-smi`). This milestone had to stop the runtime and rebuild the conditions to answer a question a few integers
would have answered from the owner's own use.
