# V.O.I.D — V1 Architecture Review

**Date:** 2026-08-29
**Reviewed by:** Implementation Engineer (Claude)
**Basis:** The actual files present in the project on the machine, staged and read directly (confirmed byte-identical to the committed source). Test results are from running the actual suite. This document describes only what exists right now — planned functionality is labelled as such and never described as working.

**Status legend used throughout:**

- **IMPLEMENTED** — code exists and works (verified by tests and/or on-machine checks).
- **PARTIALLY IMPLEMENTED** — code exists and works for some paths; a meaningful part is missing or unverified.
- **SCAFFOLDED** — structure/interface exists but the behaviour is not proven or is a placeholder.
- **NOT IMPLEMENTED** — does not exist in the codebase.

---

## 1. Current project directory tree

```
V.O.I.D/
├── .gitignore
├── README.md
├── requirements.txt
├── config/
│   └── default_config.yaml
├── examples/
│   └── demo_offline.py
├── void/                     # the application package
│   ├── __init__.py
│   ├── __main__.py           # `python -m void`
│   ├── app.py                # Assistant facade (wires everything)
│   ├── cli.py                # command-line interface
│   ├── config.py             # YAML config loader
│   ├── actions/
│   │   ├── __init__.py
│   │   ├── base.py           # Tool / ToolResult types
│   │   ├── files.py          # file search/read/write/delete
│   │   ├── apps.py           # launch apps / open paths
│   │   └── registry.py       # tool registry + dispatch
│   ├── core/
│   │   ├── __init__.py
│   │   ├── agent.py          # the agent loop
│   │   ├── task.py           # Task model + SQLite checkpoint store
│   │   └── kill_switch.py    # emergency stop
│   ├── providers/
│   │   ├── __init__.py
│   │   ├── base.py           # LLMProvider interface + neutral types
│   │   ├── gemini_provider.py
│   │   ├── local_provider.py # Ollama HTTP
│   │   └── registry.py       # provider selection/fallback
│   ├── security/
│   │   ├── __init__.py
│   │   ├── secrets.py        # OS keyring wrapper
│   │   └── risk.py           # risk levels + confirmation gate
│   └── ui/
│       ├── __init__.py
│       └── widget.py         # PySide6 circular widget
├── tests/                    # 41 tests, all passing
│   ├── __init__.py
│   ├── conftest.py
│   ├── helpers.py            # scripted FakeProvider
│   ├── test_agent.py
│   ├── test_files.py
│   ├── test_kill_switch.py
│   ├── test_providers.py
│   ├── test_risk.py
│   └── test_task.py
├── verify/                   # laptop verification tooling + outputs (not app code)
│   ├── laptop_check.py
│   ├── check_gemini_only.py
│   ├── list_models.py
│   ├── run_verify.ps1 / .bat
│   ├── run_gemini.bat
│   ├── run_listmodels.bat
│   ├── verify_report.txt / .json
│   ├── models_report.txt
│   ├── gemini_report.txt
│   ├── setup_log.txt
│   └── widget_render.png
└── .venv/                    # local virtual environment (~830 MB, gitignored)
```

Runtime state (created on first run, not in the repo): `~/.void/tasks.sqlite` and `~/.void/STOP`.

---

## 2. Overall architecture — IMPLEMENTED

V.O.I.D V1 is a single-process, local-first Python application arranged as an **agent loop** surrounded by replaceable layers. A goal in natural language is turned into a sequence of tool calls by an LLM "brain"; each tool call is screened by a risk gate and an always-checked kill switch, executed against the Windows machine, and the result is fed back to the brain until it produces a final answer. Every step is checkpointed to SQLite.

The layering is clean and genuinely decoupled:

```
CLI / PySide6 UI
      │
   Assistant (app.py) ── wires config, security, tools, providers, agent
      │
   Agent loop (core/agent.py)
      ├── LLMProvider (providers/*)   ← Gemini | Local(Ollama), selected by registry
      ├── ToolRegistry (actions/*)    ← file + app tools
      ├── RiskGate (security/risk.py) ← autonomy vs. confirmation
      ├── KillSwitch (core/kill_switch.py)
      └── TaskStore (core/task.py)    ← SQLite checkpoints
```

The design matches the agreed V1 intent: local-first, provider-replaceable, Windows-focused, autonomy-with-authority. It is a **vertical slice**, not the full vision.

---

## 3. Main components and their responsibilities

| Component | File | Responsibility | Status |
|---|---|---|---|
| Assistant facade | `void/app.py` | Build and wire all layers from config | IMPLEMENTED |
| Agent loop | `void/core/agent.py` | Plan→act→observe→repeat, retries, checkpoints | IMPLEMENTED |
| Task model + store | `void/core/task.py` | Task state + SQLite persistence | IMPLEMENTED |
| Kill switch | `void/core/kill_switch.py` | Emergency stop (in-process + file) | IMPLEMENTED |
| Provider interface | `void/providers/base.py` | Neutral message/tool/response types | IMPLEMENTED |
| Gemini provider | `void/providers/gemini_provider.py` | Cloud brain via google-generativeai | PARTIALLY IMPLEMENTED (text verified; tool-calling not verified live) |
| Local provider | `void/providers/local_provider.py` | Offline brain via Ollama HTTP | IMPLEMENTED (unverified on hardware) |
| Provider registry | `void/providers/registry.py` | Pick first available provider | IMPLEMENTED |
| File actions | `void/actions/files.py` | search/read/write/delete with confinement | IMPLEMENTED |
| App actions | `void/actions/apps.py` | launch apps / open paths | IMPLEMENTED |
| Tool registry | `void/actions/registry.py` | expose tool specs, dispatch calls | IMPLEMENTED |
| Risk gate | `void/security/risk.py` | classify risk, gate high-risk actions | IMPLEMENTED |
| Secrets | `void/security/secrets.py` | keyring wrapper | IMPLEMENTED |
| Config | `void/config.py` | YAML load + dotted access | IMPLEMENTED |
| CLI | `void/cli.py` | user commands | IMPLEMENTED |
| UI | `void/ui/widget.py` | circular desktop widget | PARTIALLY IMPLEMENTED (renders; full live-task path unverified) |

---

## 4. How a user command currently flows through the system

Traced from the actual code (`cli.py` → `app.py` → `core/agent.py`):

1. **Entry.** `python -m void "find my notes"` (or the UI input box). `cli.main()` routes a bare string to `cmd_run`.
2. **Assembly.** `Assistant(...)` loads config, builds the `KillSwitch` (with a `~/.void/STOP` file), `RiskGate`, `TaskStore` (`~/.void/tasks.sqlite`), the `ToolRegistry` (file + app tools), and the `ProviderRegistry`.
3. **Stop pre-check.** If a stop is already engaged, the run refuses until `clear-stop`.
4. **Task creation.** A `Task` is created with a system prompt + the user goal; saved (checkpoint #0).
5. **Provider selection.** `providers.select()` returns the first provider whose `available()` is true (Gemini first, then local).
6. **Loop** (`Agent._loop`, capped at `max_steps=12`):
   - Kill switch checked.
   - `provider.generate(messages, tools)` is called (with `max_retries=2` on transient errors).
   - If the response contains **tool calls**: an assistant message is recorded, then for each call the kill switch is re-checked, the **risk gate** authorises it, the tool runs, and a tool-result message is appended. The task is checkpointed. Loop continues.
   - If the response is **plain text**: it becomes the final answer; task marked COMPLETED; returned.
7. **Termination.** Reaching `max_steps` → FAILED; a stop → PAUSED (resumable); provider unavailable / unexpected error → FAILED. Every terminal state is checkpointed.
8. **Reporting.** CLI prints the status, result, and any note; the UI streams step events to its log panel.

**Important nuance:** the full loop is verified **offline** (scripted brain via `examples/demo_offline.py` and `test_agent.py`). The same loop driven by a **live Gemini** making real tool calls has **not** been executed end-to-end yet (see §5).

---

## 5. Gemini integration — PARTIALLY IMPLEMENTED

`void/providers/gemini_provider.py` implements the `LLMProvider` interface using `google-generativeai`:

- Lazy import and configuration; API key read from the OS credential store (never from config).
- `available()` returns true only if the SDK imports and a key is present.
- `_to_contents()` translates the neutral message list into Gemini `contents` (system → `system_instruction`; assistant tool calls → `function_call` parts; tool results → `function_response` parts).
- `_to_tools()` wraps tool specs as `function_declarations`.
- `_parse()` extracts text and/or `function_call` parts into the neutral `LLMResponse`.
- A fresh `GenerativeModel` is constructed per call with temperature/token limits from config.

**Verified:** a live `generate_content` call on `gemini-3.6-flash` succeeded (~2.3 s, returned "OK"). Key storage, authentication, network, model identifier, and the basic request path work.

**Model note (real finding):** the originally configured `gemini-1.5-flash` and the interim `gemini-2.5-flash` both return HTTP 404 on this API key ("no longer available to new users"); the API explicitly recommended `gemini-3.6-flash`, which is now the configured value.

**Not yet verified:** a live **tool-calling round trip** through Gemini (function_call → tool execution → function_response → final text). The translation helpers are unit-tested against fakes, but no real Gemini tool-call has been exercised. This is the single most important untested path for the "brain actually drives Windows actions" claim.

---

## 6. AI provider abstraction and local-model fallback

**Abstraction — IMPLEMENTED.** `providers/base.py` defines neutral `ToolSpec`, `ToolCall`, `LLMResponse`, and the `LLMProvider` ABC (`available()`, `generate()`). The agent speaks only these types; each provider translates to its own API. This delivers the "replaceable providers" principle cleanly.

**Selection/fallback — IMPLEMENTED.** `providers/registry.py` builds providers from config and `select()` returns the first whose `available()` is true, in order `primary` (`gemini`) then `fallback` (`local`). If none are available it raises `ProviderUnavailable`.

**Local model fallback — IMPLEMENTED (UNVERIFIED on hardware).** `providers/local_provider.py` is complete real code: it talks to Ollama's `/api/chat` over HTTP, translates messages/tools in OpenAI-ish shape, and parses tool calls. However, Ollama is **not installed/running** on the machine (connection refused during verification), so this provider has **never actually served a request**. Its tool-calling reliability is unproven — and small local models are historically weaker at tool calling, so this is a substantive unknown, not a formality.

---

## 7. Windows tool/action layer — IMPLEMENTED

`actions/base.py` defines `Tool` (name, description, JSON-schema params, handler, risk level) and `ToolResult` (ok/summary/data/error). `actions/registry.py` holds tools, exposes them to providers as `ToolSpec`, and dispatches calls by name (unknown tool and bad-argument cases return structured failures rather than crashing).

Tools currently registered:

- `search_files`, `read_file`, `write_file`, `delete_file` (from `FileActions`)
- `open_path`, `launch_app` (from `AppActions`)

**Verified on the machine:** `open_path` (via `os.startfile`) and `launch_app('calc')` both returned success and opened windows. App launching uses native OS calls (not screen automation), which matches the reliability-first intent, with a small alias map (cursor, vscode, notepad, chrome, …) and a shell fallback on Windows.

---

## 8. File operations and their safeguards — IMPLEMENTED (with policy gaps)

`actions/files.py`:

- **Path confinement:** every path is `expanduser`+`resolve()`d and must be equal to, or under, one of `allowed_roots` (via `is_relative_to`), else `PathNotAllowed`. Covered by tests, including the outside-root rejection.
- **Search:** filename substring or glob match, walks allowed roots, skips noise dirs (`node_modules`, `.git`, `AppData`, `$Recycle.Bin`, dotdirs, …), bounded by `max_results`.
- **Read:** capped at 200 KB, decoded UTF-8 with replacement.
- **Write:** creates parent dirs; refuses to overwrite an existing file unless `overwrite=True`.
- **Delete:** routed through `send2trash` to the Recycle Bin (recoverable). Hard delete is intentionally disabled in V1.

**Safeguard gaps (policy, not bugs):**

- **`allowed_roots` defaults to `~`** — the entire user home. The blast radius is broad by default.
- **`write_file` is risk MEDIUM**, and the confirmation threshold is HIGH, so **overwriting an existing file runs autonomously** (no human confirmation) as long as the model sets `overwrite=True`. Only deletion asks. See §9 and §23.

---

## 9. Risk / authorization system — IMPLEMENTED (conservative-by-omission)

`security/risk.py` defines `RiskLevel` (LOW<MEDIUM<HIGH) and `RiskGate`:

- Each tool declares a static risk: search/read/open/launch = LOW, `write_file` = MEDIUM, `delete_file` = HIGH.
- Config `security.confirm_at_or_above` = `high`, so only HIGH-risk actions require confirmation.
- When a confirmer is supplied (CLI prompt / UI dialog), a HIGH action asks the owner. When **no** confirmer is supplied (unattended), the gate **denies** high-risk actions (`deny_all`) rather than guessing — a good default. Tested.

**Consequences to be aware of:**

- With the default threshold, **file edits/overwrites (MEDIUM) are autonomous**. Only deletes are gated.
- Risk is **static per tool**, not contextual (e.g., overwriting a `.py` in a project vs. a scratch file are treated identically).

---

## 10. STOP / kill-switch implementation — IMPLEMENTED (authentication off by default)

`core/kill_switch.py`:

- Two signals: an in-process `threading.Event` and an on-disk `~/.void/STOP` file, so a stop can be triggered from another process (a second terminal, the UI button, and — in principle — later another device).
- Checked before every loop step and before every tool execution (`raise_if_engaged()` → `StopRequested` → task marked PAUSED and checkpointed).
- `handle_command()` engages on an exact (case-insensitive) match of the phrase `"VOID, STOP EVERYTHING"`.
- Optional PIN authentication via the credential store, gated by `kill_switch.require_pin`.

**Verified:** engage/clear works end-to-end from the CLI, and the file-based cross-process path and PIN path are unit-tested.

**Gaps vs. the stated principle ("highest priority… after proper authentication"):**

- `require_pin` defaults to **false**, so by default STOP is **unauthenticated** (anyone/anything that can write the file or type the phrase can stop it). Authentication exists but is off.
- Cancellation is **cooperative** — it prevents the *next* step/tool from starting; it cannot interrupt a syscall already in flight. Fine for the current fast, non-blocking tools; a limitation for future long-running ones.
- There is **no always-listening / voice** STOP; it is a CLI command, a UI button, or the stop file.

---

## 11. Task engine — IMPLEMENTED (single, synchronous)

`core/task.py` defines a `Task` (id, goal, status, messages, steps, result, error, timestamps) and status constants (PENDING/RUNNING/PAUSED/COMPLETED/FAILED). The agent creates, runs, and terminates tasks; `TaskStore` lists them and finds resumable ones.

**What it does not do (relative to the broader vision):**

- No **parallel/concurrent** tasks — the agent runs one task synchronously.
- No background scheduler or long-running daemon.
- No inter-task dependencies or delegation.

---

## 12. Checkpoint / recovery implementation — PARTIALLY IMPLEMENTED

- **Checkpointing — IMPLEMENTED.** The full task (including message history and step count) is written to SQLite after every step and at every terminal state. This is real, tested durability.
- **Recovery — PARTIALLY IMPLEMENTED.** Resumption exists but is **manual**: `python -m void resume <task-id>` reloads a task and continues the loop, and `TaskStore.resumable()` can list interrupted tasks. There is **no automatic** crash detection, no auto-resume on startup, and no watchdog. A crash leaves the task at its last checkpoint until a human resumes it.

---

## 13. Memory implementation — NOT IMPLEMENTED

There is **no memory subsystem** in the V.O.I.D codebase. The vision's "explicitly requested memories / saved workflows, exportable and deletable" does not exist as code. The only persistence present is the **task checkpoint store** (operational state, not user-facing memory).

(Note: this session used the assistant platform's own project-memory feature to record decisions across sessions — that is external tooling, not part of the V.O.I.D application.)

---

## 14. Configuration and secret storage — IMPLEMENTED

- **Config — IMPLEMENTED.** `config.py` loads `config/default_config.yaml`, deep-merges an optional gitignored `config/local_config.yaml`, and offers dotted access (`cfg.get("llm.gemini.model")`) plus derived `state_dir()` and `allowed_roots()`. Current values: primary `gemini` / fallback `[local]`; Gemini model `gemini-3.6-flash`; local model `llama3.1:8b`; `max_steps=12`, `max_retries=2`; `confirm_at_or_above=high`; `allowed_roots=["~"]`; `delete_to_recycle_bin=true`; `require_pin=false`.
- **Secrets — IMPLEMENTED.** `security/secrets.py` wraps `keyring` under service `"void"` (Windows Credential Manager backend). The Gemini key and stop PIN are stored/read here. Verified: the key is stored and read correctly, and is never written to config, logs, or output.

---

## 15. PySide6 UI — PARTIALLY IMPLEMENTED

`ui/widget.py` implements a frameless, always-on-top, draggable circular "V" orb with an input box, a streaming log panel, a red STOP button, and a Hide button. The agent runs on a `QThread` worker so the UI stays responsive; high-risk confirmations are marshalled to the UI thread via a `ConfirmBridge` (blocking the worker until the dialog answers); the STOP button engages the kill switch.

**Verified:** the widget **constructs and renders** correctly on the machine (captured to PNG — orb, input, log, STOP all present). **Not verified:** a full live task driven through the GUI worker thread (including a real confirmation dialog and a live provider). So the UI is proven as a rendered surface, not yet as an end-to-end interactive runtime.

---

## 16. Current CLI — IMPLEMENTED

`cli.py` provides: bare goal (`python -m void "…"` → run), `run`, `resume <id>`, `set-key gemini`, `set-pin`, `tasks`, `stop [--pin]`, `clear-stop`, `ui`. High-risk actions prompt `[y/N]`; provider-unavailable is handled with a clean message; results print status/result/notes. This is the most complete and directly usable surface today.

---

## 17. Testing structure and current test results — IMPLEMENTED

`tests/` uses pytest with a `conftest.py` (path setup) and `helpers.py` (a scripted `FakeProvider`). Coverage:

- `test_files.py` — search/glob, read, write/overwrite, delete-to-trash, confinement.
- `test_risk.py` — thresholds, deny-when-unattended, confirm-allows.
- `test_task.py` — save/load/upsert, resumable filtering.
- `test_kill_switch.py` — engage/reset, phrase match, cross-process file, PIN auth.
- `test_providers.py` — selection/fallback order, Gemini/Ollama message+tool translation, response parsing.
- `test_agent.py` — full loop with a fake brain: tool execution, checkpointing, high-risk denial, kill-switch pause, max-steps cap.

**Result (actual run):** `41 passed` in ~0.14 s.

**Coverage gaps:** all tests are offline/logic-level. There are **no** tests exercising live Gemini, live Ollama, real Windows launching, or the GUI (those are covered separately by the `verify/` scripts, which are one-shot manual runners, not part of the automated suite). No CI, no coverage metric.

---

## 18. Dependencies — IMPLEMENTED

From `requirements.txt`: `PyYAML`, `keyring`, `send2trash`, `google-generativeai`, `requests`, `PySide6`, `pytest`. Installed into a project-local `.venv` (Python **3.14.7**, ~830 MB). All installed successfully; the automated suite runs against them.

**Notes:** Python 3.14 is very new — fine here, but a constraint for contributors/tooling. `google-generativeai` is a fast-moving SDK and the Gemini translation is hand-rolled against it (see §24). No dependency pinning to exact versions (uses `>=`), so future installs may drift.

---

## 19. What is actually functional right now

- **Offline agent loop** with real file/app tools, risk gating, checkpointing, and stop — verified by the suite and `demo_offline.py`. **[IMPLEMENTED]**
- **Windows file operations** (search/read/write/delete-to-Recycle-Bin) with path confinement — logic tested; **os.startfile / app launch verified on the machine**. **[IMPLEMENTED]**
- **CLI** end-to-end for local operation. **[IMPLEMENTED]**
- **Gemini** plain connectivity/auth on `gemini-3.6-flash`. **[IMPLEMENTED, text-only]**
- **Kill switch, risk gate, task checkpoint store, config, secrets.** **[IMPLEMENTED]**
- **PySide6 widget** renders. **[IMPLEMENTED as a surface]**

---

## 20. What is only scaffolded / stubbed

- **Local (Ollama) provider** — full code, but **never run** (no server). Treat as unproven until exercised. **[IMPLEMENTED but UNVERIFIED — closest thing to "scaffolded" behaviourally]**
- **UI live-task runtime** — the worker-thread/confirmation path exists but is not proven end-to-end. **[PARTIALLY IMPLEMENTED]**
- There are **no empty stubs / placeholder functions** in the codebase; every module contains real logic.

---

## 21. What has NOT yet been implemented

- **Memory** (explicit save/recall of user memories and workflows). **[NOT IMPLEMENTED]**
- **Cross-device** laptop↔Android link and task routing. **[NOT IMPLEMENTED]**
- **Voice** input or output. **[NOT IMPLEMENTED]**
- **Autonomous background execution / parallelism / auto-recovery on crash / re-planning beyond the LLM's own choices.** **[NOT IMPLEMENTED]**
- **Live Gemini tool-calling round trip** (function_call → execute → function_response → answer). **[NOT VERIFIED]**
- **Sandboxing/isolation** for risky operations. **[NOT IMPLEMENTED]**
- **Device trust levels, authenticated remote commands, permission revocation.** **[NOT IMPLEMENTED]**
- **Audit logging** of actions. **[NOT IMPLEMENTED]**
- **Packaging / installer / autostart / tray daemon.** **[NOT IMPLEMENTED]**

---

## 22. Known technical limitations

1. Gemini **tool-calling** not yet exercised live — the core "brain drives Windows" path is unproven with a real model.
2. Local provider unproven; small-model tool-calling reliability is a real risk if Gemini is ever unavailable.
3. Cancellation is cooperative (cannot interrupt an in-flight syscall).
4. Single synchronous task; no concurrency or background daemon.
5. Recovery is manual (no auto-resume/watchdog).
6. File search is a live filesystem walk (no index) — acceptable now (bounded), slow on very large trees.
7. Hand-rolled Gemini translation is coupled to a fast-moving SDK.
8. No structured logging; observability is `print`/callback only.
9. `>=` dependency ranges allow silent drift; Python 3.14 is bleeding-edge.
10. Windows-first assumptions (with partial cross-platform fallbacks in `apps.py`).

---

## 23. Security concerns

1. **STOP is unauthenticated by default** (`require_pin=false`) — contradicts the "after proper authentication" principle. The mechanism exists; it is simply not enabled.
2. **Broad default scope:** `allowed_roots=["~"]` puts the entire home directory within reach of file tools.
3. **Autonomous overwrites:** MEDIUM-risk `write_file` (including `overwrite=True`) runs without confirmation under the default HIGH threshold. Combined with #2, the agent can overwrite files across the home directory without asking.
4. **Prompt-injection exposure:** tool results (including **file contents** read by `read_file`) are fed back to the LLM. A malicious or booby-trapped file could try to steer the model into destructive tool calls. Deletion is gated (HIGH→confirm), but **overwrite is not** — the realistic injection outcome today is unauthorised file overwrites within the home directory.
5. **No audit trail:** actions are not persistently logged, so after-the-fact review of what the agent did is limited to task message history.
6. Positives: secrets are correctly kept in the OS credential store and never printed; deletes are recoverable (Recycle Bin); unattended runs deny high-risk actions.

---

## 24. Architectural risks

1. **Unverified brain→action path (highest):** the whole value proposition depends on reliable tool calling; only offline (fake-brain) and text-only (live Gemini) paths are proven.
2. **Provider-coupling risk:** the Gemini integration is hand-mapped to `google-generativeai`; SDK/model churn (already seen — three model-name changes) can break it. An abstraction boundary exists, but the mapping itself is a maintenance hotspot.
3. **Fallback may not really fall back:** if Gemini is down and the local model can't tool-call reliably, "autonomy" degrades to nothing — and this hasn't been tested.
4. **Autonomy/authority gap vs. vision:** background, parallel, self-recovering execution — central to the long-term vision — is entirely absent, so V1's "autonomy" is a single supervised loop.
5. **Safety-policy defaults** (STOP unauthenticated, broad roots, autonomous overwrite) could produce an unpleasant surprise before the deeper controls exist.

---

## 25. Technical debt

- Live-path tests (Gemini tool-call, Ollama, Windows launch, GUI) live in one-shot `verify/` scripts, not in CI/pytest.
- No logging/observability layer.
- Dependency versions unpinned; no lockfile.
- `verify/` artifacts (reports, logs, PNG, `models_report.txt`) currently sit in the project tree; harmless but should eventually be gitignored or relocated.
- A new `GenerativeModel` is constructed on every Gemini call (minor overhead).
- Risk classification is static; no per-path or per-content policy.
- No `pyproject.toml` / console-script entry point (runs via `python -m void`).

---

## 26. Recommended V1 development sequence

Ordered to retire the biggest unknowns first, without expanding scope:

1. **Prove the live brain→action loop.** Run a real Gemini tool-calling task end-to-end (e.g., "find my cybersecurity notes and open it") and fix any translation issues. This converts the central claim from "should work" to "works." *(highest priority)*
2. **Tighten safety defaults before wider use:** scope `allowed_roots` to a few real working folders; add confirmation for overwriting **existing** files (or raise write risk when a target exists); enable a STOP PIN. Small, high-value hardening.
3. **Add minimal audit logging** of every tool call and decision (append-only file). Cheap, and it makes everything after it debuggable and safer.
4. **Exercise or explicitly defer the local fallback:** either install Ollama and prove one tool-calling task, or mark the local provider "experimental" in docs so no one relies on unproven behaviour.
5. **Prove the UI runtime:** one real task through the widget, including a confirmation dialog and STOP mid-run.
6. **Automate the live checks** (fold the `verify/` scripts into a documented, repeatable smoke test) and pin dependencies / add a lockfile.
7. **Then, and only then,** open the next capability milestone (memory, or the thin laptop↔Android link) — after the foundation is proven and safe.

---

## Architecture Questions Requiring Founder Decision

1. **Default file scope.** Should V.O.I.D operate over the whole home directory (current default) or be restricted to a small, explicit set of working folders for V1? This materially changes the blast radius.
2. **Autonomy for file modification.** Should overwriting an **existing** file require your confirmation (raise it to HIGH / treat existing-target writes as HIGH), or remain autonomous like today? Deletion already asks.
3. **STOP authentication.** Do you want the emergency-stop PIN **enabled by default** for V1 (aligns with the "proper authentication" principle) or left optional for convenience during development?
4. **Primary-brain policy.** For V1, is Gemini the sole practical brain (local model treated as experimental until proven), or is a working local fallback a V1 requirement — which would make proving Ollama tool-calling a blocking task?
5. **Prompt-injection stance.** How defensive should V1 be about untrusted file contents influencing the agent (e.g., never auto-modify files whose content was just read; content sanitisation; a stricter tool-use policy)? This trades autonomy for safety.
6. **Next milestone.** After the foundation is proven, which comes first — the **memory** subsystem or the **thin cross-device** link? (Earlier direction put cross-device last; confirm.)

---

## Current V1 Readiness

**Assessment: Foundation ready — approaching, but not yet at, "V1 development ready."**

Reasoning:

- It is **beyond "Prototype ready"** in structure and discipline: clean layering, a working offline agent loop, real Windows file/app actions, risk gating, a durable checkpoint store, a working kill switch, secure secret storage, a rendering UI, and 41 passing tests. The scaffolding for the whole V1 concept exists and is coherent.
- It is **not yet "V1 development ready"** because the **single most important path — a live LLM actually driving Windows actions via tool calls — has not been demonstrated once.** Gemini connectivity is proven only for plain text; the local fallback has never run; the UI is proven only as a rendered surface. Until a real model completes a real task end-to-end, the core premise remains unverified.
- It is clearly **not "V1 complete":** memory, cross-device, voice, autonomous/background execution, and several safety hardenings named above are not implemented, and the default safety posture (unauthenticated STOP, home-wide scope, autonomous overwrite) needs tightening before broad use.

**Bottom line:** the foundation is real and well-built — this is a genuine, working skeleton of V.O.I.D, not a mock-up. The next step that flips it to "V1 development ready" is small and specific: prove one live Gemini tool-calling task end-to-end and apply the three quick safety-default fixes (scope, overwrite confirmation, STOP PIN). After that, capability milestones can proceed on a trustworthy base.
