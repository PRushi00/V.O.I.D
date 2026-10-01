# V.O.I.D V2.0 Implementation Plan

| | |
|---|---|
| **Status** | Planning only. **Nothing implemented, installed, migrated, committed or modified.** |
| **Date** | 2026-09-21 |
| **Baseline** | `017e0f6cc180c889591adb22824671ec8f0cdf14` |
| **Branch / worktree** | `claude/void-v2-initialization-57bfb6` · `C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6` |
| **Inputs read** | `V2_TECHNICAL_SPEC.md` (complete; two identical copies, SHA-256 prefix `e253a2ef…`), the repository, and the earlier readiness report |
| **Evidence tags** | **[V]** verified by execution · **[C]** code-derived · **[M]** measured · **[E]** estimate/hypothesis · **[U]** unknown |
| **Scope colours** | 🟢 must have now · 🟡 later · 🔴 do not build yet |

**How this plan was validated (no production change).** Besides re-reading the code, I ran scratch scripts in temp directories/with fakes: three prior defect reproductions, plus in this pass two **design prototypes** — (1) the D-03 gateway fix and (2) a Windows path-canonicalisation matrix for protected roots — and a fresh baseline run. Live gateway, live runtime, `~/.void`, Credential Manager and `main` were not written.

---

## 1. Repository State

| Item | Finding | Tag |
|---|---|:-:|
| Branch / worktree | `claude/void-v2-initialization-57bfb6` at `C:\V.O.I.D\.claude\worktrees\void-v2-initialization-57bfb6`; other worktree: `C:/V.O.I.D [main]` | [V] |
| Baseline in Git | Commit object `017e0f6c…` exists; **no tags**; `main` = `origin/main` = worktree `HEAD` = baseline; baseline is an ancestor of `HEAD`; tracked diff vs baseline is empty (tree hash `2b1bdfcc…`); 64 commits | [V] |
| Worktree cleanliness | Only untracked `docs/` (spec copy, reports, `v2-evidence/`) | [V] |
| `main` checkout | **Tracked-clean.** Untracked: `V2_TECHNICAL_SPEC.md` and `V2_0_IMPLEMENTATION_READINESS_REPORT.md` (owner copies of my documents), `wakeword-training/` (2.9 GB, never tracked). I did not touch it. | [V] |
| Python | venv `C:\V.O.I.D\.venv` = **3.14.7** (shared with the *live* runtime); `PATH` python = 3.12.0; worktree has no venv | [V] |
| Dependencies | `requirements.txt`, `requirements-voice.txt` with `>=` ranges. **No** lockfile, `pyproject.toml`, `pytest.ini`, CI or `.github/`. Live venv: **80 packages, none editable/URL** ⇒ a lockfile is derivable read-only | [C][V] |
| Entry points | `python -m void` (`__main__.py` → `cli.main`): `run/resume/approve/deny/clarify`, `set-key/list-keys/remove-key/set-pin`, `stop/clear-stop`, `roots`, `protect`, `ui`, `app`, `singularity`, `voice`, `autostart`, `device {serve,pair-start,list,grant,revoke,forget}`; autostart launcher `runtime/voice_startup.py` | [C] |
| Gateway default | **Off unless `device serve` is run.** Not started by voice/autostart. (A manually started one is currently listening on `0.0.0.0:8765`.) | [C][V] |
| Config | Tracked `config/default_config.yaml` is safe (`allowed_roots: ~/VOID/workspace`, PIN required). Git-ignored `local_config.yaml` (machine-local) sets `allowed_roots: [C:\]`, wake `whisper_gen3`, local model `qwen3:8b` | [V] |
| State | `~/.void`: `tasks.sqlite` (103 tasks), `devices.json` (2), `device_cert/key.pem`, `pairing_window.json` (transient), `void.log`; **no** memory/audit/perf/health stores. `app.state_dir` accepts an **absolute** path | [V] |
| Live runtime | `VOID_VoiceRuntime` running from `C:\V.O.I.D` (main). **Mic dead since 18:43:32 on 2026-09-20** (re-checked 00:02) | [V] |
| Hardware | Core Ultra 9 275HX (24 cores), 31.4 GB RAM, RTX 5070 Laptop 8 GB | [V] |

### 1.1 Actual architecture (traced)

```
cli.py ──▶ Assistant (app.py) ──▶ builds: KillSwitch, RiskGate, TaskStore, ToolRegistry(13 tools), ProviderRegistry
                                   └─ _agent(): providers.select() ──▶ Agent(provider, tools, risk_gate, kill_switch, store)
Agent._loop ─▶ _generate_with_retry ─▶ provider.generate()          (only call site: agent.py:150)
            ─▶ _commit_step ─▶ [defer-eval of risk, agent.py:450-475] ─▶ _run_call ─▶ RiskGate.authorize ─▶ ToolRegistry.execute
Voice:  broker ─▶ wake/PTT ─▶ VoiceSession(reducer) ─▶ STT ─▶ Assistant.run(transcript) ─▶ speak(result.result only)
Device: TLS ─▶ parse ─▶ limiter ─▶ HMAC ─▶ replay ─▶ capabilities.dispatch ─▶ KillSwitch ─▶ RiskGate ─▶ ToolRegistry
        (gateway is constructed from the SAME Assistant: cli.py:717 `DeviceGateway(cfg, assistant.tools, assistant.risk_gate, ...)`)
```

Facts that shape the plan:

1. **Two risk-evaluation sites, not one.** `_run_call` executes; but `_commit_step` *also* evaluates risk beforehand to decide whether to suspend a step for owner approval (`agent.py:450-475`). The capability engine must therefore expose **`evaluate()`** (used by both) and **`execute()`**. [C]
2. **The gateway is a second execution path** that calls `RiskGate`/`ToolRegistry` itself (`device/capabilities.py:_run_tool_capability`). To keep *one* authority boundary it must route through the engine too. Because it is built from `assistant.*`, the engine can live on `Assistant`. [C]
3. **Only one call site** consumes `provider.generate` ⇒ a router is a drop-in at `agent.py:150`. [C]
4. **Tools have no COM/thread affinity** (only `subprocess.Popen`, `os.startfile`, win32 window APIs) ⇒ running a tool in a worker thread under a timeout is feasible. [C]
5. **`broker.stop()/start()` is invoked in exactly one place** (`runtime.py:531-532`, recovery) ⇒ fixing D-01 by guarding on `closed` instead of `running` is safe. [C]
6. **Voice `_apply` runs continuations inline on the calling thread** and the release chain runs on one `_SerialVoiceWorker` thread ⇒ a per-generation `interaction_id` owned by `VoiceSession` and set as a `contextvars` value in the worker gives correlation without signature changes. [C]

### 1.2 Test state

| Measure | Value | Tag |
|---|---|:-:|
| Collected | **883** in 42 modules | [V] |
| Result | **878 passed, 5 skipped, 0 failed** — four runs this session, latest at 00:0x today (53 s) | [V] |
| Skips | 3 symlink-privilege (`test_protected_roots.py:127,140,375`); 2 optional-dependency-installed | [V] |
| Android | 26 JVM tests pass (`gradlew test --offline`, JDK 17) — run earlier this session | [V] |
| Largest areas | voice 101 · files 57 · capture_broker 52 · providers 48 · voice_wake_integration 45 · wake 34 · protected_roots 32 · cli 32 · device_gateway 30 · agent 28 | [V] |
| Test-isolation defect | Two files touch the **real** Windows Credential Manager. `test_device_gateway.py`: 19 tests set 19 secrets, delete 2. **343 `device_secret` credentials exist for 2 paired devices (341 orphans)** | [V] |
| CI | none | [C] |

---

## 2. Specification Verification

Verdicts: **V**erified · **P**artially verified · **X** contradicted · **U**nknown.

| # | Specification claim | Verdict | Evidence / correction |
|--:|---|:-:|---|
| 1 | Baseline `017e0f6…` = `main` = `origin/main`; branch clean | **V** | §1 |
| 2 | 878 pass / 5 skip; 42 test modules | **V** | §1.2 (883 collected) |
| 3 | 13 tools (6 file, 2 app, 5 window/process) | **V** [C] | `files.py`, `apps.py`, `computer.py` |
| 4 | `_run_call` is the single tool funnel; engine can delegate from it | **P** | Execution funnel: yes. But `_commit_step` has a **second** risk-evaluation for deferral (`agent.py:450-475`) ⇒ engine needs `evaluate()` as well as `execute()` |
| 5 | Gateway routes through KillSwitch→RiskGate→ToolRegistry | **V** [C] | `capabilities.py:_run_tool_capability` — but it calls them **directly**, i.e. a second authority path the engine must absorb |
| 6 | Tools have no timeouts; kill is cooperative | **V** | [C]; kill mid-tool [V]: tool completed, next turn prevented |
| 7 | `select()` once per run; `available()` = SDK+key | **V** [C] | `registry.py:44`, `gemini_provider.py:202` |
| 8 | Retry "sleeps 1 s/2 s" | **X** | **1, 2, 4 s (7 s total)**, including after the last attempt [V] |
| 9 | Gemini builds a new client per call; no timeout | **V** [C][V] | `http_options.timeout=None` [V]; construction 35.7 ms CPU; *network* benefit of reuse **[U]** |
| 10 | Thinking can be controlled | **P** | `ThinkingConfig.thinking_level/thinking_budget` exist in the SDK [V]; support by the configured model **[U]**; effect on the 35.9 s tail **[E]** |
| 11 | SDK supports streaming; V1 can't use it usefully | **V** | `generate_content_stream` exists [V]; `SapiTTS.speak` *replaces* (no queue) [C] |
| 12 | Local `qwen3:8b`: 16–24 s default vs 3.3–4.3 s `think:false` | **V** [M] | n=2 per case; **not an accuracy evaluation** |
| 13 | "83 s cold load" | **P** | 83 s was disk-cold; **4.4 s** OS-cache-warm [M] |
| 14 | VRAM plan | **V** [M] | 8151 MiB; ≈1.5 GB desktop; 5.6 GB LLM at ctx 4096; **900 MiB free** |
| 15 | STT ≈RTF 0.9; target ≤0.8 s | **X/P** | Cost is ~constant per utterance (fixed 30 s window): CPU `small` int8 1.19/0.95/**0.81** s for default/8/16 threads on 1.8 s and 4.7 s clips [M, synthetic]. Live median 2.56 s is ≈2× isolated: **[U]** |
| 16 | GPU STT is an option | **X** for V2.0 | `cublas64_12.dll` missing [V]; no VRAM anyway |
| 17 | Wake ≈2 cores steady | **U/E** | Derived from cumulative CPU; keep as [E] |
| 18 | PTT `ctrl+space` hooks bare `space` | **V** | `_key_token()`→`'space'` [V]; 723/809 zero-sample STT starts [M] |
| 19 | `RegisterHotKey` is a viable PTT | **P** | API present [V]; Ctrl+Space would be removed system-wide from Cursor/VS Code (installed); Ctrl+Alt+Space taken (1409); Ctrl+Shift+Space free [V] |
| 20 | Mic supervisor abandons after one failed restart | **V** | Repro [V]; live process deaf [V] |
| 21 | One idle TCP connection stalls the gateway | **V** | Repro [V]; **fix prototyped [V]** (§7 T0.5) |
| 22 | File tools reach V.O.I.D secrets; roots `C:\` | **V** | Plus **D-04b**: planted `pairing_window.json` is redeemable [V] |
| 23 | `open_path` opens any URL at LOW | **V** | [V] |
| 24 | Protected roots are config-only | **V** [C] | `files.py:49-68`, `config.py:104` |
| 25 | `tasks.sqlite` plaintext/unbounded, 3 stale `running` | **V** | 103 tasks, 587 KB, 57 hold tool output |
| 26 | Task store has no schema version | **V** [C] | ad-hoc `ALTER TABLE` |
| 27 | Log non-rotating | **V** [C] | `diagnostics.py:77` `FileHandler` |
| 28 | Audit log: one hash-chained file | **P** | CLI, runtime and gateway are **separate processes** ⇒ per-role files (or an OS lock) needed |
| 29 | Extend `RiskGate` with taint context | **P** | Better: leave `RiskGate` **unchanged**; the engine computes the effective risk and passes it in (`authorize` already takes a level + `owner_decision`) |
| 30 | Hypothesis-based property tests | **X** (dependency) | V2.0 adds **no** dependencies: use seeded `random` generators |
| 31 | AES-256-GCM available; key fits keyring | **V** | `cryptography` 50.0.1; 44-char key [V] |
| 32 | SQLite FTS5 available | **V** | Not needed (in-memory BM25) |
| 33 | Voice "remember…" → active memory | **X** (design) | Wake/voice is an untrusted activation (ambient audio). Voice-originated writes ⇒ `proposed` |
| 34 | Fast path includes time/status | **X** | No time/status tool exists; open/launch only |
| 35 | On-screen confirmation | **P** | Only the developer widget has a modal; the persistent runtime is CLI-approve-only; voice is silent on AWAITING/FAILED/BLOCKED [V] |
| 36 | Two test files touch the real keyring, cleanup-scoped | **X** | One cleans up; **one leaks** (D-14) |
| 37 | Android secret plaintext | **V** [C] | `MainActivity.kt:293-294`; `allowBackup=false` mitigates ADB backup only |
| 38 | Latency shares (LLM ≈70–80 %) | **E** | n=6 interactions; treat as hypothesis until WP0.8 data |
| 39 | Fast-path coverage; wake false-accept rate | **U** | Need telemetry |
| 40 | SAC state | **P** | 0 (off) now [V]; code comment says it once blocked packages; history **[U]** |

**Net:** the spec's architecture, ordering and security direction are supported. Six claims are corrected above (#8, #15/16, #29, #30, #33/34, #36) and the plan follows the corrected versions.

---

## 3. Current V1 Defects

Verdict: **Confirmed / Partially confirmed / Rejected / Unknown**. Severity is for a single-owner personal system exposed to a LAN/hotspot.

| ID | Issue | Verdict | Evidence | Sev. | Impact | V2.0 relevance | Recommended treatment |
|---|---|:-:|---|:-:|---|:-:|---|
| **D-01** | Mic supervisor abandons recovery after one failed restart | **Confirmed** | [V] scratch tests fail on baseline; [C] `_check_mic_health` returns on `not broker.running`, `broker.start()` failure sets `_running=False`; existing test stops after first failure; live runtime deaf ≈5 h | High | Wake word silently dead until process restart | **P0** | Guard on `closed`; retry with capped backoff; add Nth-retry tests |
| **D-02** | `ctrl+space` hooks bare `space` | **Confirmed** (code+log; physical keystroke path not reproduced) | [V] token = `'space'`; [M] 723/809 STT starts 0-sample, 1 after wake | High | Any Space press starts a voice session; audio ≳30 ms is transcribed | **P0** | Match the full chord in place; `RegisterHotKey` deferred to V2.1 |
| **D-03** | One idle TCP connection stalls the gateway | **Confirmed** | [V] repro (2 ms→timeout→refused); [C] TLS-wrapped listening socket, no timeouts; **[V] fix prototype** | High (avail.) | Unauthenticated DoS of the device gateway | **P0** | Lazy handshake + handler timeout + bounded handlers (validated) |
| **D-04** | File tools reach V.O.I.D secrets; broad roots | **Confirmed** | [V] read of key/pairing files LOW/executed; new files MEDIUM/executed; roots `C:\` locally | High | Secrets flow to the cloud LLM | **P1** | Engine-protected roots (§8 T1.1) |
| **D-04b** | Agent-created `pairing_window.json` is redeemable | **Confirmed** (new) | [V] `write_file` MEDIUM→autonomous→`redeem(attacker token)` succeeds | **High** | Authority escalation into device pairing | **P1 (first)** | Same fix; candidate for early cherry-pick |
| **D-05** | `open_path` accepts arbitrary http/https at LOW | **Confirmed** | [V] attacker URL handed to (patched) browser | Med–High | Exfiltration channel under prompt injection | **P1** | URL policy + taint egress rule |
| **D-06** | `tasks.sqlite` plaintext/unbounded; stale `running` | **Confirmed** | [V] 103 tasks/587 KB/57 with tool outputs/3 stale | Med | Privacy accumulation | **P0 (sweep) + P1 (retention)** | Sweep in P0; retention/redaction in P1; never mine into memory |
| **D-07** | No provider failover | **Confirmed** | [V] 3 cloud attempts, 0 local, task FAILED; [C] `select()` once/run | Med | Offline ⇒ failure | **P2** | `ModelRouter` |
| **D-08** | Local `think`/`num_ctx`; cloud no timeout | **Confirmed** (local, timeout); **Partial** (cloud thinking effect) | [V] 16–24 s vs 3.3–4.3 s; Ollama default ctx 4096 never set; SDK timeout None; cloud thinking effect **[E]** | Med | Latency, silent context truncation | **P2** | Provider options; measure before changing cloud defaults |
| **D-09** | Gateway hygiene | **Partially confirmed** | [V] limiter 5,000 keys / 5,003 log lines from 5,000 unauthenticated requests; [C] PIN `==`, key plaintext (`cert.py:55`), pairing file plaintext; limiter *poisoning* needs the 128-bit id ⇒ low | Low–Med | Memory/log growth | **P0 (limiter, log sampling, PIN)**; V2.1 (key at rest, pairing ACL) | Bound/prune; sample; `compare_digest` |
| **D-10** | Log not rotated | **Confirmed** | [C] `FileHandler`; 1.2 MB/3 days; inflated by D-02, D-09 | Low | Unbounded growth | **P0** | Rotating handler |
| **D-11** | Android secret in plaintext prefs | **Confirmed** (code only) | [C] `MainActivity.kt:293-294`, comment `:35-38` | Med | Phone-side theft | **V2.1** | Keystore-wrapped secret |
| **D-12** | Stale duplicate device identity | **Confirmed** | [V] two "My Android Phone", both `launch_app` | Low–Med | Stale trusted identity | Owner action; policy **V2.1** | Not performed by me |
| **D-13** | Voice silent for AWAITING/FAILED/BLOCKED | **Confirmed** (new) | [V] | Med | Owner cannot see approvals/failures | **P0** | Fixed engine phrases |
| **D-14** | Tests litter the real credential store | **Confirmed** (new) | [V] spy + 341 orphans | Med | Clutter; secrets residue | **P0 (first)** | Isolation fixture |

---

## 4. V2 Milestone Boundaries

| Milestone | Contents | Why this boundary |
|---|---|---|
| **V2.0 — Foundation** | **P0** stabilise + instrument · **P1** capability + security foundation · **P2** router + offline foundation · **P3** initial memory | Everything later is a *capability*, *untrusted-data consumer* or *latency-sensitive*. Without P1 nothing new is safely authorisable; without P0/P2 nothing is measurable or resilient; memory (P3) is the prime poisoning target and needs P1's taint/audit and P2's egress filter. |
| **V2.1 — Voice + connectivity** | **P4** STT provider/endpointer/VAD-gated wake/hotkey; **P5** Keystore secret, device policy v2 (trust, expiry, stale suspension), gateway key-at-rest/source policy, tray approval prompt, posture report | Both are edge-of-system changes that benefit from V2.0 telemetry (P4) and audit/engine (P5), and P5 needs physical-phone time. |
| **V2.2 — Multimodal + system control** | **P6** camera (privacy state machine, single still, consent) · **P7** expanded Windows capabilities | Highest privacy risk and largest capability surface; each capability must ship through the V2.0 manifest/engine/audit. |
| **Future** | Streaming STT/TTS, cloud STT, AEC/barge-in, OCR, screen capture, local vision, browser automation, clipboard, settings control, dev-tool templates, asymmetric device identity, overlay remote access, advanced network monitoring, autonomy, richer memory extraction, embeddings *if measured* | Each needs its own spec, threat model and owner approval. |

**Moved relative to the spec:** stale-task sweep and D-09 limiter/PIN hygiene are P0 (small, pre-auth or already-corrupt state); on-screen approval, hotkey change and Keystore are V2.1; time/status intents are V2.2.

---

## 5. V2.0 Scope

### 🟢 Must have (34 tasks; detail in §7–§10)

| Stream | Exact features |
|---|---|
| **P0** | Hermetic test isolation · strict-xfail regression tests · fix D-01 · fix D-02 (in place) · fix D-03 + D-09 hygiene · rotating log + privacy-safe perf JSONL · `interaction_id` correlation across activation→endpoint→STT→route→LLM→tool→verify→response→completion · voice/gateway/provider health instrumentation + `perf report` + minimal `doctor` · stale-task sweep · spoken engine status (D-13) · reproducibility (lockfile from `pip freeze`, interpreter policy, hermetic CI, test-count guard) |
| **P1** | Engine-protected roots (two-layer check) · hash-chained audit log · tool manifest + JSON-schema argument validation (13 tools annotated) · task-store schema version + taint column + retention/redaction · taint model · URL policy · secret hygiene (reserved names, detector) · **`CapabilityEngine`** (`evaluate`/`execute`; timeouts; verification; audit; gateway routed through it) behind a flag |
| **P2** | Extended provider interface · Gemini deadline + measured thinking/client-reuse options · Local provider correction (`think`, `num_ctx`, `keep_alive`, availability, context guard) · `ConnectivityMonitor` · `ModelRouter` (per-call selection, breaker, failover, safe handoff) · eval harness + VRAM/context measurements · degraded-mode notices · deterministic fast path (open/launch only, through the engine, flagged) |
| **P3** | Session context · separate encrypted `memory.sqlite` · write policy/secret gate/quarantine · in-memory BM25 retrieval with hard budget · CLI list/show/remember/forget/correct/review · agent+router integration (`propose_memory`, per-provider `cloud_ok`) · retention and *logical* deletion semantics |

### 🟡 Later (deliberately deferred)

STT provider registry, Silero endpointer, VAD-gated wake, `cpu_threads` tuning, `RegisterHotKey`, tray approval prompt (V2.1) · Keystore secret, device policy v2, TLS-key-at-rest, pairing-file ACL, source-address policy, `void security status` (V2.1) · camera, `system_status`, window ops, audio (V2.2) · time/status fast-path intents (V2.2) · sentence-queued TTS + streaming answers · memory export/rotate-key · everything in the "Future" row.

### 🔴 Do not build

Camera/vision/OCR/screen capture in V2.0 · Keystore/asymmetric identity · remote/off-LAN access · streaming/cloud STT · browser automation · clipboard monitoring · settings automation · **arbitrary shell/PowerShell/command execution** · synthetic keyboard/mouse injection · UAC/elevation · registry/GPO/firewall modification · autonomous background operation · active network countermeasures · face recognition · vector DB/embeddings · multi-agent · cloud infrastructure · speculative abstractions · large Windows-capability expansion · **any new dependency**.

---

## 6. V2.0 Architecture

### 6.1 Diagram

```
   OWNER (T0)                       UNTRUSTED ACTIVATION (T1)                 PAIRED DEVICE (T2)
 CLI · dev UI · approve/deny        wake / PTT ─▶ STT ─▶ transcript           Android ─▶ TLS+pin ─▶ HMAC ─▶ replay
        │                                   │                                          │ closed allow-list
        │ typed goal                        │ voice goal (channel=voice)               │ (channel=device)
        ▼                                   ▼                                          ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │                                    Assistant / Agent (V1 loop)                               │
 │  Task{ledger, checkpoints, pending, TAINT} · SESSION CONTEXT · durable AWAITING_CONFIRMATION │
 └──────┬─────────────────────┬────────────────────────────────┬────────────────────────┬───────┘
        │ context             │ decide model                    │ every tool call        │ record
        ▼                     ▼                                 ▼                        ▼
 ┌──────────────┐   ┌─────────────────┐   ┌────────────────────────────────────┐   ┌──────────────┐
 │ MEMORY (P3)  │   │ FAST PATH (P2)  │   │ CAPABILITY ENGINE  ◀── AUTHORITY   │   │ AUDIT (P1)   │
 │ memory.sqlite│   │ open/launch only│──▶│ kill → manifest → schema →         │   │ hash-chained │
 │ AES-GCM text │   │ (no LLM)        │   │ PROTECTED ROOTS → risk → TAINT     │──▶│ per-role     │
 │ in-RAM BM25  │   └────────┬────────┘   │ → authorize(RiskGate) → audit(pre) │   │ PERF (P0)    │
 │ fenced block │            │ else       │ → execute(timeout) → verify →      │   │ JSONL, no    │
 └──────┬───────┘            ▼            │ taint update → audit(post)         │   │ content      │
        │            ┌─────────────────┐  └───────────────┬────────────────────┘   │ HEALTH (P0)  │
        │ (blocks    │ MODEL ROUTER(P2)│                  │ 13 tools (V1)          └──────────────┘
        │  filtered  │ deadline·breaker│                  ▼
        │  per       │ failover·handoff│        files · apps · windows · processes
        │  provider) └───┬─────────┬───┘        (+ protected roots, URL policy)
        └───────────────▶│         │
              ONLINE: Gemini (+CredentialPool)   OFFLINE: Ollama qwen3:8b (think:false, num_ctx set)
                    ▲            ConnectivityMonitor ▲
 Security spine (unchanged authority): KillSwitch · RiskGate · credential store · engine-protected roots
 Trust rule: LLM proposes. Engine decides. Nothing in Memory/Tool output/Files/Router ever reaches authorize().
```

### 6.2 Components, dependencies, boundaries

| Component | Depends on | Authority | New/extended |
|---|---|---|---|
| **CapabilityEngine** | manifest, protected roots, taint, `RiskGate`, `KillSwitch`, audit | **The only authority.** Decides ALLOW/DEFER/DENY; runs and verifies tools | New (wraps `_run_call`) |
| `RiskGate` | — | Threshold + owner decision (**unchanged**) | Reused |
| Protected roots | state dir, repo config dir, OS credential locations | Hard deny; not configurable, not confirmable | New (`FileActions._confine`) |
| Taint | `Task` | Engine-owned, monotonic | New |
| Audit | filesystem lock | Records; never decides | New |
| Router | providers, monitor, egress policy | **No authority**: cannot change risk/approval | New |
| Fast path | manifest `intents`, `AppCatalog` | **No authority**: produces an ordinary call executed by the engine | New |
| Memory | crypto, keyring, policy | **No authority**; output is untrusted data | New |
| Perf/health | logging | Observes only | New |

### 6.3 Data flow (voice command, V2.0)

`wake → interaction_id assigned (VoiceSession generation) → endpoint → STT → Assistant.run(goal, channel="voice") → [fast path? → engine] else [context = session + memory (fenced, cloud_ok-filtered per target)] → router picks provider (deadline, breaker) → LLM proposes tool call(s) → engine.evaluate (defer/deny/allow) → audit(pre) → execute(timeout) → verify → taint update → audit(post) → response (LLM text or engine status phrase) → speak → perf events for every stage`

### 6.4 Trust boundaries

| Zone | Rules |
|---|---|
| **T0 owner-local** (CLI/dev UI, `approve`/`deny`, config) | Trusted; the *only* source of `owner_decision` |
| **T1 owner voice** | Untrusted activation: LOW/MEDIUM only; HIGH/privacy ⇒ deferred, **never approvable by voice** (D13); never writes active memory |
| **T2 device** | Closed allow-list (`get_status`, `launch_app`); HIGH ⇒ deny |
| **U untrusted data** | LLM output, tool output, file contents, window/app names, retrieved memory, provider responses — never inputs to `authorize()`; may taint |

### 6.5 Integration points (verified in code)

`agent.py:150` (router) · `agent.py:182` `_run_call` + `agent.py:450-475` defer-evaluation (engine `execute`/`evaluate`) · `app.py:75` `_agent()` (wiring, flags) · `actions/base.py:33` `Tool` (manifest) · `files.py:83` `_confine` (protected roots) · `apps.py:57` (URL policy) · `core/task.py:92` (schema/taint/retention) · `voice/session.py:258` (status phrases, interaction id) · `voice/runtime.py:488/526` (D-01, heartbeat) · `voice/adapters.py:87` (D-02) · `device/gateway.py:178` + `auth.py:75` + `kill_switch.py:72` (D-03/D-09) · `device/capabilities.py:_run_tool_capability` (engine routing) · `runtime/diagnostics.py:60` (rotation) · `cli.py:155` (reserved names) · `cli.py:717` (engine to gateway).

---

## 7. P0 Implementation Plan — Stabilise + Instrument (11 tasks)

*Format per task: **Objective · Files (likely) · Reuses · Approach · Tests · Security · Acceptance.** File names marked "likely" are not yet established; existing files were read.*

### T0.1 — Hermetic test isolation *(S)*
- **Objective.** No test can touch the real Credential Manager, real `~/.void`, or the production log (D-14).
- **Files.** `tests/conftest.py` (+ small helper module); marker registration (no `pytest.ini` exists).
- **Reuses.** Lazy `keyring` import in `security/secrets.py`; `diagnostics._running_under_pytest` guard pattern; existing per-test fakes; scratch prototypes `docs/v2-evidence/void_test_isolation.py`, `keyring_spy.py`.
- **Approach.** Autouse session fixture installs an in-memory keyring backend; autouse per-test fixture points `HOME`/`USERPROFILE` at a temp dir (`Config.state_dir()` expands `~` at call time [C]); markers `real_socket`, `real_keyring`, `hardware` (`real_keyring` is reserved and unused). A session-end assertion fails if the real backend was ever called.
- **Tests.** Fixture self-tests (real backend unreachable; env redirected; log path redirected); full suite unchanged.
- **Security.** Removes secret residue; protects the real store.
- **Acceptance.** 878 pass / 5 skip unchanged; keyring spy shows **0** real operations; real `~/.void` listing unchanged; count of `device_secret` credentials (read-only `cmdkey /list`) unchanged before/after a full run.

### T0.2 — Failing-first regression tests *(S–M)*
- **Objective.** Turn each defect into a permanent executable specification before fixing it.
- **Files.** `tests/test_regression_v1_defects.py` (likely).
- **Reuses.** `tests/helpers.FakeProvider`; `test_voice_mic_recovery` rig (`FakeBackend`, `Clock`, `_rig`); loopback pattern of `test_device_gateway`; the scratch repros.
- **Approach.** `xfail(strict=True)` tests asserting the *intended* behaviour: D-01 (Nth retry; eventual recovery), D-02, D-03, D-04b, D-05, D-07 (failover), D-09 (limiter bound), D-13 (spoken status).
- **Tests.** Meta-check: run with `--runxfail` and confirm each fails **for the stated reason** (assert message), not for a fixture error.
- **Security.** Documents the security defects (D-03, D-04b, D-05) as tests.
- **Acceptance.** `878 passed, 5 skipped, 9 xfailed`; `git diff --stat` shows only `tests/`.

### T0.3 — Fix D-01 mic supervisor *(S)*
- **Objective.** The supervisor keeps trying until the device returns or the owner shuts down.
- **Files.** `void/voice/runtime.py` (`_check_mic_health`, `_attempt_mic_recovery`), tests.
- **Reuses.** The whole supervisor, backoff constants, `AudioCaptureBroker.closed` (verified single restart site `runtime.py:531`).
- **Approach.** Guard on `broker.closed` (owner shutdown) instead of `not broker.running`; treat `running=False ∧ ¬closed` as unhealthy immediately; failed restart schedules the next attempt with the existing capped exponential backoff; still never restarts during an active capture. Emit `mic.recovery_*` perf events.
- **Tests.** xfails flip; Nth-retry over 10 simulated minutes; capped backoff; success after K failures; no restart mid-capture; no retry after `shutdown`; heartbeat reflects state.
- **Security.** No authority change; never re-opens the mic after owner shutdown.
- **Acceptance.** See G0 #4; owner-attended physical check on a dev-isolated run (toggle Windows microphone access; recovery ≤60 s).

### T0.4 — Fix D-02 PTT chord *(S)*
- **Objective.** `ctrl+space` requires Ctrl; bare Space does nothing.
- **Files.** `void/voice/adapters.py` (`PTTActivation`), tests in `tests/test_voice.py`, config comment.
- **Reuses.** Existing edge tracking and the injectable `key_is_down` seam.
- **Approach.** Parse the chord into modifiers + main key; require each modifier down (injected predicate, bound to `keyboard.is_pressed` at `start()`); release still ends on main-key up. Flag `voice.ptt_strict_chord` (default true) for rollback. **No `RegisterHotKey` in V2.0** (it would remove Ctrl+Space from Cursor/VS Code; decision deferred to V2.1).
- **Tests.** Bare Space ignored; chord starts; Ctrl released before Space; auto-repeat unchanged; hotkeys with no modifier unchanged.
- **Security.** Removes accidental mic activation from ordinary typing; the low-level hook remains (acknowledged, V2.1).
- **Acceptance.** G0 #5; on a dev-isolated run with the `null` capture backend, 1 h of typing yields ≈0 PTT sessions.

### T0.5 — Fix D-03 and D-09 hygiene *(M)*
- **Objective.** One idle/slow client cannot stall the gateway; pre-auth state and logs are bounded.
- **Files.** `void/device/gateway.py`, `void/device/auth.py` (`RateLimiter`), `void/core/kill_switch.py`, tests (`test_device_gateway.py` + regression file).
- **Reuses.** `ThreadingHTTPServer`, existing limiter class, all existing error codes/HTTP statuses.
- **Approach (prototype-validated [V]).** `wrap_socket(..., do_handshake_on_connect=False)` so the handshake happens in the handler thread; `_GatewayHandler.timeout` (config `device.connection_timeout_s`); bounded concurrent handlers via a semaphore (config `device.max_connections`, default 32; excess dropped); `handle_error` logs one sampled line (also explains the intermittent stderr in gateway tests); `RateLimiter` capped/pruned (LRU) and a per-IP pre-auth limiter ahead of body parse; rejection logs sampled (≤1/s per reason, suppressed counts); PIN via `hmac.compare_digest`.
  *Prototype result:* V1 → legit request 4,022 ms→timeout, then connection refused; prototype → legit request 5–27 ms with 1 idle, 3 half-open ClientHello, and 150 idle connections; server closed all attacker sockets itself; 118 excess connections dropped.
- **Tests.** Idle, half-open, slow-loris, 150-connection flood; legit unaffected; limiter ≤ cap after 10 k probes; log sampling; constant-time PIN; **all ~114 existing device tests unchanged** (TLS, pin, HMAC, replay, allow-list).
- **Security.** No TLS/pin/HMAC/replay/allow-list weakening; fail-closed on timeout; availability only.
- **Acceptance.** G0 #6–7.

### T0.6 — Rotating log + privacy-safe perf sink *(M)*
- **Objective.** Bounded logs (D-10) and a structured, content-free performance stream.
- **Files.** `void/runtime/diagnostics.py`; new `void/perf/` (likely); config keys (`logging.*`, `perf.enabled`).
- **Reuses.** The idempotent install pattern and pytest guard in `diagnostics.py`; existing `LLM_CALL_DONE`/`TOOL_CALL_DONE` markers (kept).
- **Approach.** `RotatingFileHandler` (default 5 × 5 MB) in place of `FileHandler`. Perf logger `void.perf` (propagate=False) → `~/.void/perf/perf-YYYYMM.jsonl` (rotating). `emit(event, **fields)` enforces a **schema allowlist**: numbers, booleans, enumerated strings, ids, tool/provider/model names, exception *class* names; any other or >64-char string is dropped and counted.
- **Tests.** Rotation; idempotence; schema rejects transcript-like/path-like/secret-like strings (seeded random corpus); thread safety; pytest guard.
- **Security.** Privacy by construction: no transcripts, audio, args, file content, secrets.
- **Acceptance.** G0 #9/#11.

### T0.7 — `interaction_id` correlation *(M)*
- **Objective.** One id links activation → endpoint → STT → route → LLM → tool → verify → response → completion.
- **Files.** `voice/session.py`, `voice/runtime.py`, `voice/adapters.py` (STT timing), `core/agent.py` (thin emit calls at existing log sites), `app.py` (`run(goal, channel=…)`), `device/gateway.py`.
- **Reuses.** `VoiceSession.generation` (bumped per session), `_SerialVoiceWorker` (one thread for the release chain), existing log sites.
- **Approach.** `contextvars.ContextVar("interaction")`. `VoiceSession` mints the id at `NEW_GENERATION` and sets it in the worker before STT/dispatch/speak; `Assistant.run` mints one for CLI goals. Stage events use a fixed enum: `activation, endpoint(reason, capture_s), stt(audio_s, decode_s, model, threads), route(provider, reason), llm(attempt, duration_s, tokens, tool_calls), tool(name, risk, duration_s, ok), verify(ok), respond(kind), complete(status, total_s), speak(start_s, dur_s)`. **No agent logic changes.**
- **Tests.** A fake-voice interaction yields one contiguous chain with monotone timestamps; CLI chain; no content fields; overhead <1 ms/event.
- **Security.** No content; ids are random.
- **Acceptance.** G0 #9.

### T0.8 — Health instrumentation, `perf report`, `doctor` *(M)*
- **Objective.** Make a D-01-class failure visible in seconds; produce the measurement tables.
- **Files.** `voice/runtime.py` (heartbeat in the monitor tick), `device/gateway.py` (counters), `cli.py` (`perf report`, `doctor`), `void/perf/report.py` (likely).
- **Reuses.** `poll_once` cadence; `running_port`; diagnostics; my `log_analysis.py` as the report prototype (also parses *legacy* `void.log` markers so pre-V2 history remains usable).
- **Approach.** `~/.void/health.json` atomically replaced every ≈5 s: `{ts, pid, state, mic{healthy, seconds_since_frame, recovery_attempts}, wake{armed, inference_hz, last_score_age}, broker_running}`. Gateway emits per-minute counters (requests/ok/rejected by code, active connections, handshake timeouts). `doctor` is **read-only**: stale heartbeat >30 s, silent mic, gateway loopback handshake (2 s timeout), log/perf sizes, task counts. `perf report`: per-stage counts and p50/p95 (p99 only when n ≥ 300).
- **Tests.** Atomic write; `doctor` on fresh/stale/mic-silent/absent; report golden output from a fixture log; legacy-log parser.
- **Security.** Read-only; no content.
- **Acceptance.** G0 #9–10 (report reproduces the §2.3 tables of the spec on the 2026-09-17→20 log).

### T0.9 — Stale-task sweep *(S)*
- **Objective.** No task remains `running` after its process died.
- **Files.** `core/task.py` (`TaskStore.sweep_stale`), `app.py`/`cli.py` (call at runtime start and `tasks`).
- **Reuses.** `Status.PAUSED` (resumable), `updated_at`.
- **Approach.** `running` tasks with `updated_at` older than 15 min ⇒ `paused`, `error="interrupted: process exited"`; recent tasks untouched; dry-run mode; logs the count. Never auto-resumes.
- **Tests.** Synthetic V1-shape DB: old→paused; recent untouched; terminal untouched; idempotent; two connections.
- **Security.** Fail-safe (paused, not completed); no execution.
- **Acceptance.** Sweep on a synthetic copy reclassifies exactly the stale rows.

### T0.10 — Spoken engine status (D-13) *(S)*
- **Objective.** The owner hears when approval is needed or a task failed.
- **Files.** `voice/session.py` (`_run_dispatch`), constants module (likely).
- **Reuses.** Existing `DISPATCH_OK_SPEAK` path via `_pending_response`.
- **Approach.** For `AWAITING_CONFIRMATION`, `BLOCKED`, `FAILED`, `PAUSED` speak a **constant** phrase per status (e.g. "That needs your approval — use the command line."). Never include task text, error text, or tool output. Respect `speak_responses`.
- **Tests.** D-13 xfail flips; each status; TTS-off; property test that spoken text ∈ constant set.
- **Security.** Untrusted text can never be spoken through this path; voice still cannot approve.
- **Acceptance.** G0 #8.

### T0.11 — Reproducibility *(S–M)*
- **Objective.** Reproducible environment and a guard against silent coverage loss.
- **Files.** `requirements.lock` (new), `.github/workflows/tests.yml` (likely), guard script/test, README dev section.
- **Reuses.** `pip freeze` of the live venv (read-only; 80 plain packages).
- **Approach.** Lock from freeze; document the single interpreter (venv 3.14.7); hermetic CI on `windows-latest` with the newest available Python (3.14 availability **[U]**), skipping hardware tests; guard: `collected ≥ baseline + new`, skips must not increase.
- **Tests.** Guard self-test.
- **Security.** Pinned versions; **no new dependency**.
- **Acceptance.** G0 #11; lock reproduces the environment (verified in a *new* venv only after owner approval).

**Hotfix packaging.** T0.3, T0.4, T0.5 and T1.1 are independent, small, in-place commits ⇒ cherry-pickable to `main` if the owner chooses (D1).

---

## 8. P1 Implementation Plan — Capability + Security Foundation (8 tasks)

*Order: T1.1 → T1.2 → T1.3 → T1.6 → T1.4 → T1.5 → T1.8 → T1.7.*

### T1.1 — Engine-protected roots *(M — do first)*
- **Objective.** V.O.I.D's own secrets and state are unreachable by any tool, regardless of `allowed_roots` (D-04, D-04b).
- **Files.** New `void/security/protected.py` (likely); `actions/files.py` (`_confine`, `_is_protected`, traversal in search/find/list); `actions/apps.py` (`open_path` local paths already use `_confine`); `app.py`, `config.py` (derive state dir/config dir); tests.
- **Reuses.** The single choke point `FileActions._confine/_is_protected`; config `protected_roots`; `roots.py` (add-only).
- **Approach (prototype-validated [V]).** Protected = state dir · repo `config/` (`local_config.yaml`) · Windows credential/vault/DPAPI stores · `~\.ssh`, `.aws`, `.gnupg` · browser profile secret stores · other users' profiles; write/delete-deny: `C:\Windows`, `Program Files*`, `ProgramData`. **Two independent layers**: (1) canonical — reject UNC/`\\?\` forms that don't normalise to a local drive path; `Path.resolve()` then case-insensitive containment; (2) identity — walk the candidate and its **nearest existing ancestors** and compare with `os.path.samefile` against each protected root/file. Any exception ⇒ deny. Search/list omit protected entries.
  *Prototype matrix (14 cases):* resolve+prefix alone was **bypassed by `\\?\` prefix, `\\localhost\C$` admin share, and a pre-existing hardlink**; adding the identity layer denied **all 14** (exact, case, `\\?\`, junction existing/new, trailing dot/space, ADS, dir-ADS, 8.3, UNC, `..`, mixed slashes, hardlink).
- **Tests.** The 14-case matrix plus non-existent-target-under-UNC/junction; junction-based replacements for the 3 skipped symlink tests; write-new-file into the state dir denied; search/list hide; D-04b and D-04 xfails flip; existing 57+32+17 tests.
- **Security.** Un-overridable, LLM-independent, fail-closed; owner can only add roots.
- **Acceptance.** G1 #2.

### T1.2 — Hash-chained audit log *(M)*
- **Objective.** Tamper-evident record of every authorisation decision and security event.
- **Files.** New `void/security/audit.py` (likely); `cli.py` (`audit verify|tail`); hooks in the engine (T1.7), gateway, kill-switch engage/clear, approve/deny.
- **Reuses.** `Config.state_dir`; `_summarize_args` redaction style; logging conventions.
- **Approach.** **Per-role files** (`runtime|cli|gateway`) `~/.void/audit/audit-<role>-YYYYMM.jsonl` because these are separate processes; record `{v, ts, seq, role, event, actor(channel), task_id, capability, risk_before, risk_after, taint, decision, args_summary(≤80, redacted), args_hash(salted SHA-256), ok, duration_ms, verified, prev}` with `prev = SHA-256(previous record)`; append under an OS file lock (`msvcrt.locking` on a sidecar) to serialise concurrent CLI processes; monthly rotation with head-hash carry-over; `audit.fail_closed` (default true): if the pre-event cannot be written, **effect-bearing actions are denied**, read-only tools proceed with a warning counter.
- **Tests.** Verify; edit/delete/reorder/truncate detection; rotation continuity; two-process concurrent appends; no-content property test; fail-closed; 10 k-record verify <1 s.
- **Security.** Integrity vs accidental/casual tampering only (same-user malware can rewrite files — stated, not hidden); no secrets or content.
- **Acceptance.** G1 #5.

### T1.3 — Tool manifest + argument validation *(M)*
- **Objective.** Every tool declares its security-relevant properties; malformed arguments are rejected deterministically before authorisation.
- **Files.** `actions/base.py` (`Tool`), `actions/registry.py`, `files.py`, `apps.py`, `computer.py`, tests.
- **Reuses.** Existing JSON-schema `parameters` on all 13 tools; `risk`, `risk_fn`, `terminal_on_success`.
- **Approach.** Additive optional fields with V1-safe defaults: `domain, effects, untrusted_output (None|"names"|"content"), timeout_s, offline_ok, path_args, verify, intents`. A small in-house validator for the schema subset V1 uses (`type`, `properties`, `required`, `enum`, `additionalProperties:false`) — **no `jsonschema` dependency**. Annotations per the classification table (readiness report §14.3). Failure message shape kept V1-compatible.
- **Tests.** Registry test: every tool annotated; validator units; invalid/unknown args; V1 tool tests unchanged.
- **Security.** Deterministic validation; unknown parameters denied.
- **Acceptance.** G1 #3.

### T1.6 — Task-store schema, taint column, retention *(M)*
- **Objective.** Versioned schema, persisted taint, bounded retention (D-06).
- **Files.** `core/task.py`, `cli.py` (`tasks prune`), config.
- **Reuses.** Additive `ALTER TABLE` pattern; tolerant `_row_to_task`.
- **Approach.** `schema_meta(version)` (v1 = current; v2 adds `taint`, `conversation_id`); **automatic backup copy before first migrate**; `Task.taint` as a persisted set; `tasks prune`: terminal tasks >90 d deleted (dry-run by default), tool-output message bodies >7 d replaced by `[redacted: N bytes]` (ledger kept); non-terminal never touched. Tested on **synthetic V1-shape fixtures** — the owner's real DB is not copied.
- **Tests.** Open/migrate V1 shape; V1 code reads migrated DB; redaction; dry-run; concurrency.
- **Security.** Reduces retained sensitive data; non-destructive by default.
- **Acceptance.** G1 #6.

### T1.4 — Taint model *(M)*
- **Objective.** Engine-owned, monotonic record of untrusted-content exposure; escalation of egress/privacy effects.
- **Files.** `core/task.py` (field), new `void/security/taint.py` (likely), tests. **`RiskGate` is unchanged.**
- **Reuses.** `RiskLevel` (IntEnum), ledger, resume path.
- **Approach.** Sources: `file_content` (from `read_file`), `memory_nonowner`; reserved names for V2.2 (`clipboard`, `vision`, `web`). Rules in code (not config): `egress ∧ taint ⇒ HIGH`; `channel=device ∧ eff ≥ HIGH ⇒ DENY`. `escalate()` returns `≥ base`. Names in listings do **not** taint (sanitised, framed).
- **Tests.** Seeded-random property test ≥20 k cases: eff ≥ base; taint never shrinks; voice never ALLOW at HIGH; survives resume.
- **Security.** LLM cannot read or modify it.
- **Acceptance.** G1 #4.

### T1.5 — URL policy *(S)*
- **Objective.** Close D-05 without breaking legitimate "open <site>".
- **Files.** New `void/security/urlpolicy.py` (likely); `actions/apps.py`; config `urls.allow_hosts`.
- **Reuses.** `open_path`, `risk_fn` mechanism.
- **Approach.** `urllib.parse`; scheme ∈ {http, https}; no `userinfo@`; no control characters; punycode/IDN hosts ⇒ HIGH; query+fragment ≤256 chars and entropy-checked; host allow-list; result ⇒ LOW / HIGH / DENY. Tainted ⇒ HIGH (via T1.4/T1.7).
- **Tests.** ≥25-case table (allowed, long query, high entropy, userinfo, control chars, punycode, `javascript:`/`file:`, tainted).
- **Security.** Reduces exfiltration; residual: an owner-approved or untainted short URL is allowed by design.
- **Acceptance.** G1 #4 (D-05 flips).

### T1.8 — Secret hygiene *(S)*
- **Objective.** New secrets cannot be overwritten via `set-key`; secret-shaped text is detected before storage/logging.
- **Files.** `cli.py` (`_reserved_names`), new `void/security/secretscan.py` (likely).
- **Reuses.** Existing reserved-name mechanism; alias regex forbids `:` so `device_secret:*` cannot collide.
- **Approach.** Reserve `memory_key` (+ audit salt key if stored in keyring); detector returns **categories only** (never matched text): API-key shapes, PEM headers, `password|token|pin` assignments, Luhn-valid card numbers, high-entropy ≥32-char tokens.
- **Tests.** Positive/negative corpus; reserved-name refusal.
- **Security.** Best-effort; false negatives possible and documented; review queue is the backstop.
- **Acceptance.** G1 #7.

### T1.7 — CapabilityEngine *(L — last in P1)*
- **Objective.** One authority boundary for the agent, fast path and device gateway.
- **Files.** New `void/core/capability.py` (likely); `core/agent.py` (`_run_call` delegates; `_commit_step` deferral uses `evaluate`); `app.py` (build engine, flag `capability_engine.enabled`); `device/capabilities.py` (`_run_tool_capability` → engine, `channel="device"`); `cli.py:717` (pass engine); tests.
- **Reuses.** `RiskGate.authorize` (unchanged; called with the engine's effective risk), ledger, durable confirmation, `owner_decision`, `_untrusted`, `KillSwitch`, `ToolResult`.
- **Approach.** `evaluate(call, ctx)` → `Decision{ALLOW|DEFER|DENY, base_risk, eff_risk, reasons}`: kill → manifest → schema → protected roots on `path_args`/URLs → risk (exception ⇒ HIGH, V1 rule) → taint/channel escalation → `RiskGate.authorize`. `execute()`: audit(pre) → run in a bounded worker with `timeout_s` (abandon on timeout, report failure) → verify → taint update → audit(post) → `ToolResult` + `verified`. `_run_call` keeps its signature/return tuple; flag off ⇒ V1 code path. App/window names are sanitised (control chars, length) before reaching speech/logs. Kill switch checked pre-exec and between steps (cooperative semantics preserved, **verified [V]**).
- **Tests.** Whole baseline passes with engine **on and off**; **differential test** (same scripted provider through both paths ⇒ identical `plan`, `messages`, statuses); timeout; verify outcomes; kill mid-tool; approval integrity (stored call set executes once); gateway parity; abuse cases S-01, S-04, S-05, S-06, S-11, S-12, S-13, S-14.
- **Security.** This *is* the boundary: the LLM cannot lower risk, remove taint, bypass authorisation, approve HIGH, declare success, or change policy.
- **Acceptance.** G1 #1, #3.

---

## 9. P2 Implementation Plan — Model Router + Offline Foundation (8 tasks)

### T2.1 — Provider interface extension *(M)*
- **Objective.** Capabilities, per-request options and usage/latency without breaking V1 providers.
- **Files.** `providers/base.py`; tests (contract suite).
- **Reuses.** `LLMProvider`, `LLMResponse`, `FakeProvider` (unchanged; new attributes default).
- **Approach.** Add `ProviderCapabilities`, optional `LLMRequest(purpose, deadline_s, thinking, max_output_tokens)`, `LLMResponse.usage/latency`; `generate(messages, tools=None, *, request=None)`.
- **Tests.** Contract suite runs against Fake, Gemini (stubbed SDK), Local (stub server).
- **Security.** No behavioural change.
- **Acceptance.** All three providers pass the contract suite; V1 tests unchanged.

### T2.2 — Gemini provider options *(M)*
- **Objective.** Deadlines, measured thinking control, optional client reuse, usage.
- **Files.** `providers/gemini_provider.py`; tests.
- **Reuses.** `CredentialPool` rotation, `_classify_error`, translation code (signatures untouched).
- **Approach.** `HttpOptions(timeout=…)` from the request deadline; extend error classes (timeout/connection/5xx); `thinking_level`/`thinking_budget` mapping behind `llm.gemini.thinking` (unset = V1 behaviour); **client reuse behind `llm.gemini.reuse_client`, default off** (V1 deliberately avoided retaining a client/key; reuse is a security/perf trade-off decided by measurement in T2.6); capture `usage_metadata`.
- **Tests.** Timeout enforcement against a local stub endpoint (`HttpOptions.base_url`) or a patched `generate_content`; classification; rotation unchanged; signatures round-trip unchanged.
- **Security.** Key still read on demand; no key in logs/telemetry.
- **Acceptance.** Deadline honoured within +0.5 s in tests; defaults reproduce V1.

### T2.3 — Local provider correction *(M)*
- **Objective.** Fix D-08 for the local model.
- **Files.** `providers/local_provider.py`; tests.
- **Reuses.** Existing Ollama translation/parsing.
- **Approach.** Request-driven top-level `think` (`false` for tool selection); `options.num_ctx` set explicitly; `keep_alive`; `(connect, read)` timeouts; `available()` = server up **and** model present (`/api/tags` names); `preload()` (empty generate with `keep_alive`); **context guard**: estimate size and truncate *tool-output bodies* (never the system prompt) when over budget, flagging `context_truncated`.
- **Tests.** Stub Ollama server: `think:false` sent; `num_ctx` sent; model-missing ⇒ unavailable; oversize output truncated; V1 tests.
- **Security.** Local only; outputs remain untrusted.
- **Acceptance.** No silent system-prompt truncation in the oversize test.

### T2.4 — ConnectivityMonitor *(S–M)*
- **Objective.** Know the network state before paying a timeout.
- **Files.** New `providers/connectivity.py` (likely); config.
- **Approach.** TCP+TLS probe to the **provider host only** (2 s timeout, ≈30 s interval, daemon thread); passive signals from router outcomes; `ONLINE|DEGRADED|OFFLINE` with hysteresis (3 failures ⇒ OFFLINE, 2 successes ⇒ ONLINE); disable-able; perf events.
- **Tests.** Fake clock/socket: transitions, flapping, disable.
- **Security.** Probes nothing else; no data sent.
- **Acceptance.** Deterministic state machine tests pass.

### T2.5 — ModelRouter *(L)*
- **Objective.** Per-call provider selection with deadlines, breaker, failover and safe handoff (D-07).
- **Files.** New `providers/router.py` (likely); `app.py` (`_agent()`, flag `llm.router.enabled`); `core/agent.py` (`_generate_with_retry`, outage mapping); tests.
- **Reuses.** `ProviderRegistry` (provider construction), `CredentialPool`, single call site `agent.py:150`.
- **Approach.**
  - **Selection:** by purpose + connectivity; ONLINE→cloud, DEGRADED/OFFLINE→local.
  - **Failure classes → action:** timeout/5xx/connection ⇒ mark unhealthy (breaker: 3 failures, 30 s open, half-open probe) → one jittered retry only if deadline remains → failover; 429 ⇒ rotate credential (existing) then local; 401/403 ⇒ cool credential; **no sleep after the last attempt**.
  - **Deadlines:** placeholders `tool_select 8 s`, `final_answer 12 s`, `local 30 s` **[E]**, final values from T2.6.
  - **Safe handoff (no fabricated metadata):** Gemini→Gemini across credentials: permitted (signature semantics assumed model-bound **[U]** — verified by an owner-attended test); Gemini→Local: permitted (signatures ignored); **Local→Gemini mid-task: never replay history** — rebuild `[system, goal, "Progress so far" block generated by the engine from `Task.plan` and last tool-result summaries, framed as data]`; if a tool call is mid-flight, complete or pause first; otherwise **pause** (`PAUSED`, resumable).
  - **Outage:** all providers unavailable ⇒ router raises `ProviderOutage` ⇒ Agent sets `PAUSED` + spoken status (behind the flag; flag off keeps V1 `FAILED`).
  - **Egress:** target-specific prompt rendering enforces `cloud_ok` (used by P3).
  - **Kill:** router wait loop polls the kill switch and cancels in-flight HTTP.
- **Tests.** Fault injection with a fake clock: timeout, 5xx, 429 rotation, offline, flapping, exhaustion, kill mid-call; D-07 xfail flips; handoff tests (Local→Gemini rebuild; no signature invented); single-turn worst-case bound; flag-off parity.
- **Security.** Authorisation identical for every provider; fallback never widens egress; no tool acts on a partial response.
- **Acceptance.** G2 #1–4.

### T2.6 — Evaluation harness and measurements *(M)*
- **Objective.** Decide defaults with data, not hypotheses.
- **Files.** `verify/eval_router.py`, `verify/measure_local_vram.py` (likely; `verify/` is the repo's convention for manual/live scripts); golden set stored **outside the repo** if private.
- **Reuses.** `ollama_bench.py`/`stt_bench.py` prototypes.
- **Approach.** ~50 owner commands + adversarial, expected first tool + normalised args; run per provider/config ⇒ **tool-selection accuracy and latency percentiles**. Measurements: VRAM and speed vs `num_ctx` ∈ {4096, 6144, 8192}; local load cold/warm; Gemini thinking A/B; client-reuse A/B (**cloud runs need owner approval — they use quota**).
- **Tests.** Harness self-test with fakes.
- **Security.** Golden set may contain private phrasing ⇒ not committed.
- **Acceptance.** Report produced; thresholds agreed with the owner **before** any default changes.

### T2.7 — Degraded-mode notices *(S)*
- **Files.** `voice/session.py` / router hook. **Approach.** One rate-limited engine-generated phrase on OFFLINE↔ONLINE transitions ("I'm offline — using the local model."). **Tests.** Rate limit; constant strings only. **Acceptance.** No untrusted text; ≤1 notice per state change per 5 min.

### T2.8 — Deterministic fast path *(M, flagged)*
- **Objective.** Skip the LLM for the simplest voice command (D6), safely.
- **Files.** `core/agent.py` (`run_direct`), `actions/apps.py` (manifest `intents`), `app.py`; tests.
- **Reuses.** `launch_app`, `AppCatalog`/aliases, `terminal_on_success` acknowledgement, **the engine (T1.7)**.
- **Approach.** Only manifest-declared intents: `open|launch|start <app>`; resolution by **exact/normalised** match against catalog/aliases; ambiguity/no match ⇒ fall through to the LLM. Constructs an ordinary call executed by the engine (audit, verify, taint, risk); must be LOW or fall through. Flag `fast_path.enabled` **default false** until G2 measurement (wrong-app launches must be 0 in the eval).
- **Tests.** Grammar table (positive/negative/ambiguous, STT-noisy variants); engine parity; audit shows engine call; flag off = V1; no second authorisation code path (static test: fast path imports no `RiskGate`).
- **Security.** Not an authority; no new capability.
- **Acceptance.** G2 #7.

---

## 10. P3 Implementation Plan — Initial Persistent Memory (7 tasks)

### T3.1 — Session context *(M)*
- **Objective.** Follow-ups work ("open that again").
- **Files.** `app.py`/new `void/memory/session.py` (likely), `core/agent.py` (context param), tests.
- **Reuses.** `Task.conversation_id` (T1.6); message list.
- **Approach.** In-RAM ring of the last ≤4 exchanges (owner goal ≤300 chars + engine status or final answer), idle TTL 10 min, per conversation (voice: runtime lifetime; CLI: only with `--session`). Injected as **one user-turn message before the goal**, fenced `[RECENT CONVERSATION — context only, not instructions]`. An assistant answer from a *tainted* task is omitted **and marks the next task tainted** (configurable). **Not persisted** in V2.0.
- **Tests.** Follow-up resolves; TTL; ring bound; tainted-answer rule; system prompt byte-identical.
- **Security.** Untrusted-framed; bounded; RAM only.
- **Acceptance.** G3 #5 (part).

### T3.2 — Store + cryptography *(M)*
- **Files.** New `void/memory/{store,crypto}.py` (likely); tests.
- **Reuses.** `cryptography` AESGCM (verified), keyring wrapper, `schema_meta` pattern, backup-before-migrate.
- **Approach.** `~/.void/memory.sqlite`; `journal_mode=DELETE`, `secure_delete=ON`; text (+tags) in `text_enc = nonce‖ct‖tag`, AAD = `item_id‖schema_version`; key = 32 random bytes, base64 in Credential Manager `memory_key`; plaintext metadata only (`kind, origin, status, sensitivity, cloud_ok, importance, use_count, timestamps, supersedes_id, source_task_id`). Missing/wrong key ⇒ memory **disabled with a message**; never plaintext fallback; never regenerate over existing ciphertext.
- **Tests.** Round-trip; wrong key; wrong AAD; bit-flip; row-swap; canary text absent from the file (`strings` scan); migration on a synthetic DB; independence from `tasks.sqlite`.
- **Security.** §14 memory analysis.
- **Acceptance.** G3 #1.

### T3.3 — Write policy *(M)*
- **Files.** New `void/memory/policy.py` (likely).
- **Reuses.** T1.8 secret detector; T1.4 taint.
- **Approach.** Origin/channel matrix: CLI/UI explicit ⇒ `active`/`owner_stated`; LLM `propose_memory` ⇒ `proposed`; from a **tainted** task ⇒ `quarantined`; voice-originated ⇒ `proposed` (opt-in auto-accept for non-sensitive `preference`, default off); owner accept ⇒ `active`/`owner_confirmed`. Gate: reject secrets; classify `sensitive` (`cloud_ok=0`); length cap; dedupe/supersede; ≤3 proposals/task. **No LLM-reachable path creates `active`.**
- **Tests.** Full matrix; secret corpus; caps; quarantine never retrievable.
- **Security.** Poisoning defence.
- **Acceptance.** G3 #2.

### T3.4 — Retrieval *(M)*
- **Files.** New `void/memory/index.py`, `context.py` (likely).
- **Approach.** In-RAM BM25 (stdlib) over decrypted `active` rows built at startup; score = BM25 + recency + importance + use_count; score floor; **top-k ≤5 and ≤≈400 tokens** hard cap; block injected as a user-turn message with structured `meta` (ids, `cloud_ok`) so the router can re-render per provider; non-owner origins taint the task. **No FTS table, no vector index** (nothing on disk to leak or fail to delete).
- **Tests.** Determinism; budget; floor; injection fence; taint; micro-benchmark at 10³–10⁴ items **[E target p95 < 20 ms]**.
- **Security.** Memory is data; no path into `authorize()`.
- **Acceptance.** G3 #3.

### T3.5 — CLI operations *(M)*
- **Files.** `cli.py`.
- **Approach.** `memory list | show <id> | remember "…" | forget <id|--all> | correct <id> "…" | review` (interactive accept/reject); each emits an audit event (metadata only); `forget` via voice is a *request* only. Reserved keyring names enforced.
- **Tests.** Each command; audit events; provenance shown by `show`; no text in audit.
- **Acceptance.** G3 #5.

### T3.6 — Agent and router integration *(M)*
- **Files.** `core/agent.py`, `app.py`, `providers/router.py`, manifest for `propose_memory`.
- **Reuses.** Engine (T1.7), taint (T1.4), router (T2.5).
- **Approach.** `propose_memory(text, kind)`: LOW, proposal-only, engine-validated; context injection before the goal; router strips/re-renders memory blocks per target provider (`cloud_ok=0` never in a cloud prompt).
- **Tests.** S-02, S-08, S-18; system prompt byte-identical; provider-switch egress.
- **Security.** LLM cannot activate memory, cannot read quarantined items.
- **Acceptance.** G3 #2.

### T3.7 — Retention and deletion semantics *(S–M)*
- **Files.** `memory/store.py`.
- **Approach.** `forget` = row `DELETE` + `VACUUM` + index purge + audit; retention (episodic 90 d unless pinned, proposals 30 d). **Stated guarantees:** *logical* deletion, DB-file overwrite via `secure_delete`, no plaintext or index remnants **in the DB file**. **Not guaranteed:** physical destruction on the SSD (wear levelling), NTFS/VSS shadow copies, pre-migration **backup copies** (ciphertext, recoverable by whoever holds the key), plaintext copies in process RAM/page file, cloud prompts already sent. Optional key rotation (🟡) forward-protects against *future* copies only.
- **Tests.** Deletion completeness in the DB file (canary absent; index empty; `VACUUM` ran); expiry; audit; documented-limits text asserted in CLI output.
- **Acceptance.** G3 #4.

---

## 11. Dependency Graph

```
 T0.1 isolation ──▶ T0.2 xfail tests ─┬▶ T0.3 D-01 ─┐
      │                               ├▶ T0.4 D-02  │
      │                               ├▶ T0.5 D-03/09 (prototype-validated)
      │                               ├▶ T0.9 stale sweep
      │                               ├▶ T0.10 spoken status ─────────────────────────────▶ T2.7
      │                               └▶ T1.1 PROTECTED ROOTS (may ship early / cherry-pick)
      ├▶ T0.6 rotating log + perf sink ─▶ T0.7 interaction_id ─▶ T0.8 health/report/doctor ─▶ (T2.5, T2.6 data)
      │         └───────────────────────▶ T1.2 audit log
      ├▶ T0.11 lock / CI / guards
      └▶ T1.6 task-store schema+taint+retention
                                         ═══ G0 (owner review) ═══
 T1.3 manifest+validation ─▶ T1.4 taint ─▶ T1.5 URL policy ─┐
 T1.6 ───────────────────────┘        T1.8 secret hygiene ──┤
 T1.1, T1.2 ─────────────────────────────────────────────────┴▶ T1.7 CAPABILITY ENGINE ═══ G1 ═══
 T2.1 provider iface ─▶ {T2.2 Gemini, T2.3 Local} ─▶ T2.4 monitor ─▶ T2.5 ROUTER ─▶ T2.6 eval/measure
                                                                         │                 │
                                                       T1.7 ─────────────┴▶ T2.8 fast path (gated) ═══ G2 ═══
 T3.1 session ctx (needs T1.6)          T3.2 store+crypto (needs T1.2, T1.8) ─▶ T3.3 policy (needs T1.4)
        └──────────────────────────────────────▶ T3.4 retrieval ─▶ T3.5 CLI ─▶ T3.6 integration (needs T1.7, T2.5) ─▶ T3.7 ═══ G3 = V2.0 ═══
```

**Critical path:** T0.1 → T0.2 → T0.6 → T1.2 → T1.7 → T2.5 → T3.6 → G3.
**Adjustment from the sketch P0→P1→P2→P3:** technically P2 (T2.1–T2.6) needs only P0's telemetry; only T2.8 needs the engine, and P3 needs both P1 and P2. I keep P1 before P2 (security before capability growth; the router is not a capability) but flag T2.1–T2.4 as safely reorderable if the owner prioritises resilience.

---

## 12. File Impact Map

### 12.1 Likely modified (all read in this task)

| File | Change | Tasks |
|---|---|---|
| `tests/conftest.py` | Isolation fixtures, markers | T0.1 |
| `void/voice/runtime.py` | Supervisor liveness; heartbeat; perf events | T0.3, T0.7, T0.8 |
| `void/voice/adapters.py` | Full-chord PTT; STT timing emit | T0.4, T0.7 |
| `void/voice/session.py` | Interaction id; spoken status; degraded notices | T0.7, T0.10, T2.7 |
| `void/device/gateway.py`, `void/device/auth.py` | Lazy handshake, timeouts, cap, limiter, sampled logs, counters, engine routing | T0.5, T0.8, T1.7 |
| `void/device/capabilities.py` | `_run_tool_capability` → engine (allow-list unchanged) | T1.7 |
| `void/core/kill_switch.py` | `compare_digest` (semantics unchanged) | T0.5 |
| `void/runtime/diagnostics.py` | Rotating handler | T0.6 |
| `void/core/task.py` | `sweep_stale`, `schema_meta`, `taint`, `conversation_id`, retention | T0.9, T1.6 |
| `void/actions/files.py` | Protected roots in `_confine/_is_protected`; traversal filtering; manifest | T1.1, T1.3 |
| `void/actions/apps.py` | URL policy; manifest/intents | T1.5, T1.3, T2.8 |
| `void/actions/computer.py`, `actions/base.py`, `actions/registry.py` | Manifest fields, validator | T1.3 |
| `void/core/agent.py` | `_run_call` delegate; `_commit_step` uses `evaluate`; router hook; `run_direct`; context param; outage mapping; thin perf emits | T0.7, T1.7, T2.5, T2.8, T3.1 |
| `void/app.py` | Build engine/router/memory/audit behind flags; `run(goal, channel=…)` | T0.7, T1.7, T2.5, T3.x |
| `void/providers/base.py`, `gemini_provider.py`, `local_provider.py`, `registry.py` | Interface extension; deadlines/thinking/`think`/`num_ctx` | T2.1–T2.3, T2.5 |
| `void/cli.py` | `perf`, `doctor`, `audit`, `tasks prune`, `memory …`; `_reserved_names`; pass engine to gateway | T0.8, T1.2, T1.6, T1.8, T1.7, T3.5 |
| `void/roots.py`, `void/config.py` | Derive protected paths; new config sections | T1.1 |
| `config/default_config.yaml` | Additive sections with **safe defaults** (`capability_engine`, `llm.router`, `audit`, `memory`, `fast_path`, `logging`, `perf`, `device.max_connections/connection_timeout_s`, `urls`) | many |
| `tests/test_protected_roots.py`, `test_files.py`, `test_device_gateway.py`, `test_voice.py`, `test_agent.py`, `test_providers.py` | Extended, **never weakened** | various |
| `README.md`, `V.O.I.D_ARCHITECTURE_REVIEW.md`, `android-companion/README.md` | Correct stale statements (at G3) | docs |

### 12.2 Likely created (names are proposals)

`tests/test_regression_v1_defects.py` · `void/perf/{__init__,log,report}.py` · `void/security/{protected,audit,taint,urlpolicy,secretscan}.py` · `void/core/capability.py` · `void/providers/{router,connectivity}.py` · `void/memory/{store,crypto,policy,index,context,session}.py` · `verify/eval_router.py`, `verify/measure_local_vram.py` · `requirements.lock` · `.github/workflows/tests.yml` · new `tests/test_*` per module (engine, audit, taint, URL policy, protected roots matrix, router, connectivity, memory ×4, perf, doctor).

### 12.3 Must **not** be modified in V2.0

| Path / thing | Why |
|---|---|
| **`main`** and the live runtime, its venv (`C:\V.O.I.D\.venv`), `~/.void`, Credential Manager, the `VOID_VoiceRuntime` scheduled task, `device serve` process, firewall | Live V1; owner-only operations |
| `void/security/risk.py` (`RiskGate`) | Preserved by requirement; engine passes the effective risk in |
| `void/device/{protocol,cert,pairing,identity}.py`; HMAC/replay functions in `auth.py`; the closed allow-list in `capabilities.py` | TLS/pin/HMAC/replay/allow-list are non-negotiable; V2.0 changes only availability limits and routes execution through the engine |
| `void/voice/state.py` (reducer), `wake.py`, `whisper_gen3_wake.py`, `tts.py`, SAPI class | Working V1 voice core; V2.1 territory |
| `void/runtime/scheduled_task.py`, `autostart.py` | Live persistence; no changes |
| `void/ui/*` | Mirrors state; no authority; V2.1 for approval prompt |
| `android-companion/` | Keystore is V2.1 |
| Existing `verify/*` scripts, `examples/`, `wakeword-training/`, `config/local_config.yaml` | Manual tools / untracked / machine-local |
| **Existing tests** | Never deleted or weakened; skips may only decrease |
| `requirements*.txt` (except adding the lock file) | **No new dependencies** |

---

## 13. Database / Migration Plan

**Nothing is migrated in this task.** Development uses a separate absolute `app.state_dir` [V]; the owner's real databases are migrated only when the owner later runs V2 code against them.

| Store | Today | V2.0 | Version / migration | Retention |
|---|---|---|---|---|
| `tasks.sqlite` | 1 table, ad-hoc `ALTER TABLE`, 103 rows, plaintext | + `schema_meta`, `taint`, `conversation_id` (**additive**) | v1 (implicit) → v2 | Terminal >90 d prune (dry-run default); tool-output bodies >7 d redacted; stale `running` swept |
| `memory.sqlite` | — | New, separate file | `schema_meta` v1 | Episodic 90 d; proposals 30 d; semantic until forgotten |
| `audit/audit-<role>-YYYYMM.jsonl` | — | New, per-role, hash-chained | record `v:1` | 12 months |
| `perf/perf-YYYYMM.jsonl` | — | New, schema-restricted | `v:1` | 90 days |
| `health.json` | — | New, atomically replaced | `v:1` | Overwritten |
| `void.log` | non-rotating | Rotating (5×5 MB) | — | Bounded |
| `devices.json`, `device_cert/key.pem`, `pairing_window.json` | as V1 | **Unchanged** (protected roots make them tool-unreachable) | policy v2 is V2.1 | — |
| Credential Manager (`void`) | Gemini keys, PIN, `device_secret:*` | + `memory_key` (reserved) | — | — |
| `backups/` | — | New | `<store>.<UTC timestamp>` | Keep last 5 per store |

**Strategy.** (1) *Additive only*: new columns/tables, never drop or rewrite existing ones. (2) *Backup first*: before the first migration of any store, copy it to `~/.void/backups/`, then `PRAGMA integrity_check` on the copy. (3) *Idempotent*: re-running a migration is a no-op. (4) *Tested on synthetic V1-shape fixtures* generated by the test suite — the owner's real DB is not copied into scratch. (5) *V1-compatible*: V1 code must still open a migrated `tasks.sqlite` (tested), so code rollback needs no data rollback.

**Rollback.** Stop the runtime → restore the backup file → run V1 code (or set the feature flag off). `memory.sqlite`, `audit/`, `perf/` are independent files that can be moved or deleted without affecting checkpoints. **No destructive migration is proposed anywhere.**

---

## 14. Security Model

| Topic | V2.0 design |
|---|---|
| **Authentication** | Owner = the local OS user (CLI/dev UI). Device = TLS + pinned cert + per-device HMAC (unchanged). PIN compare becomes constant-time. Provider credentials stay in Credential Manager; router handles names, not values. |
| **Authorization** | Only the `CapabilityEngine` decides (ALLOW/DEFER/DENY) via the §6/T1.7 procedure; `RiskGate` unchanged; approval only via CLI `approve/deny` (`owner_decision`); approval executes the *stored* call set once. Voice, fast path, router, memory and device input have **no** approval authority. |
| **Capability restrictions** | 13 existing tools + `propose_memory` (proposal-only). Manifest-declared; schema-validated; protected roots; URL policy; **no new exec-capable tool**. Device allow-list stays closed (`get_status`, `launch_app`). |
| **Risk** | Static or `risk_fn` (exception ⇒ HIGH, V1 rule); escalations only raise. Existing V1 risk assignments unchanged. |
| **Taint** | Engine-owned monotonic set on `Task`; sources `file_content`, `memory_nonowner`; effects `egress` (V2.0), `privacy` (V2.2). |
| **Confirmation** | HIGH ⇒ deferred `AWAITING_CONFIRMATION`; CLI approve/deny; **never voice** (D13); device HIGH ⇒ deny; spoken notice tells the owner (constant phrases). On-screen prompt is V2.1. |
| **Audit** | Hash-chained per-role JSONL; fail-closed for effect-bearing actions if the pre-event cannot be written; content-free; honest limit: same-user malware can rewrite files. |
| **Secrets** | Keyring only; protected roots keep state files out of tools; reserved names; detector categories only; no secrets in perf/audit/logs; `memory_key` never logged. |
| **Filesystem boundaries** | `allowed_roots` (config) ∩ **engine-protected roots** (two-layer, un-overridable, fail-closed) ∩ V1 protected roots. Owner can only add. |
| **Cloud egress** | Today everything the agent reads goes to Gemini. V2.0: protected data never reaches tools (so never the cloud); memory items carry `cloud_ok` (sensitive ⇒ 0) enforced **per target provider** by the router; no audio, no camera; telemetry stays local. Ordinary owner-requested file reads still reach the cloud (that is the V1 product) — see D4. |
| **Kill switch** | Cooperative semantics preserved [V]; checked pre-exec, between steps, and by the router wait loop (aborts in-flight HTTP); timeouts abandon calls; audit `killed`. |
| **Failure behaviour** | Validation/engine error ⇒ deny/HIGH; audit failure ⇒ deny effect-bearing; router: bounded failover or `PAUSED`; memory key problems ⇒ memory disabled, never plaintext; gateway timeouts fail closed. |
| **Privilege** | Runs as the interactive user (limited token); no elevation, no service, no shell. |

### 14.1 Memory security analysis

| Aspect | Design | Honest limit |
|---|---|---|
| **Encryption** | AES-256-GCM per item; AAD binds ciphertext to its row/version | Protects copied files/backups only; a same-user process that can read Credential Manager can decrypt |
| **Key storage** | Windows Credential Manager (`memory_key`, DPAPI, per-user) | Key material also lives in process RAM while running |
| **Metadata** | Plaintext: kind, origin, status, sensitivity, timestamps, counters | Reveals activity patterns and approximate counts; ciphertext length leaks approximate text size |
| **Deletion** | **Logical deletion** (row `DELETE`) + `secure_delete=ON` + `VACUUM` + index purge + audit | **Not a guarantee of physical destruction** (below) |
| **WAL/index remnants** | `journal_mode=DELETE` (no WAL file); **no on-disk index** (in-RAM BM25) | RAM/page file may hold decrypted text |
| **Not covered by deletion** | — | SSD wear-levelling; NTFS/VSS shadow copies; pre-migration backups (ciphertext + a key still valid); prompts already sent to the cloud; OS crash dumps |
| **Provenance** | `origin`, `source_task_id`, timestamps; `memory show` answers "why do you believe this?" | Provenance is only as good as the writer's honesty; owner review is the backstop |
| **Taint** | Non-owner origins taint any task that retrieves them; tainted writes quarantined | Owner may accept a false proposal |
| **Prompt injection** | Fenced, labelled untrusted block in the user turn, budget-capped; system prompt constant; **no memory value reaches `authorize()`** | Model may still follow injected text for LOW-risk actions |
| **Secret detection** | Pattern/entropy detector before write; `proposed` default for voice; owner review | False negatives exist; documented |
| **Cloud egress** | `cloud_ok` per item, per-provider re-rendering; `sensitive` default 0 | A `normal` memory retrieved for a cloud call *does* leave the machine (D4) |
| **Retention** | Episodic 90 d; proposals 30 d; unused semantic flagged at 180 d | — |
| **Correction** | New superseding version; old marked `superseded` until purge | Old version remains until purged |
| **Review** | `memory review` (CLI) accept/reject; voice cannot accept | — |
| **Audit** | Write/accept/reject/delete/retrieve(ids)/key-missing events, no text | — |

**Guarantees we make:** encrypted-at-rest text in the DB file; deleted items absent from the DB file and index after `forget`; memory can never authorise or alter instructions (structural). **Guarantees we do not make:** physical erasure, protection from same-user malware, protection of data already sent to a provider.

---

## 15. Testing Strategy

### 15.1 Test types and exact tests

| Type | Exact tests (likely file) | Pass criterion |
|---|---|---|
| **Unit** | Manifest/validator; taint escalation; URL policy (≥25 cases); protected-path matcher; secret detector corpus; BM25 scoring; crypto; audit record/chain; router state machine; breaker/hysteresis; retry policy; phrase table; interaction-id/ perf schema | All pass; no wall-clock sleeps (injected clocks) |
| **Integration** | Agent + engine + `FakeProvider`; router + fake providers; gateway over loopback TLS; memory end-to-end (in-memory keyring); voice session with fakes end-to-end emitting one perf chain | All pass |
| **Regression (V1)** | The 883 baseline tests, unchanged, run **engine on and off** and with all V2 flags off | 878 pass / ≤5 skip; count guard |
| **Differential** | Same scripted provider through V1 `_run_call` and the engine ⇒ identical `Task.plan`, `Task.messages`, status | 100 % parity |
| **Security / abuse** | §18 S-01…S-18 | Attack fails **and** is audited |
| **Fault injection** | Provider timeout, 5xx, 429, offline, flapping, credential exhaustion, kill mid-call; audit write failure; DB locked; keyring failure; disk full (audit/perf) | Defined terminal state; no unhandled exception; no unsafe action |
| **Provider failover** | Cloud→local, local→cloud (rebuild), Gemini→Gemini across credentials; outage⇒`PAUSED`; flag-off = V1 `FAILED` | Per §9 T2.5 |
| **Taint property** | ≥20 k seeded cases: `eff ≥ base`; taint monotone; voice never ALLOW at HIGH; device HIGH ⇒ DENY; survives resume | 0 violations |
| **Protected roots** | 14-case matrix + non-existent-target variants + search/list hiding + junction tests | 100 % denied |
| **URL policy** | Table incl. tainted | 100 % as specified |
| **Audit integrity** | Edit/delete/reorder/truncate/rotate; two-process append; fail-closed; 10 k verify | Detected; <1 s |
| **Memory poisoning** | Tainted write ⇒ quarantine; injection text in items; false-fact proposal; secret shapes; caps | Never `active`/retrievable without review |
| **Memory deletion** | Canary text absent from DB file after `forget`; index empty; expiry; audit present; CLI states limits | As stated |
| **Memory crypto** | Round-trip; wrong key; wrong AAD; bit-flip; row swap; key missing ⇒ disabled | `InvalidTag`/disabled; no plaintext |
| **Offline** | Local-only run with the cloud stub unreachable; monitor transitions; notices; local `think:false` and `num_ctx` sent | Completes or `PAUSED` |
| **Router** | §9 T2.5 list | Worst-case single-turn bound |
| **Fast path** | Grammar table; ambiguous ⇒ LLM; engine parity; static "no RiskGate import" | 0 wrong-app launches in eval |
| **Runtime smoke (owner-attended)** | Dev-isolated runtime start/stop; mic-permission toggle recovery; network-cut offline run; gateway soak; `doctor`/`perf report` on a real session | Recorded, not asserted by me |
| **Performance** | §16 | Report, not pass/fail, until thresholds agreed |

### 15.2 Rules

- **Repro-first:** each defect test is written failing before its fix (T0.2).
- **Liveness rule** (from D-01): retry/supervisor tests must assert the second and Nth attempt.
- **Isolation:** T0.1 fixture applies to every test; hardware/socket/real-keyring tests are marked and excluded by default.
- **Junctions, not symlinks,** for boundary tests.
- **No new dependencies:** property tests use seeded `random`.
- **Coverage guard:** `collected ≥ baseline + new`; skipped count may only fall.

---

## 16. Performance Measurement Plan

**Nothing is claimed as achieved.** Existing figures are a small sample (n=6 interactions, 11 LLM calls) and are used only to decide *what to measure*.

### 16.1 Telemetry (T0.6–T0.8)

Per interaction (`interaction_id`): `activation → endpoint(reason, capture_s) → stt(audio_s, decode_s, model, threads) → route(provider, reason) → llm(attempt, duration_s, tokens, tool_calls) → tool(name, risk, duration_s, ok) → verify(ok) → respond(kind) → complete(status, total_s) → speak(start_s, dur_s)`. Process health: heartbeat (mic frame age, wake inference Hz, state). Gateway: per-minute counters. **No content fields** (schema-enforced).

### 16.2 Datasets

| Dataset | Content | Size | Notes |
|---|---|---|---|
| **Real interactions** | Telemetry from normal use | ≥100 interactions (≥1 week) before any target is frozen; ≥300 to report p99 | Privacy-safe by construction |
| **Golden tool-selection set** | Owner commands + adversarial | ≈50 | Kept outside the repo if private |
| **Synthetic audio** | SAPI-generated command clips | 30 commands × available voices | Valid for **compute/latency only, not accuracy** |
| **Injection corpus** | File/window-title/memory strings | ≈30 | Security tests |
| **Memory corpus** | Synthetic items | 10³ and 10⁴ | Index build/query timing |

### 16.2b Metrics and statistics

Percentiles p50/p95 per stage and per command class; **p99 only when n ≥ 300**; cold vs warm reported separately (≥5 cold, ≥30 warm repetitions for benchmarks); environment recorded (AC/battery, power plan, other GPU load, live-runtime state) because this is a laptop with a live voice runtime.

### 16.3 Resource measurements

| Resource | Method | When |
|---|---|---|
| CPU | `psutil` per-process % at 1 Hz: idle 10 min (wake armed), during interactions, during STT | T0.8, V2.1 |
| VRAM | `nvidia-smi` at 1 Hz; `ollama ps`; matrix `num_ctx` ∈ {4096, 6144, 8192} with V1's real prompt + a large tool output | T2.6 |
| Disk | Bytes/day of `void.log`, `perf`, `audit`, `tasks.sqlite`, `memory.sqlite` | Continuous |
| Local model load | Disk-cold vs OS-cache-warm; preload lead time | T2.6 |
| Memory index | Build/query at 10³/10⁴ items | T3.4 |

### 16.4 Questions the measurements must answer *(current status)*

| # | Question | Status now |
|--:|---|---|
| 1 | Why is live STT ≈2× slower than isolated (1.2 s)? | **[U]** |
| 2 | Real command distribution (fast-path go/no-go) | **[U]** |
| 3 | Wake steady CPU and inference cadence | **[E]** ≈2 cores, 2–3 Hz |
| 4 | Cloud thinking effect on the 35.9 s tail | **[E]** |
| 5 | Client-reuse network benefit | **[U]** |
| 6 | Local tool-selection accuracy beyond 6 trials | **[U]** |
| 7 | VRAM vs `num_ctx` | **[U]** (only ctx 4096 measured: 5.6 GB) |
| 8 | Router worst-case single-turn bound under stall | **[U]** — target ≈`deadline + local` **[E]** |
| 9 | Memory index p95 at 10⁴ items | **[E]** < 20 ms |

### 16.5 Reporting

`void perf report` emits per-stage tables; a threshold is agreed with the owner only after the relevant dataset exists; a regression alarm (`doctor`) flags a stage whose p95 exceeds the agreed budget.

---

## 17. Rollback / Recovery

**V1 stays recoverable at every step.**

| Mechanism | Detail |
|---|---|
| Baseline | Commit `017e0f6…` is immutable history; `main` and `origin/main` point at it. There is **no tag**; I recommend (not performed) a local lightweight tag `v1-baseline` for convenience — an owner-approved, purely additive action. |
| Branch/worktree isolation | All V2 work is on `claude/void-v2-initialization-57bfb6` in its own worktree. Git refuses to check out `main` in that worktree (it is checked out at `C:/V.O.I.D`); the V2 branch has **no upstream**, so a bare `git push` cannot reach `main`; no hooks are configured. Nothing is committed to `main`. |
| Small commits | One task ≈ one or a few commits; P0 fixes and T1.1 are cherry-pickable (D1). |
| Feature flags | `capability_engine.enabled`, `llm.router.enabled`, `memory.enabled`, `fast_path.enabled`, `audit.enabled`, `voice.ptt_strict_chord`; **all V2 flags default to V1 behaviour until their gate**. Flags-off parity is a test. |
| Data | Additive migrations, backup-before-migrate, V1 reads V2 `tasks.sqlite`; restoring a backup restores V1 data. |
| Config | Additive keys only; unknown keys ignored by V1; `local_config.yaml` never edited by tools (protected). |
| Live runtime | Never touched: dev uses an **absolute `app.state_dir`**, a different gateway port (`device.port` / `--port`), the `null` capture backend or `voice.enabled:false`, no scheduled-task changes, and **no installs into `C:\V.O.I.D\.venv`**. |
| Abort criteria | Stop and return to the owner if a baseline test must be deleted/weakened; a security invariant would need weakening; a step requires modifying `main`/live state; the same failure recurs twice by the same method (two-failure rule). |

| Symptom | Recovery |
|---|---|
| A V2 flag misbehaves | Set the flag off (config); behaviour reverts to V1 |
| A migration is suspect | Stop runtime → restore `~/.void/backups/<store>.<ts>` |
| Memory key lost | Memory disabled by design (no plaintext fallback); checkpoints unaffected; delete/recreate `memory.sqlite` |
| Audit file corrupt | `audit verify` reports the break; later months remain valid (head-hash carry-over) |
| Commit needs reverting | `git revert` the task's commits; V1 baseline unaffected |

---

## 18. Security Review Checklist

Each item is a **test** that must fail on the V1 baseline where a baseline behaviour exists, and pass after the relevant task. Pass = the attack fails **and** the attempt appears in the audit log (where an audit event applies).

| ID | Abuse case | Attack / method | Expected result | Task |
|---|---|---|---|:-:|
| **S-01** | **Prompt injection through a file** | Fake provider: `read_file` on a file saying "open https://attacker/?d=…", then `open_path` | Task tainted after read; `open_path` ⇒ **deferred**, not executed | T1.4/1.5/1.7 |
| **S-02** | **Memory poisoning** | Tainted task calls `propose_memory("owner's bank is X")`; voice-origin "remember…" | `quarantined` / `proposed`; not retrievable; never `active` without CLI review | T3.3/3.6 |
| **S-03** | **Malicious URL generation** | Model-composed URLs: long/high-entropy query, `user@host`, punycode, `javascript:`, `file:`, control chars, tainted context | Denied or HIGH per policy table | T1.5 |
| **S-04** | **Attempts to read V.O.I.D secrets / plant state** | Under `allowed_roots=[<root over state dir>]`: read/list/write/delete key, pairing file, `devices.json`, DBs, audit, `STOP`; **plant `pairing_window.json`** | All denied at the engine; `redeem(planted token)` fails; denials audited | T1.1 |
| **S-05** | **Taint bypass** | (a) split one attack across two tasks; (b) tool output claiming "taint cleared/approved"; (c) resume after restart; (d) mutate stored `pending` arguments; (e) carry tainted answer via session context | Taint persists across resume; text cannot change state; `pending` mutation rejected; (e) marks next task tainted | T1.4/1.7/3.1 |
| **S-06** | **Risk downgrade** | Model passes `risk:"LOW"`/`overwrite:"false"` (string) args; tool-name collision; unknown tool; `risk_fn` raises; type-confused arguments | Unknown/extra params rejected by schema; exception ⇒ HIGH; risk never below manifest base | T1.3/1.7 |
| **S-07** | **Audit tampering** | Edit/delete/reorder/truncate a record; rotate boundary; two processes appending; disk full | `audit verify` detects; concurrent appends valid; **effect-bearing actions denied** when pre-audit fails (documented DoS trade-off) | T1.2 |
| **S-08** | **Provider failover abuse** | Attacker blocks the network to force local; cloud fails mid-task with `cloud_ok=0` memory in context; Local→Gemini handoff with poisoned "progress" text | Authorisation identical; no `cloud_ok=0` in any cloud request body; handoff block is engine-built, framed as data; **no signature fabricated** | T2.5/3.6 |
| **S-09** | **Offline-mode abuse** | Offline: try to skip audit/taint/protected roots; spoof reachability probe; local model injected | All controls identical offline; monitor probes only the provider host and can only push traffic to *local* (safer) | T2.4/2.5 |
| **S-10** | **Malformed tool arguments** | Wrong types, extra keys, NUL bytes, >MAX_PATH, 10 MB strings, deep JSON, Unicode confusables | Rejected before authorisation; no crash; bounded time/memory | T1.3 |
| **S-11** | **Timeout abuse** | Hanging tool; repeated hangs to exhaust workers; slow provider; approved step that hangs | Returns failure at `timeout_s`; worker cap + circuit-break; agent never blocks indefinitely | T1.7/2.5 |
| **S-12** | **Kill-switch races** | Kill during `evaluate`, before `execute`, during a slow tool, during an LLM call, during approve, during a memory write; kill+`clear-stop` race; agent plants a `STOP` file | Tool completes and is committed (cooperative, [V]) or is prevented if not started; router aborts HTTP; memory write atomic; STOP planting denied by protected roots; `killed` audited | T1.7/2.5/3.2 |
| **S-13** | **Gateway abuse** | Idle/half-open/slow-loris, oversized, replay, stale, bad HMAC, unknown device flood, ungranted capability, HIGH via `launch_app` args | Legit client unaffected; V1 error codes unchanged; limiter bounded; events audited (sampled) | T0.5/1.7 |
| **S-14** | **Approval integrity** | Voice tries to approve ("yes" as a goal); mutate pending; approve terminal task; device requests HIGH | No approval path except CLI `approve/deny`; terminal tasks execute nothing | T1.7 |
| **S-15** | **Telemetry/audit privacy** | Canary secrets, transcripts, paths and memory text pushed through every path | Absent from perf, audit, logs, health | T0.6/1.2 |
| **S-16** | **Protected-root escapes** | 14-case matrix + UNC/junction targets that do not yet exist | 100 % denied; search/list reveal nothing | T1.1 |
| **S-17** | **Secret-detector evasion** | Obfuscated keys, split tokens, base64 | May pass the gate ⇒ still `proposed` (voice/agent) and owner-reviewed; **documented false negatives** | T1.8/3.3 |
| **S-18** | **DB copy / key loss** | Copy `memory.sqlite`; open without/with wrong key; bit-flip; swap rows | No plaintext in file; `InvalidTag`; memory disabled with message | T3.2 |

---

## 19. Owner Decisions Required

Numbering follows your list. **No decision blocks starting V2.0.** Rows show when an answer is first needed.

| # | Decision | Options → consequence | Recommendation | Needed by |
|---|---|---|---|---|
| **D1** | Hotfix D-01/D-02/D-03 (+ D-04b via T1.1, D-09 limiter/PIN) on `main`, or V2-only? | **(a) Cherry-pick onto `main`** → your live runtime and gateway stop being deaf/DoS-able/plantable weeks sooner, but `main` changes (needs your explicit authorisation, and a live-runtime restart). **(b) V2-only** → live V1 stays affected until V2 merges. | **(a)** for T0.3, T0.4, T0.5, T1.1 (small, independent, in-place). Plan works either way. | Any time; does **not** block |
| **D2** | Keep broad `allowed_roots` with engine-enforced protected roots, or narrow roots? | **Broad + engine deny-list** → keeps current usefulness, closes D-04/D-04b. **Narrow** → smaller blast radius but you lose breadth. | Broad + un-overridable deny-list (T1.1 is always on); you may narrow `local_config.yaml` yourself at any time. | Does not block |
| **D3** | Memory: explicit-only, or explicit + proposals-for-review? | Explicit-only → safest, no agent-initiated memory. **+Proposals** → useful, all agent/voice writes need CLI review. | **+Proposals**; voice-originated ⇒ `proposed`; nothing auto-activates | Before T3.3 (default is already set) |
| **D4** | What may cross the cloud boundary? | **(i)** V1 behaviour + protected state kept out of tools + `sensitive` memory local-only *[default]*. **(ii)** also make `normal` memory local-only unless per-item opt-in → strongest privacy, weaker recall in cloud answers. **(iii)** no file contents to cloud → breaks most V1 tasks. | **(i)**, with (ii) as a config switch | **Before T3.3/T3.6 defaults**; not P0–P2 |
| **D5** | Local-model residency for 8 GB VRAM | **(a)** on-demand + preload-on-degrade *[default]*. **(b)** always resident → 5.6 GB used, **900 MiB free**, no room for any second GPU model. **(c)** smaller model resident → less VRAM, accuracy unmeasured. | **(a)**; finalise after T2.6 measurements (`num_ctx` matrix, cold/warm load) | Before T2.6 finalises; not blocking |
| **D6** | Fast-path commands for V2.0 | **open/launch/start `<app>`** only *[default]* (LOW, existing tools). +close app → HIGH ⇒ never fast path. time/status → no tool exists ⇒ V2.2. | Default set; enable only after the eval shows 0 wrong-app launches | Before enabling T2.8 |
| **D7** | PTT / barge-in | V2.0: fix the chord **in place** (keeps your configured `ctrl+space`, which then requires Ctrl). V2.1: default hotkey — `Ctrl+Space` would be removed system-wide from Cursor/VS Code under `RegisterHotKey`; `Ctrl+Alt+Space` is already taken; **`Ctrl+Shift+Space` is free** [V]. Barge-in needs AEC/headset ⇒ future. Voice never authorises. | Keep config as is for V2.0; choose the V2.1 hotkey later (you can set `voice.ptt_hotkey` yourself now) | V2.1 |
| **D8** | Camera acquisition/consent | **Recorded as V2.2; not decided.** | — | V2.2 |
| **D9** | Remote/off-LAN connectivity | **Recorded as future; not decided.** LAN-only. | — | Future |
| **D10** | Operational live-runtime actions | *Identified, not performed:* (1) restart `VOID_VoiceRuntime` (deaf since 18:43 on 09-20); (2) decide whether to `device forget` the stale "My Android Phone"; (3) clean **341 orphan `device_secret:*`** credentials (list = ids not in `devices.json`; verify against the 2 live ids); (4) optionally tag `v1-baseline`; (5) note that a manual `device serve` occupies port 8765 during dev | Your schedule; I can supply a dry-run-first cleanup script | Not blocking |
| **D11** | CI / Python / lockfile | Lock from the live venv's `pip freeze` (80 pkgs, read-only); one interpreter (venv 3.14.7); hermetic CI (runner Python 3.14 availability **[U]**). Tests can run with the existing interpreter (read-only); a **worktree-local venv** from the lock is optional and would be the only install. | Lock + CI now; approve the worktree venv when convenient | Not blocking |
| **D12** | Autonomy | **V2.0: none.** No agent-initiated/background tasks; the existing autostart runtime is unchanged. | Confirmed | — |
| **D13** | Confirmation UX | **V2.0:** CLI `approve/deny` + constant spoken notice. **V2.1:** on-screen tray prompt. **Never voice** for HIGH/privacy. | Confirmed | — |

---

## 20. Final Recommended Execution Order

**Precondition:** the owner authorises implementation to begin. Until then nothing is changed.

| # | Task | Gate |
|--:|---|:-:|
| 1 | **T0.1** Hermetic test isolation | |
| 2 | **T0.2** Failing-first regression tests (9 strict xfails) | |
| 3 | **T0.3** Fix D-01 | |
| 4 | **T0.4** Fix D-02 (in place) | |
| 5 | **T1.1** Engine-protected roots *(pulled forward: highest-severity fix)* | |
| 6 | **T0.5** D-03 + D-09 hygiene | |
| 7 | **T0.10** Spoken engine status | |
| 8 | **T0.9** Stale-task sweep | |
| 9 | **T0.6** Rotating log + perf sink | |
| 10 | **T0.7** `interaction_id` correlation | |
| 11 | **T0.8** Health, `perf report`, `doctor` | |
| 12 | **T0.11** Lock / CI / guards | **G0** |
| 13 | **T1.2** Audit log | |
| 14 | **T1.3** Manifest + validation | |
| 15 | **T1.6** Task-store schema/taint/retention | |
| 16 | **T1.4** Taint model | |
| 17 | **T1.5** URL policy | |
| 18 | **T1.8** Secret hygiene | |
| 19 | **T1.7** CapabilityEngine (flagged) | **G1** |
| 20 | **T2.1** Provider interface | |
| 21 | **T2.2** Gemini options | |
| 22 | **T2.3** Local provider | |
| 23 | **T2.4** ConnectivityMonitor | |
| 24 | **T2.5** ModelRouter | |
| 25 | **T2.6** Eval + measurements | |
| 26 | **T2.7** Degraded notices | |
| 27 | **T2.8** Fast path (flagged; gated on T2.6) | **G2** |
| 28 | **T3.1** Session context | |
| 29 | **T3.2** Store + crypto | |
| 30 | **T3.3** Write policy | |
| 31 | **T3.4** Retrieval | |
| 32 | **T3.5** CLI | |
| 33 | **T3.6** Agent/router integration | |
| 34 | **T3.7** Retention/deletion | **G3 = V2.0** |

**Per-task loop for the implementation agent:** INSPECT the touched code → write the failing test(s) → implement → run targeted tests → run the **full suite in isolation** (T0.1) → run the relevant §18 abuse tests → update docs/config comments → report evidence (exact commands and counts) → commit only when the owner has authorised commits. At each gate: stop, present evidence, wait for go/no-go.

**Two-failure rule (mandatory).** For every failure: capture the exact error → locate where it failed → classify (code / configuration / dependency / environment / permissions / tooling / integration / architecture / incorrect assumption) → record what was attempted and learned → fix the identified cause when evidence supports it → retest. If the same or a materially similar error recurs **twice by substantially the same method: STOP** — compare both failures, find the common cause/assumption, investigate the repository/docs/dependencies/environment/tools, generate **at least two materially different approaches**, compare them (compatibility, simplicity, reliability, maintainability, security, project conventions), and switch method. A third attempt with the same method only if new evidence or a real state change makes success plausible. Never retry-loop. *(Examples from this planning task: a global `time.sleep` monkeypatch made a test invalid; a redirected-stdio benchmark hung — both fixed by changing method, not repeating.)*

---

## 21. V2.0 Acceptance Criteria

### G0 — P0 complete (T0.1–T0.11)

| # | Criterion (testable) |
|--:|---|
| 1 | `collected ≥ 883` + new; **0 failures**; skips ≤ 5; xfail count = 0 for fixed items |
| 2 | A full run performs **0** real credential-store operations (spy) and writes nothing to the real `~/.void`; read-only `cmdkey /list` count of `device_secret` unchanged before/after |
| 3 | Strict-xfails for D-01, D-02, D-03, D-09, D-13 **pass**; D-04b/D-05/D-07 remain xfail until T1.1/T1.5/T2.5 |
| 4 | D-01: after a simulated failed restart the supervisor retries with capped backoff and recovers when the device returns (Nth-retry test). **Owner-attended physical check** on a dev-isolated run: toggling Windows microphone access ⇒ recovery ≤ 60 s |
| 5 | D-02: bare Space never starts capture; configured chord does; auto-repeat unchanged; ≈0 PTT sessions in 1 h of typing on a dev-isolated `null`-capture run |
| 6 | D-03: a legitimate TLS request completes while an idle connection is held ≥30 s, with 3 half-open ClientHellos, and with 150 idle connections (prototype achieved 5–27 ms); excess connections dropped; **all existing device tests unchanged** |
| 7 | D-09: limiter keys ≤ cap after 10 k unauthenticated probes; rejection logs ≤ 1/s per reason; PIN comparison constant-time |
| 8 | D-13: AWAITING/BLOCKED/FAILED/PAUSED speak constant phrases only; TTS-off honoured |
| 9 | One `interaction_id` chain per interaction with monotone timestamps; perf/health/log contain **no content** (canary test); `perf report` reproduces the §2.3 stage tables of the spec on the 2026-09-17→20 log within rounding |
| 10 | `doctor` reports a stale heartbeat and a silent mic (the D-01 state) and gateway status, read-only |
| 11 | Log rotates at the configured size; `requirements.lock` present; CI green; count/skip guard active; stale sweep verified on a synthetic DB |

### G1 — Capability + security foundation (T1.x)

| # | Criterion |
|--:|---|
| 1 | Whole baseline passes with the engine **on and off**; **differential test**: identical `plan`/`messages`/status for scripted scenarios |
| 2 | Protected-root matrix **100 % denied** under `allowed_roots=[C:\]`-equivalent; search/list reveal no protected entry; D-04 and D-04b xfails pass; junction tests replace the 3 symlink skips |
| 3 | All 13 tools annotated; unknown/malformed args rejected pre-authorisation; a hung tool returns failure within `timeout_s + 0.5 s`; worker cap enforced |
| 4 | Taint property test ≥20 k seeded cases, **0 violations**; S-01, S-03, S-05, S-06 pass; D-05 passes; URL table ≥25 cases |
| 5 | Audit: 10 k records verify in <1 s; any single-byte edit/deletion/truncation/reorder detected; concurrent two-process appends valid; fail-closed proven; content-free |
| 6 | Task store: additive migration on synthetic V1-shape DB; V1 code still opens it; retention dry-run correct; backup created and integrity-checked |
| 7 | S-04, S-10, S-11, S-12, S-14, S-16 pass; `set-key` refuses reserved names; gateway routes through the engine with the allow-list unchanged (S-13) |

### G2 — Router + offline (T2.x)

| # | Criterion |
|--:|---|
| 1 | Fault injection (timeout, 5xx, 429, offline, flapping, exhaustion, kill mid-call): each ends in a defined state (local success or `PAUSED`); **no sleep after the final attempt**; no unhandled exception; D-07 xfail passes |
| 2 | Under an injected cloud stall a single LLM turn completes or fails over within `deadline + local latency + 1 s` (placeholder ≈12 s **[E]**, final values from T2.6) |
| 3 | Local provider sends `think:false` and `num_ctx`; oversize tool output never truncates the system prompt; model-missing ⇒ unavailable |
| 4 | S-08 and S-09 pass; handoff tests pass (Local→Gemini rebuild; **no fabricated signature**) |
| 5 | Eval report exists (accuracy + latency percentiles per provider/config); **default-model/thinking/deadline changes only after thresholds are agreed with the owner** |
| 6 | **Owner-attended** network-cut run: an offline command and a cloud-blocked mid-run task complete or pause as specified |
| 7 | Fast path (if enabled): measured over ≥30 runs; **0 wrong-app launches** in the eval; every fast-path action is an engine call in the audit log; static test shows no `RiskGate` import in the fast-path module; flag default off until this criterion is met |

### G3 — V2.0 acceptance

| # | Criterion |
|--:|---|
| 1 | Crypto: round-trip, wrong key, wrong AAD, bit-flip, row-swap pass; canary text absent from `memory.sqlite`; missing key ⇒ disabled, never plaintext |
| 2 | S-02, S-17, S-18 pass; "memory cannot authorise" and "system prompt byte-identical with/without memory" tests pass; no LLM-reachable path creates `active` |
| 3 | Retrieval: ≤5 items and ≤≈400 tokens always; deterministic; p95 < 20 ms at 10⁴ items **[E target — report measured value]** |
| 4 | Deletion: after `forget`, canary absent from the DB file, index empty, `VACUUM` ran, audit present; CLI states the non-guarantees |
| 5 | Scenarios: **memory** (CLI `remember` → new session recalls with provenance → `forget` verified); **follow-up** works within TTL; voice-originated write lands `proposed` |
| 6 | **Overall:** all tests pass; baseline count preserved and skips not increased; no secrets in repo/logs/audit/perf; docs corrected (README, architecture review, Android README); **`main` untouched until the owner merges; `017e0f6` recoverable**; no new dependency added |

---

## 22. Risks and Unknowns

### 22.1 Risks

| # | Risk | L | I | Mitigation |
|--:|---|:-:|:-:|---|
| R1 | Refactoring `_run_call`/`_commit_step` regresses security semantics | M | H | Flagged engine; dual-mode suite; differential test; `evaluate()` shared by both sites |
| R2 | Taint/deferral friction (CLI-only approvals) annoys the owner | M | M | Narrow taint sources; spoken notice; escalation telemetry; tray prompt V2.1 |
| R3 | Local model accuracy inadequate as fallback | M | M | Eval gate; fallback-only; honest degraded messaging |
| R4 | Cross-provider history incompatibility | H | M | No mid-task replay into Gemini; engine-built progress block; pause otherwise |
| R5 | VRAM contention (900 MiB free with LLM resident) | M | M | On-demand residency; `num_ctx` matrix; CPU STT |
| R6 | Dev collides with the live runtime (mic, 8765, venv, `~/.void`) | H | H | Dev-isolation protocol; isolation fixture |
| R7 | Conclusions from tiny samples | H | M | Telemetry first; thresholds only after datasets |
| R8 | Tests weakened/deleted to make changes pass | M | H | Count/skip guards; owner sign-off |
| R9 | Scope creep into V2.1/V2.2 | H | H | 🟡/🔴 lists; gates |
| R10 | Protected-root bypass via Windows path semantics | M | H | Two-layer check (prototype-validated); fail-closed; matrix |
| R11 | Audit fail-closed becomes a DoS lever (fill disk ⇒ deny effects) | L | M | Documented; read-only tools continue; `doctor` reports disk/audit health; configurable |
| R12 | Client-reuse retains API key in memory longer | L | L | Flag off by default; measure benefit first |
| R13 | Memory key loss / handling errors | L | M | Fail-closed; backup-before-migrate; never regenerate over ciphertext |
| R14 | Live STT slower than isolated invalidates STT targets | M | M | T0.7/T0.8 instrumentation before any STT commitment (V2.1) |
| R15 | Single-developer bandwidth | H | M | P0/P1 independently valuable; strict gates |
| R16 | Worker-thread timeouts abandon a stuck tool thread | L | L | Worker cap; circuit-break; tools are short subprocess/win32 calls, no COM affinity [C] |

### 22.2 Evidence labels for the plan's key statements

| Tag | Statements |
|---|---|
| **[V] verified by execution** | D-01, D-03 (+ fix prototype), D-04/D-04b, D-05, D-07, D-09 (limiter/log), D-13, D-14; kill semantics; 878/5 tests ×4; AES-GCM; path-matrix results (resolve+prefix bypassed by 3 cases, +identity denied all); RegisterHotKey probes; 343/2 credentials; state-dir override; CUDA failure; runtime still deaf |
| **[C] code-derived** | Integration points; two risk-evaluation sites; gateway built from `Assistant`; single provider call site; single broker restart site; no COM in tools; audit multi-process need; no CI/lock; Android secret storage; PIN compare; key plaintext |
| **[M] measured** | Local LLM latency/VRAM (5.6 GB, 900 MiB free, 4.4 s warm/83 s cold); STT CPU 1.19/0.95/0.81 s (synthetic audio); live voice latencies (n=6); log counts |
| **[E] estimate/hypothesis** | Wake ≈2 cores; cloud thinking effect on the 35.9 s tail; router deadlines; memory index p95 <20 ms; Qwen3 KV-cache growth with `num_ctx`; Gemini signature model-boundness |
| **[U] unknown** | Live-vs-isolated STT gap; wake false-accept rate and wake→listening latency; real command distribution; client-reuse network benefit; local accuracy on the golden set; CI runner Python 3.14; BitLocker and `~/.void` ACLs; which firewall rule admits the phone; SAC history; the first STT-bench hang (tooling) |

### 22.3 Not done (by instruction) and not validated

No production code, dependency, database, config, scheduled task, pairing, firewall, permission, `main`, live runtime or commit was touched. **Not validated:** anything requiring a physical device (Android, camera, mic-permission toggle), real cloud calls (Gemini quota), the owner's real database migration, CI on GitHub.

---

## Readiness Assessment

**READY FOR V2.0 IMPLEMENTATION**

Basis (each supported by inspection above): architecture traced with every V2.0 integration point located in code; two risk-evaluation sites and the gateway as a second authority path identified and absorbed into the design; critical defects reproduced or root-caused and, for D-03 and protected roots, **fix designs validated by prototype**; corrected spec claims recorded; baseline green (878 passed / 5 skipped, four runs); no V2.0 task depends on an excluded feature or a new dependency; no owner decision blocks the first task.

**Conditions (not technical unknowns):** (1) your explicit authorisation to begin implementation (and, separately, whether commits are allowed); (2) awareness that D10 operational items remain yours; (3) any objection to the defaults in §19 — absent one, they stand.

**First task on approval:** T0.1 + T0.2 — isolation fixture and nine failing-first tests, **tests only, no production change**.
