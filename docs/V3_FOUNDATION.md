# V.O.I.D V3 — the orchestration foundation

2026-10-02. What was built, what it decides, what it deliberately cannot do, and what is still missing.

Companion to `workspace/VOID_V3_Main_Blueprint_and_Architecture.md` (the intended architecture) and
`docs/V2_DOMAINS.md` (the capability layer this sits on). The blueprint is the source of truth for
direction; this document records what the repository actually contains.

Baseline before this work: commit `8455a99`, **3149 tests passing, 0 failing**. After: **3325 passing, 0
failing**, guard floor raised 3157 → 3333.

---

## 1. The one-line summary

V2 answers *"what can V.O.I.D do, and may it do this?"* — tools, RiskGate, the kill switch, the single
`Agent._run_call` funnel. V3 adds the layer that answers *"given this goal and the computer's current state,
which of those capabilities should run, in what order, and did it actually work?"*

```
goal → intent → observe → route → authorize → act → verify → adapt → complete
```

The new layer is `void/orchestration/`. It sits **above** the capability layer, not beside it.

---

## 2. What was built

| Module | What it owns |
|---|---|
| `events.py` | The V.O.I.D-native interaction model: 15 semantic task events, an append-only bounded log |
| `commands.py` | Deterministic control semantics — stop speaking / pause / resume / cancel / modify |
| `routes.py` | The route model, scoring, the resolver, and providers over existing V2 systems |
| `apps.py` | The application registry: installed vs running vs preferred vs last-observed |
| `verify.py` | Capability-aware verification — structured state first, honest when it cannot check |
| `replan.py` | Task modification and bounded failure recovery |
| `trace.py` | V.O.I.D's observability vocabulary, mapped onto OpenTelemetry spans |

Plus an **extension** (not a replacement) of `void/core/task.py`: three new statuses and one new persisted
column for V3 task state.

### Three rules that hold across the package, each tested

**Orchestration authorizes nothing.** A route is a proposal, a plan is a proposal, a verification result is
an observation, a modification is an instruction about work. Every attempt still goes through
`Agent._run_call` — kill switch, then the tool's own `effective_risk`, then `RiskGate`. The security review
asserts no orchestration module references `RiskGate`, `.authorize(`, `requires_confirmation`, `confirm_fn`,
`kill_switch` or `raise_if_engaged`.

**Orchestration is not a second capability.** The registry reads the existing `AppCatalog`; routes execute
existing tools; verification observes through the existing confined `FileActions` and window layer. Asserted
live: **no registered tool has a handler implemented under `void/orchestration`**, and the module does no
discovery of its own (`winreg`, `glob`, `os.walk`, `listdir` all absent).

**Deterministic where it can be.** Route scoring, state transitions, command classification and
verification are pure functions over observed state. The model is consulted for semantic interpretation and
planning — never to decide whether a tab exists, whether an application is installed, or whether the owner
said "stop".

---

## 3. Control semantics — the gap that was closed

**The defect.** A bare "stop" spoken while V.O.I.D was talking did not match the kill switch's full phrase
("VOID, STOP EVERYTHING"), so it fell through to the ordinary command path and *the model* decided what it
meant. That is the wrong layer: whether the owner wants silence or wants the work abandoned is a control
signal, and getting it wrong is expensive in both directions — cancelling an hour of work because someone
wanted quiet, or talking over someone who asked for quiet.

Five intents are now classified **before any model sees the words**, in `Assistant.run`, ahead of the memory
pre-step and the fast path:

```
STOP_SPEAKING   be quiet now; the task is untouched
PAUSE_TASK      stop working, keep everything, resumable
RESUME_TASK     carry on from where you were
CANCEL_TASK     abandon the work   ← the only destructive one
MODIFY_TASK     "instead of that, ..." → the replanner
```

**Context decides, not just words.** `ControlContext` is read from *real state* — the TTS backend's own
`is_speaking`, the task store's statuses — never inferred from the utterance.

| Said | While speaking | While working | Idle |
|---|---|---|---|
| "stop" | STOP_SPEAKING | PAUSE_TASK | STOP_SPEAKING |
| "stop talking" | STOP_SPEAKING | STOP_SPEAKING | STOP_SPEAKING |
| "cancel that" | CANCEL_TASK | CANCEL_TASK | CANCEL_TASK |

**A bare "stop" can never cancel, from any state.** The asymmetry is deliberate: a wrongly-paused task costs
a word to resume, a wrongly-cancelled one may cost everything. Cancelling needs an unambiguous phrase. This
is asserted over 11 bare-stop phrases × 5 contexts in both the test suite and the security review.

**Whole-utterance matching.** "Stop the music", "don't stop", "cancel my subscription", "pause the video",
"continue reading the file to me" are work, not control signals — 10 such cases are tested.

**The kill switch is untouched.** "stop everything" cancels a *task*; halting V.O.I.D itself still requires
the full phrase, and `kill_switch.handle_command("stop everything")` returns False. Asserted.

---

## 4. The route resolver — the blueprint's headline example

A **route** is an inspectable, scored, executable way of reaching a goal. Scoring is a pure function, so
"why did it pick that?" is answerable rather than a matter of reading control flow.

Preference order, most significant first:

1. **Reuses existing state** — a separate and stronger signal than kind, so a browser route that reuses a
   session beats a native route that starts something new
2. **Route kind** — the blueprint's ladder: `EXISTING_STATE → STRUCTURED → NATIVE_APP → BROWSER →
   DESKTOP_UI → VISION → INPUT_FALLBACK`
3. **Lower risk** (a usability preference — never a security decision; RiskGate still decides)
4. **Higher reliability**, then **lower latency** — correctness before speed
5. **Fewer calls**, then id — so the order is total and stable

Validated against the blueprint's own example:

```
"open gmail", with a Gmail tab already open in the preferred browser
  → selected: existing_state  "switch to the gmail tab already open in Opera GX"
  → reuses_existing: True
  → candidates: [existing_state 0.2s, native_app 3.0s]

"open gmail", with no tab open
  → selected: native_app  "launch a browser"

"open gmail", with no browser adapter installed
  → ROUTE_UNAVAILABLE route-… needs ['browser']   (dropped BEFORE selection)
  → selected: native_app
```

That last one matters: a route whose adapter is absent is dropped *before* it can be chosen, so the owner
never sees a selection that then fails at execution.

**Providers are adapters over existing V2 systems**, not new mechanisms: `FastPathRoutes` wraps
`void.core.fast_path` (keeping its catalog matching, de-gluing and multi-target handling), and
`ExistingTabRoutes` proposes tab reuse. A broken provider is isolated — one failing source cannot stop the
others answering.

**Ambiguity is reported, not guessed.** Two routes that score identically *and differ in kind* set
`Resolution.ambiguous`, which is the blueprint's "ask the user rather than guessing".

---

## 5. Application registry

Five things the blueprint asks V.O.I.D to distinguish, kept apart because they are different questions:

| | Source |
|---|---|
| installed | the existing `AppCatalog` |
| currently running | the window/process list, with a 5 s TTL |
| previously observed running | a record kept over time |
| preferred | the owner's config, as **policy data** not prompt text |
| available | installed **and** has a usable launch route |

It answers the blueprint's questions directly — `is_installed`, `is_running`, `launch_route`,
`preferred("browser")` — and against the real machine reports 60 installed, 4 running.

**Identity, not names.** Entries are keyed on the catalog's `app_id`. `launch_route()` returns
`{app_id, name, kind, running}` and deliberately **no path** — the launch capability resolves an id against
the catalog itself, which is what keeps a caller-supplied path from becoming an execution target.

**Preferences are a closed set** (`browser`, `editor`, `terminal`, `music`, `mail`, `messaging`). An
unrecognised preference is ignored rather than becoming a free-form channel from config into routing.

**Running state is refreshed, not trusted forever** — an application closed two minutes ago must not still
look running, or the resolver would choose "activate the existing window" for a window that is gone.

---

## 6. Verification

The assumption this breaks: `action succeeded == task succeeded`. A tool returning ok means the call did not
raise — not that the application is in front of the owner, not that the file is usable.

**Structured first, pixels last — and usually not at all.** Methods, in order of preference:
`WINDOW_STATE`, `FILESYSTEM`, `PROCESS_STATE`, `BROWSER_STATE`, and `TOOL_RESULT` as a *labelled-weak*
fallback. Asserted against the code: the verifier contains no `screenshot`, `ImageGrab`, `cv2`, `ocr` or
`pyautogui`.

**`UNVERIFIED` is a first-class outcome**, deliberately distinct from `FAILED`: "I could not confirm this"
and "this did not work" lead to different decisions. A path outside the allowed roots is UNVERIFIED, not a
failure claim — and that is also how verification stays inside confinement: it observes through the
*confined* `FileActions`, so it is not a way around allowed roots. Asserted live.

**Artifacts need a plausible size.** A generator that writes a valid-but-empty document "succeeds" at the
tool level and produces something useless, so a file under 64 bytes is reported FAILED with its size rather
than passed because it exists. Measured: a real 500-byte file verifies in 1.4 ms.

---

## 7. Task state and replanning

**Extended, not duplicated.** The V2 `Task` already had the id, goal, status set, step ledger,
`current_step`, history, pending-for-owner slot, timestamps and SQLite persistence the blueprint asks for,
and was already separate from memory. Building a second task engine beside it would have been the exact
duplicate-system mistake the blueprint warns against.

Added: three statuses (`PLANNING`, `REPLANNING`, `VERIFYING` — all resumable, none terminal) and one
additive column holding `intent`, `route`, `applications`, `artifacts`, `checkpoints`, `verifications`,
`modifications`, `failures`, `replans`.

Nothing was renamed: V2 statuses are persisted in the owner's existing database and asserted in V2 tests.
`PENDING` remains the blueprint's CREATED; `AWAITING_CONFIRMATION` remains WAITING_FOR_USER. **A database
written before this change still loads**, with `v3 == {}` — tested, including malformed state failing safely
to empty.

### Modification preserves completed work

```
ledger before:  succeeded  succeeded  executing  pending
"instead of that, add a costs section"
ledger after:   succeeded  succeeded  superseded superseded     status → REPLANNING
                └─ preserved: 2 ─┘    └─ invalidated: 2 ─┘      current_step → 2
```

`SUPERSEDED` is a distinct status so "we chose not to do this because the plan changed" never reads as
"this failed". **Completed work is never undone** — reversing a side effect is a consequential operation with
its own authorization, and silently rolling back a sent message would be far worse than leaving it. A
terminal task cannot be amended: "instead of that" after something completed is a new request.

### Recovery is bounded

| Failure | Replan? | Why |
|---|---|---|
| `ROUTE_FAILED` | yes | the mechanism failed; a different route may work |
| `TARGET_MISSING` | no | the thing does not exist; another route will not help |
| `DENIED` | **no** | routing around a refusal would be badgering the owner |
| `STOPPED` | **no** | the kill switch means stop |

Plus a hard `max_replans` (default 3) and an exclusion set, so a replan cannot propose the route that just
failed — that would be a retry in disguise. `classify_failure` *translates* the outcomes the V2 funnel
already produces rather than being a second classifier with its own opinions.

---

## 8. Observability

**The API only, no new dependency.** `opentelemetry.trace` is already present transitively;
`opentelemetry.sdk` is not. The API alone is enough to emit spans — with no SDK configured it returns a
no-op tracer, so instrumenting costs nothing. Whoever wants to collect traces configures an SDK in their own
deployment, which is the right division: V.O.I.D should not choose anyone's telemetry backend.
`requirements.txt` is unchanged, and a test asserts that.

**`void/perf` is not replaced.** V.O.I.D's existing privacy-by-construction allowlist stays the local
record; this is an additional mapping for distributed tracing.

**Attributes are allowlisted, like perf events.** A span travels further than a log line, so there is no
attribute for a goal's text, a transcript, tool arguments, page content, screen content or memory —
verified by asserting no attribute name contains any of those words. Unknown keys are dropped. A span
**never changes control flow**: a broken or missing tracer leaves the body running, and a defect found
during this work (`_tracer()` raising *outside* the try, which turned a telemetry problem into a control-flow
problem) was fixed so there is exactly one `yield` on every path.

---

## 9. Measured

```
control classification (pure)                 0.004 ms p50
task modification (pure)                      0.003 ms p50
route resolution, tab reuse                   0.050 ms p50
route resolution, real fast path              0.050 ms p50
application registry (cached running)         0.122 ms p50
application registry (forced refresh)         0.354 ms p50
verification: app present (real windows)      0.229 ms p50
verification: artifact (real 500-byte file)   1.403 ms p50
control command end to end ("stop")           0.250 ms p50
```

All of it deterministic and **zero model calls** — which is the blueprint's "do not invoke the LLM when
deterministic state is sufficient", as a measurement rather than an intention.

---

## 10. Not built, and honestly so

The blueprint's 🟢 list has 18 items. This pass built the orchestration core and the two things that needed
no new dependency. The rest is **not** implemented, and no stub pretends otherwise.

### 🟢 Next — the adapter boundaries exist, the adapters do not

* **Browser abstraction + Playwright adapter.** `RouteKind.BROWSER` and `ExistingTabRoutes` are in place and
  the resolver already prefers tab reuse — but `requires={"browser"}` is never satisfied, because no browser
  capability is registered. `playwright` is not installed. `ExistingTabRoutes.ACTIVATE_TOOL` names
  `activate_tab` by string precisely so the provider does not import an adapter that does not exist yet.
  **Until that adapter lands, the Gmail tab-reuse path is validated only against injected `WorldState`, not
  against a real browser.**
* **Desktop abstraction + Windows UI Automation.** V2's `ComputerActions` already covers windows, activate,
  close, state and the new active-window/window-state tools. Controls, menus, dialogs and the accessibility
  tree need UIA; `uiautomation` and `comtypes` are both absent.
* **Perception abstraction + screen understanding.** No screen capture source is installed (`mss`, `PIL`
  absent) and no OCR (`pytesseract` absent). Note this is *achievable without new dependencies* — pywin32
  (present) can capture via GDI and the existing `void/vision` layer already encodes JPEG and has a
  validated Gemini image path — so this is the cheapest remaining 🟢.
* **Artifact engine.** No DOCX/PPTX/PDF/XLSX generation. `note_artifact` and `artifact_created` exist to
  record and verify one once something produces it.
* **Entity navigation** ("open Rushi's chat"). Needs the desktop layer to resolve a contact inside an
  application.

### 🟡 Later
AG-UI adapter (the internal event model is deliberately protocol-free so one can be added without touching
the task engine); A2UI; A2A; research engine; resource manager beyond V2's read-only telemetry; local
semantic vision.

### 🔴 Deliberately not built
A browser engine, a Chromium replacement, a Playwright replacement, a UIA replacement, custom telemetry
infrastructure, a multi-agent swarm, unrestricted shell or admin, credential access, autonomous offensive
cybersecurity, remote camera control.

---

## 11. Acceptance criteria status

| Blueprint criterion | Status |
|---|---|
| 1. "Open YouTube" → route → preferred browser → verify | **partial** — resolver and verification work; the Playwright leg is missing |
| 2. "Open Pinterest" → reuse existing tab | **partial** — reuse is selected correctly from `WorldState`; no real tab source yet |
| 3. "Open Rushi's chat" → entity navigation | **not built** — needs the desktop layer |
| 4. "What is this on my screen?" | **not built** — needs screen capture |
| 5. Active task modification | **done and validated** end to end through the real `Assistant` |
| 6. TTS interruption / "stop" | **done and validated** end to end, including that it never cancels |

---

## 12. How to re-check

```bash
python -m pytest -q                                      # 3325 passed, 0 failed
python -m pytest -q tests/test_v3_orchestration.py tests/test_v3_integration.py
python scripts/check_test_guard.py --junit junit.xml      # floor 3333
python -m compileall -q void
```
