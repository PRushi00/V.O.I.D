# V.O.I.D V2 — G0 Gate Report

*Prepared 2026-09-21 (evidence gathered 15:00–17:15 IST). Documentation-only task: no source, test, guard, configuration or
production-state change was made to produce this report.*

**Evidence labels used throughout**

| Label | Meaning |
|---|---|
| **CURRENT** | Reproduced while preparing this report (commands and results are quoted). |
| **PRIOR** | Established in an earlier session of this engagement on the same machine and repository lineage; **not** re-run here. |
| **AUTOMATED** | Covered by a named test that passed in the CURRENT full-suite runs. |

**Status vocabulary:** PASS · PARTIAL · NOT VALIDATED · BLOCKED · NOT APPLICABLE.
Synthetic (WAV / synthesised-speech) validation is never reported as physical-microphone validation.

---

## 1. Gate Purpose

`docs/V2_0_IMPLEMENTATION_PLAN.md` §20–§21 defines **G0 — P0 complete (T0.1–T0.11)** as the owner-review gate between the
"Stabilise + Instrument" phase and P1 (Capability + Security Foundation): *"At each gate: stop, present evidence, wait for
go/no-go."* This report presents that evidence against the plan's own 11 acceptance criteria. It does **not** grant the gate.
No owner approval record for G0 exists in the repository or `docs/` (searched).

## 2. Repository State (CURRENT)

| Item | Value |
|---|---|
| Canonical repository | `C:\V.O.I.D` |
| Branch | `main` |
| HEAD | `1b1af96` (`1b1af9637213234bc5b5041cfc69aa4726960477`, 2026-09-21 14:42 +05:30) |
| V1 baseline | `017e0f6cc180c889591adb22824671ec8f0cdf14` |
| Commits since baseline | 22 |
| Worktrees | one: `C:/V.O.I.D 1b1af96 [main]` (the old V2 worktree was removed on the owner's instruction and not recreated) |
| Pushed | **No.** `origin/main` is still `017e0f6`. |
| Working tree | clean; untracked only: `docs/`, `wakeword-training/`, `workspace/You originally described V2 around.md` (owner files, untouched) |
| Runtime environment | Windows 11, CPython 3.14.7, `.venv` (the live V1 venv was used read-only for test runs) |

Commit lineage (all on `main`): P0 `b7b0af3` T0.1 → `8694a23` T0.2 → `709fbc4` T0.3 → `4bd1d58` T0.4 → `c3b1c82` T0.5 →
`b5ddb37` T0.6 → `c6c7c14` T0.7 → `a52bf65` T0.8 → `ec445d9` T0.9 → `a5fb47f` T0.10 → `04de789` T0.11 →
`a7c015a`, `ebf4853` (G0 follow-ups); P3 memory `1c310a9`, `111e80d`, `a0dfd64`, `15b6b2c`, `b9449ce`, `4769991`;
`51cb070` pytest scoping; `d3153b8` forget-fix; `1b1af96` minimum-speech guard.

## 3. Scope Reviewed

`docs/V2_TECHNICAL_SPEC.md`, `docs/V2_0_IMPLEMENTATION_PLAN.md` (§8, §11, §20, §21), `docs/V2_0_IMPLEMENTATION_READINESS_REPORT.md`,
`docs/v2-evidence/` (scripts), `git log 017e0f6..HEAD`, the diff of every pre-existing test file and every security-relevant
source file against the baseline, the P0 test files, the collection guard, three full-suite runs (CURRENT), a credential-store
trap run (CURRENT), and the real `~/.void/void.log` / `doctor` output (read-only).

**Not in scope of this gate:** P1/P2/P3 acceptance (G1–G3). Persistent memory (P3) is *ahead* of the plan's G0→G1 ordering; its
evidence is summarised in §8 for completeness and is not a G0 criterion.

## 4. G0 Acceptance Criteria

Criteria are quoted from plan §21 "G0" (abridged where marked …).

| # | Criterion (plan §21) | Evidence | Status | Notes |
|--:|---|---|---|---|
| 1 | `collected ≥ 883` + new; **0 failures**; skips ≤ 5; xfail count = 0 for fixed items | CURRENT: `pytest -q -p no:cacheprovider` → **1468 collected, 1460 passed, 0 failed, 5 skipped, 3 xfailed** (95 s); run 3×, identical. `scripts/check_test_guard.py` → `run: {tests:1468, failed:0, skipped:5, xfailed:3}` / `guard ok`. All **883** baseline test IDs still collected. The 3 xfails are exactly **D-04b, D-05, D-07**. | **PASS** | Skips: 3× symlink creation not permitted (`test_protected_roots.py`; G1 #2 replaces them with junction tests), 2× optional-dependency-present. Guard floor unchanged by this task (`min_collected 1468`, md5 `636396a0…`). |
| 2 | A full run performs **0** real credential-store operations (spy) and writes nothing to the real `~/.void`; read-only `cmdkey /list` count of `device_secret` unchanged | CURRENT: a scratch plugin (outside the repo) replaced the real `WinVaultKeyring` get/set/delete with a **counting trap that raises** → full suite (1460 passed) recorded **`real_vault_calls: 0`**. `cmdkey /list` `device_secret` count **0 → 0**. Credential names present (`gemini_api_key`, `memory_key`) unchanged. Full run #1 (15:09–15:10): md5 of `devices.json`, `device_cert.pem`, `device_key.pem`, `gateway_address.json`, `tasks.sqlite`, `memory.sqlite`, `health.json`, `perf.jsonl` **identical** before/after; `void.log` size and line count unchanged. AUTOMATED: `tests/test_isolation.py` (in-memory keyring, sandboxed HOME/state, `real_keyring` opt-in only). | **PASS** | On run #2 (15:15–15:17) `health.json`, `tasks.sqlite`, `perf.jsonl`, `void.log` changed **because a real V.O.I.D runtime started at 15:15:01** (see §9); the timestamps coincide with that runtime's own log lines (e.g. `tasks.sqlite` 15:16:15.827 = its `LLM_CALL_DONE` 15:16:15.826). `memory.sqlite` and all device/credential files were unchanged. The trap used is stricter than the plan's spy (it refuses, not just records). |
| 3 | Strict-xfails for D-01, D-02, D-03, D-09, D-13 **pass**; D-04b/D-05/D-07 remain xfail until T1.1/T1.5/T2.5 | CURRENT/AUTOMATED: `tests/test_regression_v1_defects.py` — `test_d01_supervisor_keeps_retrying_after_a_failed_restart`, `test_d01_mic_eventually_recovers_when_device_returns`, `test_d02_bare_space_does_not_start_ptt`, `test_d03_idle_tcp_connection_does_not_block_legitimate_clients`, `test_d09_limiter_state_is_bounded_under_unauthenticated_load`, `test_d09_rejection_logging_is_sampled`, `test_d13_non_completed_outcomes_speak_a_constant_phrase[…]` all **pass** (markers removed); each has a passing harness-sanity companion. Remaining `XFAIL(strict)`: `test_d04b_…`, `test_d05_…`, `test_d07_…` (reasons quoted in the run output). | **PASS** | The tests were verified failing-first for the stated reasons at T0.2 (PRIOR). |
| 4 | D-01: after a simulated failed restart the supervisor retries with capped backoff and recovers when the device returns (Nth-retry test). **Owner-attended physical check** on a dev-isolated run: toggling Windows microphone access ⇒ recovery ≤ 60 s | AUTOMATED: `tests/test_voice_mic_recovery.py` — first failure observable, second/Nth failures keep retrying, backoff capped (never a busy loop), no interruption of an active capture, shutdown ends recovery, heartbeat state. PRIOR (synthetic fault injection through the real launcher, 13:52): silence detected after 8.0 s → attempts 1 and 2 failed → backoff 4 s, 8 s → attempt 3 succeeded → idle. | **PARTIAL** | Software half **PASS**. The **physical, owner-attended** half (toggle microphone access, recovery ≤ 60 s) is **NOT VALIDATED**. It was hardware-blocked until ~15:13 (see §9) and is now unblocked, but requires the owner to attend. |
| 5 | D-02: bare Space never starts capture; configured chord does; auto-repeat unchanged; ≈0 PTT sessions in 1 h of typing on a dev-isolated `null`-capture run | AUTOMATED: `tests/test_ptt_chord.py` (bare Space, Ctrl+Space, Ctrl released first, auto-repeat, modifier-free hotkey, aliases, fail-closed on unreadable modifier state, `strict_chord:false` restores V1). `test_an_hour_of_typing_starts_no_sessions_and_every_chord_starts_exactly_one`: seeded simulation, 18 000 key events ≈ 1 h at 5 keys/s. | **PASS** (simulated) | The "1 h" is a seeded in-test simulation, not a wall-clock hour of real typing. **Physical Ctrl+Space was never exercised** (would inject global keystrokes) → NOT VALIDATED physically (§12). |
| 6 | D-03: a legitimate TLS request completes while an idle connection is held ≥30 s, with 3 half-open ClientHellos, and with 150 idle connections (prototype 5–27 ms); excess dropped; **all existing device tests unchanged** | CURRENT, exact parameters, real `DeviceGateway` on loopback, temp state dir, production-like limits (`connection_timeout_s=10`, `max_connections=32`, per-IP 4): **A** 1 idle connection held (re-established when closed) 35 s → 8 legit requests, all served (HTTP 401 = handshake + application reply), latency 3/5/19 ms (min/median/max). **B** 3 half-open ClientHellos 35 s → 7 requests served, 3/4/17 ms. **C** 150 idle connections from another source → 150 accepted by the OS, **146 dropped** (`connections_dropped_per_ip`), 4 requests served, 2/3/30 ms. AUTOMATED: `tests/test_gateway_availability.py` (idle, half-open, slow-loris, flood cap, slot recovery, TLS still mandatory) plus PRIOR sandbox run (3 idle sockets closed by server after >10 s; handshake 34 ms). | **PASS** | An earlier version of my own scenario combined attackers on one source and exhausted the per-IP cap (4) so the legit connection was dropped — a scenario flaw, not a gateway fault; the plan lists the three cases separately and they pass separately. **No existing device test file was modified** (`git diff --name-status 017e0f6 HEAD -- tests`: only `conftest.py`, `test_voice.py`, `test_voice_mic_recovery.py`, `test_voice_wake_integration.py` are modified). |
| 7 | D-09: limiter keys ≤ cap after 10 k unauthenticated probes; rejection logs ≤ 1/s per reason; PIN comparison constant-time | CURRENT (10 000 distinct unauthenticated bodies, 250 source addresses, 0.2 s): request-limiter keys = **1024 (= configured cap)**; gateway log records = **1 total, max 1 per second window**. AUTOMATED: `test_rate_limiter_key_set_is_bounded_and_lru_evicted`, `test_log_sampler_admits_first_suppresses_burst_then_reports_the_count`, `test_stop_pin_is_compared_in_constant_time`, `test_stop_pin_semantics_unchanged_including_non_ascii_and_missing_pin`. Source: `KillSwitch` PIN check now `hmac.compare_digest`. | **PASS** | LRU eviction only *resets* an evicted key's counter; it never blocks a caller (documented in `auth.py`). |
| 8 | D-13: AWAITING/BLOCKED/FAILED/PAUSED speak constant phrases only; TTS-off honoured | AUTOMATED: `tests/test_engine_status.py` — each attention status speaks exactly its constant phrase; untrusted text never spoken on the status path; spoken text is a function of status alone (property test); TTS-off honoured for status phrases; voice cannot approve the task it announced; `respond` telemetry records kind without text. PRIOR (real launcher, synthetic audio, 14:08): a `failed` task (three Gemini `ServerError`s) spoke only the 60-character constant phrase, not the error text. | **PASS** | An `awaiting_confirmation` outcome was not exercised by voice on the live runtime (unit-tested only). |
| 9 | One `interaction_id` chain per interaction with monotone timestamps; perf/health/log contain **no content** (canary test); `perf report` reproduces the §2.3 stage tables of the spec on the 2026-09-17→20 log within rounding | AUTOMATED: `tests/test_interaction_id.py` (one ordered content-free chain per voice command, distinct ids per command, failing provider recorded without error text, inert when unconfigured); `tests/test_perf.py` (schema allow-list, unknown/mistyped values dropped, seeded fuzz never lets content reach the file); `tests/test_health_doctor.py` (heartbeat only counts/flags/timings). CURRENT: `perf report --legacy always` on a copy of the 13 300 lines dated 2026-09-17→20 → **reproduces exactly**: 35 wake activations (16 `no_speech`, 19 `silence`); 86 STT runs with audio; LLM n=11 p50 **3.54 s** max **35.92 s**; text-only n=5 p50 **4.52 s**; tool-call n=6 p50 **2.92 s** max 3.80 s; transcribed STT decode n=6 p50 **2.56 s** max **7.83 s**; `launch_app` 0.06 s, `list_windows` 0.04 s; 1 `ServerError`. | **PARTIAL** | **Not reproduced:** the spec's *composite interaction* rows (wake→endpoint, agent total, endpoint→first spoken word, speech duration) — the legacy log has no `interaction_id` so `perf report` cannot assemble them (it reports `interactions (with id): 0`); and the spec's **p90** column (the report emits p50/p95/p99). Stage rows match. |
| 10 | `doctor` reports a stale heartbeat and a silent mic (the D-01 state) and gateway status, read-only | AUTOMATED: `test_doctor_flags_the_d01_state`, `…_a_deaf_but_running_stream`, `…_a_stale_heartbeat_as_hung_or_dead`, `…_owner_shutdown_is_not_a_failure`, `…_gateway_probe_detects_a_dead_and_a_live_port`, `…_is_read_only_on_a_populated_state_dir`, `test_cli_doctor_exit_codes_and_read_only`. CURRENT (read-only, against the real state): `doctor` reported `FAIL runtime heartbeat STALE: last heartbeat 2153s ago` while no runtime was running, then `ok … fresh (1s ago, pid 49840, state idle)` once the real runtime started. | **PASS** | Observed weakness (🟡): while the heartbeat was stale, `doctor` still printed `ok microphone … delivering audio frames` and `ok wake word … armed`, derived from that same stale file. The FAIL line was correct; the OK lines were not independent evidence. |
| 11 | Log rotates at the configured size; `requirements.lock` present; CI green; count/skip guard active; stale sweep verified on a synthetic DB | AUTOMATED: `tests/test_log_rotation.py` (single/two-writer copy-truncate rotation, bounded backups, config limits, clamping); `tests/test_task_sweep.py` (only stale `running` → `paused`, never resumed/completed, dry-run changes nothing, idempotent, boundary, concurrency-safe, corrupt row neither swept nor hidden). `requirements.lock` present (80 pins from a read-only `pip freeze`, commit `04de789`). `scripts/check_test_guard.py` + `tests/guard_limits.json` + `tests/baseline_v1_test_ids.txt` (883 IDs) active, self-tested by `tests/test_guard.py`, and wired into `.github/workflows/tests.yml`. | **PARTIAL** | **CI green is NOT VALIDATED**: the workflow is authored (Windows runner, Python 3.14, install lock, run suite, run guard) but has **never executed** — nothing has been pushed; the file itself is marked `UNVALIDATED`. The lock has also not been re-installed into a fresh venv (plan §T0.11: only after owner approval). |

**Tally:** PASS 1, 2, 3, 5 (simulated), 6, 7, 8, 10 · PARTIAL 4, 9, 11 · FAIL none.

## 5. P0 Evidence

All eleven P0 tasks are implemented, committed, and covered by named tests (commit → test file):

| Task | Commit | Tests (AUTOMATED, passing) |
|---|---|---|
| T0.1 hermetic isolation | `b7b0af3` | `test_isolation.py` |
| T0.2 failing-first regressions | `8694a23` | `test_regression_v1_defects.py` (D-01/02/03/04b/05/07/09/13 + sanity companions) |
| T0.3 D-01 mic supervisor | `709fbc4` | `test_voice_mic_recovery.py` |
| T0.4 D-02 PTT chord | `4bd1d58` | `test_ptt_chord.py` |
| T0.5 D-03/D-09 gateway | `c3b1c82` | `test_gateway_availability.py` |
| T0.6 rotating log + perf stream | `b5ddb37` | `test_log_rotation.py`, `test_perf.py` |
| T0.7 interaction_id | `c6c7c14` | `test_interaction_id.py` |
| T0.8 health / perf report / doctor | `a52bf65` | `test_health_doctor.py` |
| T0.9 stale-task sweep | `ec445d9` | `test_task_sweep.py` |
| T0.10 spoken engine status | `a5fb47f` | `test_engine_status.py` |
| T0.11 reproducibility | `04de789` | `test_guard.py`; `requirements.lock`; CI workflow (unrun) |

## 6. Regression Evidence (CURRENT)

| Run | Command | Result |
|---|---|---|
| Full suite #1 | `python -m pytest --junitxml=… -q -p no:cacheprovider -rxs` | 1468 collected · **1460 passed · 0 failed · 5 skipped · 3 xfailed** · 95.5 s |
| Guard | `python scripts/check_test_guard.py --junit …` | `run: {'tests': 1468, 'failed': 0, 'skipped': 5, 'xfailed': 3}` · `guard ok: no baseline test lost, skip/xfail ceilings respected` |
| Full suite #2 (credential trap) | same, with `-p g0_vault_trap` | 1460 passed · 5 skipped · 3 xfailed · **`real_vault_calls: 0`** |
| Sub-suites (collected) | memory 364 (`test_memory_*.py`), audio guard 37 | included above |

The collection count differs from the 1431 recorded at the previous commit because commit `1b1af96` added 37 tests
(`tests/test_audio_guard.py`); the floor was raised in that same commit to 1468. It was **not** altered for this report.

## 7. V1 Preservation

| Check | Result |
|---|---|
| Baseline test IDs preserved | **PASS (CURRENT)** — all 883 IDs in `tests/baseline_v1_test_ids.txt` are collected; the guard fails if any is lost or renamed. |
| Baseline test files changed | 4 of the pre-existing files were modified; every removed line reviewed: `conftest.py` (T0.1 isolation; one docstring line replaced), `test_voice_mic_recovery.py` (107 lines added, none removed), `test_voice_wake_integration.py` (test rig now builds the session with `min_speech_ms=0` because its synthetic 180–240 ms "commands" are shorter than the new guard's minimum; the guard has its own 37 tests), `test_voice.py` (**one V1 assertion was deliberately changed**: `test_high_risk_is_opaque_and_voice_cannot_approve` previously encoded the D-13 defect — silent return to idle — and now asserts the constant status phrase is spoken; it still asserts voice never approves and only calls `Assistant.run`). No test was deleted. |
| `RiskGate` (`void/security/risk.py`) | **UNCHANGED** since baseline. |
| Root/file boundary (`void/roots.py`, `void/actions/files.py`), `registry.py`, `base.py`, `void/security/credentials.py`, `void/device/cert.py`, `identity.py`, `pairing.py`, `protocol.py` | **UNCHANGED**. |
| `KillSwitch` (`void/core/kill_switch.py`) | Present; +6/−1: PIN check changed from `==` to `hmac.compare_digest` (D-09). Semantics tests pass (non-ASCII, missing PIN). |
| Gateway (`void/device/gateway.py`, `auth.py`) | Present; TLS mandatory (min TLS 1.2, cert load unchanged), HMAC/stale/replayed/unknown-device rejection paths and allow-list retained; changes are per-connection handshake with timeout, bounded handler pool, bounded LRU limiter, sampled rejection logging. `test_plaintext_http_is_still_refused_tls_is_mandatory` and all baseline device tests pass. |
| `void/security/secrets.py` | +1 line: reserved key name `MEMORY_KEY`. |
| Agent / app | `agent.py` +76/−3 and `app.py` +110/−6 (memory integration, stale sweep). The removed lines are refactor call-sites (`self.store` wrapper, `specs` with `recall_only`, `_agent()` → `_agent(memory_first)`, `tools.execute` indented under a guard); `RiskGate`, `KillSwitch` and confirmation call sites remain. |
| Voice, provider infrastructure | Present and exercised by the baseline suite. |

**Limit of this evidence:** it establishes that baseline *tests* and security *source files* are preserved. It does not establish
runtime behavioural equivalence beyond what those tests cover; there is no differential engine-on/off test (that is G1 #1).

## 8. Persistent Memory Evidence

Memory (P3) is outside the plan's G0 criteria; summarised here because it precedes G1 in the actual history.

| Property | Evidence | Label |
|---|---|---|
| Automated coverage | 364 tests across `test_memory_store/policy/retrieval/integration/recall_routing/persistence_privacy/e2e_validation`; all pass | **AUTOMATED / CURRENT** |
| Remember, recall (memory-first, no tools), correction, forget, restart persistence | Real CLI path, real `~/.void/memory.sqlite`, real Credential-Manager key, real Gemini; each step a fresh OS process | **PRIOR** (2026-09-21 ~09:50–14:12) |
| Forget also removes a pending copy of the fact | Defect found live → fixed in `d3153b8` with 4 tests; re-run live ("Forgot 3") | **PRIOR + AUTOMATED** |
| Privacy | Raw-byte scans of `memory.sqlite`, `void.log`, `perf.jsonl`, config: no memory text or transcripts; persisted task answers redacted; only owner-typed goals remain in `tasks.sqlite` (V1 behaviour) | **PRIOR** |
| Memory is context, not authorization | Sandboxed run with an obedient provider and three adversarial memories: `delete_file` (file and directory), `write_file` overwrite, `close_app explorer.exe`, delete outside the test folder all stopped at `awaiting_confirmation`, nothing executed; tainted `propose_memory` landed `quarantined`; kill switch paused before any model call | **PRIOR** |
| Voice + memory | Real launcher, real Gen3 wake / Whisper / SAPI, **synthetic** microphone: voice "remember" → `proposed`; owner accept → voice recall "Your … test project is Nova" (3 clean runs); voice cannot forget; recall after forget → "don't have that stored" | **PRIOR (synthetic audio)** |
| Not re-run for this report | Destructive/production-state memory tests (per instruction) | — |

## 9. Voice / Runtime Evidence

**Microphone timeline (facts):**
- Until ~15:13 the physical microphone could not be opened by any application on the machine (PortAudio −9999 / −9996, `winmm` "in use"; last successful capture 08:17:36). Classified as an **environment blocker**; nothing was changed to alter it (PRIOR, plus a CURRENT baseline probe at ~15:04 that still failed).
- `audiodg` (the Windows audio engine) restarted at **15:13:06**; the machine was not rebooted (boot 03:29). I did not cause this.
- **15:15:01 (CURRENT observation):** the scheduled task started the real runtime. Log: `VOICE_RUNTIME_STARTING` → `MICROPHONE_OPENED device='Microphone Array (3- Realtek(R)' index=1 samplerate=16000` → `VOICE_RUNTIME_READY wake_configured=True` → `WAKE_ARMED` (15:15:05). No harness marker in that runtime's log. `doctor` (read-only): heartbeat fresh (pid 49840), microphone delivering frames, wake armed, 0 running tasks.
- **15:15:59–15:16:20 (CURRENT observation, one interaction):** wake trigger (score 0.515, threshold 0.34) → capture endpointed on silence (26 400 samples) → STT decoded a 10-character transcript → one LLM call (12.35 s, no tool calls) → 70-character reply spoken via SAPI → idle → wake re-armed. A second wake at 15:16:44 (score 0.393) ended `no_speech` (64 320 samples, empty transcript, 9 ms) and returned to idle.

| Item | Status | Basis |
|---|---|---|
| Synthetic/WAV runtime path: real launcher, mutex, Gen3 wake, capture, audio guard, Whisper STT, Assistant, Gemini, memory, SAPI TTS, return to idle | **PASS** | PRIOR (multiple runs 13:44–14:38) |
| Minimum-speech guard (`1b1af96`): 37 tests, mutation-checked (15 fail with the guard disabled); on the real launcher with real Whisper, three 100–200 ms slivers skipped (no STT, no LLM call) and a genuine 470 ms command dispatched | **PASS** | AUTOMATED (CURRENT) + PRIOR (14:37 run) |
| Real runtime starts on the **physical** microphone, opens it, arms wake, stays up, heartbeat current | **PASS (observed)** | CURRENT log/doctor evidence above. First observed ≈ 2 minutes after start; a later read-only `doctor` at 17:15 still showed the same process (pid 49840) alive with a 2-second-old heartbeat (≈ 2 h wall-clock, which may include machine sleep). **Not** a controlled stability test. |
| One complete physical-mic interaction (wake → capture → STT → agent → provider → TTS → idle) executed by the real runtime | **PASS (observed, n = 1)** | CURRENT log lines above. The privacy-safe log records lengths and timings only, so **who or what produced the audio (human speech vs. ambient sound) is not established**, and the transcript was deliberately not read. |
| Real **human** spoken memory lifecycle (remember → accept → recall → restart → recall → forget → restart → verify) | **NOT VALIDATED** | Never performed with a person speaking; requires the owner. Previously BLOCKED by the microphone; now unblocked. |
| Runtime restart/recovery on the physical microphone | **NOT VALIDATED** | Not exercised on hardware. |
| Physical Ctrl+Space | **NOT VALIDATED** | Would inject global keystrokes; unit/simulation-tested only. |
| D-01 physical toggle ≤ 60 s (criterion 4) | **NOT VALIDATED** | Owner-attended by definition. |
| Long-term physical runtime stability | **NOT VALIDATED** | Process alive ≈ 2 h at the last check (17:15); no soak, sleep/resume or recovery test was run. |

**None of the NOT VALIDATED items indicates a software failure**: each is a test that was hardware-blocked or owner-attended and has not yet been run. Plan assumption **A8** explicitly permits development without the live microphone.

## 10. Security Evidence

| Control | Status | Evidence / limit |
|---|---|---|
| `RiskGate`, confirmation semantics | Unchanged; enforced | Source unchanged since baseline; baseline suite passes; PRIOR sandbox run: every risky tool stopped at `awaiting_confirmation` with adversarial memory present. |
| Kill switch | Present; PIN compare constant-time | AUTOMATED; PRIOR sandbox run: engaged stop paused a run before any model call. |
| Memory as untrusted context; tainted proposals | Enforced | AUTOMATED (`test_memory_*`); PRIOR adversarial runs (§8); tainted model proposals land `quarantined`. |
| Voice cannot approve | Enforced | `test_voice_cannot_approve_the_task_it_just_announced`; constant status phrases only (D-13). |
| Gateway: TLS, HMAC, replay, allow-list | Retained; availability hardened | §7; §4 #6, #7. |
| Secret handling | Retained | Keys only in the OS store; memory key `memory_key` created in the production vault by the memory feature (PRIOR, expected); 0 real-vault calls during full suite (CURRENT); `secretscan` rejects secret-shaped memory. |
| Logging / telemetry | Privacy-safe | Schema allow-list, fuzz test, content-free chain; raw scans (PRIOR) found no memory text or transcripts in `void.log`/`perf.jsonl`. |
| Filesystem protections | **V1-level only** | `allowed_roots`/`protected_roots` are config-driven (`void/roots.py`, `files.py` unchanged). On the owner's machine `allowed_roots` includes `C:\`. There is **no engine-level protected-root exclusion** for `~/.void`, `local_config.yaml`, or credential stores. |
| Audit log (hash-chained) | **Does not exist** | P1 T1.2. Only `void.log` diagnostics exist. |
| CapabilityEngine, tool manifest/arg validation, taint model in RiskGate context, URL policy, task-store schema/retention, reserved-keyring-name guard | **Do not exist** | P1 T1.3–T1.8. Verified: no `CapabilityEngine` or audit module in `void/`. |
| D-04 / D-04b (agent-created `pairing_window.json` becomes a valid pairing window; file tools reach V.O.I.D state) | **OPEN — future P1 work** | Strict xfail `test_d04b_…` still xfails. Plan/readiness report: closed by **T1.1 Engine-protected roots**, "hotfix candidate". |
| D-05 (`open_path` data-carrying URL rated LOW) | **OPEN** — P1 T1.5 | strict xfail |
| D-07 (dead cloud retried, task fails, local never used) | **OPEN** — P2 T2.5 | strict xfail; observed live again (Gemini 503s ended a task `failed`) |

## 11. Performance Evidence

G0 defines no latency targets other than the gateway figure (5–27 ms prototype) and the ≤ 60 s physical recovery. Measured values that exist:

| Target / question | Measured value | Method | Status | Limitations |
|---|---|---|---|---|
| Gateway serves a legit client under attack (plan: 5–27 ms) | 2–30 ms (min/median/max 3/5/19, 3/4/17, 2/3/30 ms across A/B/C) | CURRENT loopback, real gateway code, `_legit` requests during the attack | **PASS** | Loopback, not the phone hotspot path. |
| D-09 limiter under 10 k probes | 1024 keys (cap); 1 log record; 0.2 s for 10 k requests | CURRENT, in-process | **PASS** | Bodies rejected before crypto; not a network flood. |
| D-01 physical recovery ≤ 60 s | — | not run | **NOT VALIDATED** | Owner-attended. |
| Legacy (V1) stage latencies for the §2.3 tables | LLM p50 3.54 s (max 35.92); STT transcribed p50 2.56 s (max 7.83); tools ≤ 0.07 s | CURRENT `perf report` on the 17→20 Sep log | **MEASURED**, matches spec | n = 11 / 6 / 7 — "too few samples" flagged by the report. |
| Real-runtime interaction (15:15:59, physical mic) | endpoint→first spoken word **14.56 s**; STT decode **1.20 s** for 1.65 s audio; agent total **13.27 s** of which the LLM call **12.35 s**; non-LLM agent time ≈ 0.9 s; TTS 4.7 s for 70 chars | CURRENT, log timestamps | **MEASURED, n = 1** | Single sample; model latency (Gemini) ≈ 85 % of the perceived wait; **V.O.I.D-side** overhead (STT + non-LLM agent + dispatch) ≈ 2 s. |
| Persistent-memory operations (real store, real vault key) | remember p50 3.1 ms · retrieve 0.25 ms · build_context 2.5 ms · forget+VACUUM 4.7 ms · first open+key fetch 10 ms | PRIOR (`memperf`, n = 20 each) | MEASURED (prior) | Small store (≤ 20 items). |
| Minimum-speech guard cost | 0.27 ms per call on a 3 s capture | CURRENT, `timeit`, n = 2000 | **MEASURED** | Numpy on this CPU. |
| STT decode / LLM / TTS from synthetic-audio runs | STT p50 1.19 s (n = 11); LLM p50 6.4 s (n = 8, Gemini incl. retries); TTS p50 5.6 s | PRIOR `perf report` | MEASURED (prior, synthetic audio) | Mixed with harness runs; not a benchmark. |
| Wake-to-STT and end-to-end on a **human** utterance | — | — | **NOT VALIDATED** | No deliberate human-voice run. |

Model latency and V.O.I.D overhead are separated wherever the data allow; no value in this table is estimated.

## 12. Known Limitations

### NOT VALIDATED
- Criterion 4's owner-attended physical microphone-toggle recovery (≤ 60 s).
- CI: `.github/workflows/tests.yml` has never run (nothing pushed); lock never re-installed in a fresh venv.
- Real human-voice spoken memory lifecycle; restart/recovery on the physical microphone; physical Ctrl+Space; long-term physical stability; wake-to-STT on a human utterance.
- `awaiting_confirmation` spoken by voice on the live runtime.
- Spec §2.3 composite interaction rows and p90 column reproduced by `perf report` (criterion 9).
- Provenance (human vs ambient) of the audio in the observed 15:15:59 interaction.

### HARDWARE BLOCKED (historical)
- All physical-microphone items were blocked from 08:17 to ~15:13 by a machine-wide Windows audio fault. The block has **cleared** (see §9); the items above remain unvalidated because they have not yet been run, not because they are blocked.

### KNOWN RISKS
- **The live process running V2 code is new** (started 15:15, alive ≈ 2 h at the last check, 17:15); behaviour over days, sleep/resume cycles and mic loss on the real hardware is unobserved.
- `doctor` prints `ok` for microphone/wake derived from a **stale** heartbeat (🟡, fix later; not a G0 criterion).
- Whisper can still invent text from long non-speech audio; the guard's 250 ms threshold was calibrated on synthetic SAPI voices and may need tuning (`voice.min_speech_ms`). Continuous noise passes the guard and is then rejected by the STT's own VAD.
- Gemini 5xx/503 exhaust retries and fail the task; the local model is not used as fallback (D-07, open by design until P2).
- `~/.void/tasks.sqlite` contains a number of validation task rows (goals typed/spoken during validation, memory answers redacted). No supported delete exists and direct row deletion was blocked by the permission model, so they were left. Task goals are stored verbatim (V1 behaviour).
- "Actually, <fact>" without the word "remember" is not a correction command; the model turns it into a pending proposal.
- 7 of the 13 `docs/v2-evidence/*.py` scripts (`ollama_bench`, `proto_gateway_fix`, `repro_gateway_idle_conn`, `repro_mic_recovery`, `validate_misc`, `validate_v1_defects`, `validate_v1_defects2`) hard-code the deleted worktree path; they are historical evidence, not runnable as-is.

### FUTURE P1 WORK (not implemented; not started)
- **T1.1 Engine-protected roots** (closes D-04, D-04b; readiness report: hotfix candidate) — first.
- T1.2 hash-chained audit log · T1.3 tool manifest + argument validation · T1.6 task-store schema/retention · T1.4 taint model · T1.5 URL policy (D-05) · T1.8 secret hygiene · T1.7 CapabilityEngine.
- G1 #2 replaces the 3 symlink skips with junction tests.

## 13. G0 Gate Assessment

**READY FOR OWNER REVIEW**

Reasoning, using only the project's own criteria:
- **8 of 11** criteria are **PASS** on CURRENT evidence (1, 2, 3, 5 [simulated], 6, 7, 8, 10). **No criterion FAILS.**
- **3** are **PARTIAL**, and each partial is a specific, named residue rather than an unknown:
  - **#4** — the software half passes; the *owner-attended physical* half has not been run (the plan defines it as owner-attended, and it is now unblocked).
  - **#9** — stage-level tables reproduce exactly; the composite interaction rows and p90 column are not producible from the legacy log.
  - **#11** — all local mechanisms verified; **CI has never run** (nothing pushed) and the lock was not re-installed in a fresh venv.
- The physical-microphone items are not counted as software failures; plan assumption A8 allows dev-isolated development without them.
- V1 preservation evidence is complete for tests and security source files (§7). No production state was altered by this report.

This is **not** an approval. The owner must decide whether to (a) accept the three PARTIAL items as deferred to a later gate,
(b) require any of them first (e.g. run the attended microphone-toggle check now that the microphone works, or push to obtain a
first CI run), or (c) not proceed. No G0 approval record exists.

## 14. Recommended Next Engineering Milestone

If the owner grants G0: **P1 → T1.1 Engine-Protected Roots** (un-overridable exclusion of the state directory, `local_config.yaml`,
credential/DPAPI stores and similar; closes D-04/D-04b, whose strict xfail remains). It is first in the plan's P1 order, needs
no microphone, and closes the one open authority-escalation path the readiness report flags. **Not implemented in this task.**
Open question for the owner: develop it on a new branch or directly on `main` (the V2 worktree no longer exists).

In parallel and independent of P1, the smallest owner-side action that would convert the largest set of NOT VALIDATED items is one
deliberate spoken memory cycle on the now-working microphone ("Hey V.O.I.D, remember that my test project is Orion" → accept via
`python -m void memory accept <id>` → ask → restart → ask → forget via CLI → ask), which can then be verified from the privacy-safe
log.
