# Failure recovery after a Gemini 503 — investigation and outcome

Date: 2026-09-23 · Base commit 1b1af96 (working tree, uncommitted)

## Reported symptom

> "open Opera GX" (~1 s, fine) → "explain ARP" (failed) → "open WhatsApp" (failed) → "open WhatsApp" (failed) →
> "open Opera GX" (failed), each time: *"That task failed. Please check the command line for more details."*

Working hypothesis in the report: **a failed reasoning task poisons the voice command loop**, so later deterministic
commands fail too.

## Finding: the poisoning hypothesis is FALSE

It was not reproduced at any layer, and the runtime's own records contradict it.

**1. The runtime recovered after every failure.** `~/.void/void.log` shows, after each failed dispatch:
`VOICE_STATE idle` → `WAKE_ARMED` → `AUDIO_FRAMES_RECEIVED … subscribers=1` → `WAKEWORD_PROCESSING_ACTIVE
inferences=17-18 over≈5 s`. Wake, capture and endpointing kept working at full cadence throughout.

**2. Every task was left cleanly `failed`** in `tasks.sqlite` — none stuck `running`, none with a `pending` payload.

**3. Controlled reproduction of the exact sequence, twice, at two layers** (deterministic fake provider that always
raises the 503 the SDK raises, so no quota was used):

| | A "Open Opera GX" | B "Explain ARP" | C "Open WhatsApp" | D "Open Opera GX" | E "Open File Explorer." |
|---|---|---|---|---|---|
| `Assistant.run` | completed, 0 LLM, 34.8 ms | **failed**, 3 LLM | failed, 3 LLM | **completed, 0 LLM, 2.8 ms** | completed, 0 LLM |
| real `VoiceSession` | completed, state `idle` | **failed**, state `idle` | failed, state `idle` | **completed, 3.5 ms, state `idle`** | completed, state `idle` |

The session returned to `idle` after every turn, the generation advanced normally, the microphone was never left open,
and the fast path kept working *immediately after* two consecutive failures.

**4. The fast path still worked on the live machine** while the user was reporting the failure: `"Open Opera GX"`
→ fast path, 0.28 ms.

## What actually happened

The task store holds the real transcripts. Reading them explains every reported failure without any shared state:

| # | User said | STT produced | Route | Why it failed |
|---|---|---|---|---|
| 1 | open Opera GX | `Open Opera GX` | fast path | ✅ completed |
| 2 | explain ARP | `Explain ARP` | agent → Gemini | **503 UNAVAILABLE** (provider, Problem A) |
| 3 | open WhatsApp | `open WhatsApp` ✔ correct | agent → Gemini | **WhatsApp was not in the app catalog** → fast-path miss → 503 |
| 4 | open WhatsApp | `open WhatsApp` ✔ | agent → Gemini | same |
| 5 | open Opera GX | **`Open all projects.`** ✗ misheard | agent → Gemini | not a known app → 503 |

So the later commands did not fail *because* the reasoning command failed. They each independently missed the fast
path — #3/#4 because of a real catalog gap, #5 because STT misheard the phrase — and then hit the same Gemini outage
that was failing everything needing the model. Two unrelated causes wearing one generic error message.

Gemini's state during the session (from `perf.jsonl`): **21 of 32 LLM calls failed, all `ServerError` (503)**.

## Fix: Microsoft Store apps are now discoverable and launchable

The one genuine V.O.I.D defect behind the report. `AppCatalog` discovery reads App Paths, Start-Menu `.lnk` files and
a few PATH aliases. A Store (MSIX/UWP) app is none of those — it has no `.exe` and no `.lnk` — so **51 installed apps
on this machine, WhatsApp among them, were invisible**. "Open WhatsApp" could never fast-path, and fell to the model.

* `RealWindowsBackend._store_apps()` enumerates the shell `AppsFolder` namespace through the **pywin32 stack the
  backend already uses** (no new dependency), yielding `{"name", "kind": "uwp", "target": <AppUserModelID>}`.
  Read-only: nothing is installed, changed or downloaded.
* Store entries are added **last, and never under a name a path-based source already claimed.** A duplicate name would
  turn an exact match into an ambiguous one and silently stop that command fast-pathing — verified: **0 duplicate
  names introduced**, and `"Open Opera GX"` still resolves to the `.lnk` entry exactly as before.
* `AppCatalog.revalidate()` checks a Store entry by **id shape** (there is no path to stat); everything else still
  stats its target.
* Launch: `explorer.exe shell:AppsFolder\<AUMID>` — two fixed argv elements, **no shell string**, mirroring the
  existing `.lnk`/`.exe` launchers.
* `is_app_user_model_id()` accepts only `Publisher.App_hash!Entry`, so a hostile or malformed catalog entry cannot
  carry a space, quote, path separator, `&` or a second argument into the launcher. Checked at discovery, at
  revalidation and again immediately before launch (fail closed).

Security is unchanged in kind: the argument is still an **engine-chosen catalog `app_id`** resolved from the OS shell
namespace — never speech, never model text — and it still passes RiskGate and `launch_app` validation. The
protected-*location* check is a filesystem check and does not apply to an id with no path; the id-shape check takes
its place there.

## Before / after

| | Before | After |
|---|---|---|
| "Open Opera GX" | fast path, ~1 s | **unchanged** — fast path, 0 LLM (58.7 ms incl. first catalog build; 0.28 ms warm) |
| "open WhatsApp" | catalog miss → Gemini → 503 → failed | **fast path, 0 LLM, 9.3 ms** — verified by stopping WhatsApp and watching the production path start it (new pid) |
| Store apps visible | 0 of 51 | 51 |
| catalog entries / build | 160 / 0.049 s | 211 / 0.386 s (once per process, first catalog-resolved command only) |
| after a provider failure | *(believed poisoned)* | proven clean: task `failed`, session `idle`, next fast command works |

## What was deliberately NOT changed

Gemini's 503 behaviour, the retry policy, provider selection/cooldown, the fast-path grammar and prefix matching,
RiskGate, CapabilityEngine, memory, the wake CPU fix (runtime re-verified at **0.38 cores**). Problem A is untouched
and **not fixed** — Gemini 3.8 Flash still returns 503 under demand.

## Regression tests added

* `test_a_failed_reasoning_task_does_not_poison_the_next_fast_command` — parameterised over 503 / 429 / 500 / 504:
  fast → provider failure → fast → fast, asserting the later launches complete with **zero** provider calls.
* `test_a_provider_failure_is_a_failed_task_not_an_escaping_exception`
* `test_a_failed_task_is_left_failed_not_running_and_is_not_reused`
* `test_an_unavailable_provider_does_not_stop_a_fast_command` — a launch must not depend on the cloud at all.
* `test_the_voice_session_returns_to_idle_after_a_provider_failure_and_still_dispatches` — the same sequence through
  the real `VoiceSession`, asserting `idle` after every turn and that the microphone is never left open.
* Store apps: strict id-shape matrix (spaces, quotes, separators, `&`, empty, non-string), revalidation by id,
  no-shell AppsFolder launch, malformed id refused before any launch, and discovery never shadowing an existing name.

Both fixes were mutation-checked: disabling the fast-path route fails 6 recovery tests; loosening the id check fails
10 Store-app tests.

## Remaining issue (unchanged, documented)

**A 503 does not trigger fallback to Ollama within a run.** A 503 is not credential-specific, so the provider re-raises
it, the agent retries it up to 3 times on the same provider, and the task fails; Ollama is only selected on a *later*
command once credentials are cooled (which 503s never do). During this session `local` was selected 3 times and
`gemini` 10. Changing that is a provider-selection/failover change, explicitly out of scope here — it is the natural
next milestone, and it is what would have turned "explain ARP" into a slower-but-successful local answer instead of a
failure.
