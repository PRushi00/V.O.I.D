# Deterministic fast path, provider hygiene and engine-protected roots

Scope of this phase: a model-free path for plain "open <known app>" commands; a configurable Gemini deadline and validated
Gemini thinking setting with classified failures; a correctly shaped Ollama request; engine-enforced protected roots (P1
T1.1, closing D-04 and D-04b); and hardening of the pairing window. **Not** in this phase (and unchanged): the
conversational voice/session redesign, a permanent provider or voice choice, OpenAI/Anthropic/OmniRoute adapters, a TTS
provider, VAD, runtime model/voice switching, shell execution, voice authorization.

## 1. Deterministic fast path

### Flow
```
Assistant.run(goal)
  ├─ memory command ("remember that …")            -- unchanged, no model
  ├─ FAST PATH  void/core/fast_path.py             -- decides only
  │     parse_launch(goal)  ->  app phrase | None
  │     FastPath.decide()   ->  FastPlan(launch_app{name: alias-key | app_id}) | None (+ reason)
  │     Agent.run_direct()  ->  Agent._run_call()  -- THE SAME FUNNEL as a model-proposed call:
  │           kill switch -> tool risk level -> RiskGate.authorize -> launch_app -> taint -> telemetry
  ├─ recall route                                   -- unchanged
  └─ Agent.run (model)                              -- unchanged; receives everything the fast path declines
```
The fast path **decides**; it never launches, never authorizes and never touches the filesystem. `void/core/fast_path.py`
imports neither `subprocess`, `os`, `shutil`, `pathlib` nor the risk module (a test asserts this statically). It reuses,
and does not duplicate, `launch_app` (the only code that starts anything), the alias map `_APP_ALIASES`, and
`AppCatalog.find` (the exact-match lookup behind `find_app`).

### Intent scope (deliberately tiny)
Accepted: one launch verb (`open` / `launch` / `start`, optional `up`), an optional polite prefix/suffix (`hey void`,
`please`, `could you …`), one app name of letters, digits, spaces and `. + ' -`, ≤ 40 characters.

| Refused by the grammar (falls to the model path unchanged) | Why it cannot be expressed |
|---|---|
| paths, drive letters, UNC, `~`, `%VAR%`, URLs | `\ / : ~ %` are not in the app-name alphabet |
| shell metacharacters `& \| ; < > $ ( ) { } [ ] * ? ^ " ` ` `` ` | not in the alphabet |
| file/script names (`x.exe`, `.bat`, `.ps1`, `.lnk`, …) | a dotted token with an executable/script extension is refused |
| compound or qualified requests (`… and …`, `… with …`, `… in …`) | connector words are refused: not a plain launch |
| anything that is not a launch (`close notepad`, `what time is it`) | no match |
| over-long or non-string input | length / type check |

### Resolution (exact, never fuzzy)
1. Spoken name → fixed alias key (`vs code`→`vscode`, `calculator`→`calc`, …) or the alias key itself. Highest priority.
2. Otherwise `AppCatalog.find(phrase)`: **exactly one** case-insensitive whole-name match → its opaque `app_id`.
   Zero matches → declined (`unknown`); more than one → declined (`ambiguous`; the agent asks which). Never a substring, never a guess.
3. Shells and administration consoles (cmd, PowerShell, terminal, WSL, regedit, mmc, python, …) are **never** fast-pathed,
   whether named by the sentence or by the catalog entry. (The model path still allows them behind the same gate as before.)
4. The tool argument is always the alias key or `app_id` chosen by the engine – text from the request never reaches `launch_app`.

The reply is engine-owned and short: `Opening Notepad.` – display names come from a fixed table or a sanitised catalog
name; no paths, tool names or exception text. If a launch fails the run is **not** reported as a success: it falls
through to the model path, which can use `find_app`.

### Security boundaries
* Kill switch: engaged before → the fast path does not run (normal path reports PAUSED); engaged mid-run → `StopRequested` → PAUSED, nothing launched.
* RiskGate: `launch_app` is LOW, so it is auto-authorized *by the gate* (one `authorize` call per launch, tested). If the owner's
  threshold would require confirmation, the fast path declines and the normal loop owns the prompt/deferral. A gate **denial** is final
  (reported, not retried through the model).
* Protected locations: `launch_app` now refuses a catalog target inside V.O.I.D's own state or a credential store
  (`EngineProtected.denies_launch`; browser/other-user trees are intentionally excluded so per-user browser installs still launch).
* Telemetry: `route` events `reason=fast_path` (`kind`, `llm_calls=0`) and `fast_path_miss` (`why`) – enumerations only, no transcript, no name.
* Records: the run is stored as a normal task (goal text is in `tasks.sqlite`, exactly like every other command).
* Off switch: `fast_path.enabled: false`.
* It needs no model: it works with no provider available.

### Latency (controlled local harness, `scripts/bench/fastpath_latency.py`, n = 300, results in `docs/v2-evidence/fastpath_latency.json`)
Transcript injected, model = instant scripted stub, launch = stub (nothing started). This is V.O.I.D's own work only – **not**
mic-to-speaker and **not** a real model.

| Path | Intent routing p50 | Execution p50 | **Total local p50 / p95** | LLM calls per command |
|---|---|---|---|---|
| A. STT → LLM → capability | 4.86 ms | ~0 ms (stub) | 7.34 / 8.47 ms | **1** |
| B. STT → intent → capability (fast path) | 0.09 ms | ~0 ms (stub) | 2.49 / 2.95 ms | **0** |

The decisive number is the model call, not the milliseconds: path A adds one real model round trip that this harness does not
pay (from the earlier evaluation, quoted not re-measured: 1.3 s flash-lite, 2.7 s local qwen3 think-off, ~7.5 s for the current
`gemini-3.6-flash` config). Path B removes it. First non-alias app name also pays a one-time catalog build (157 entries: 8.5 ms here).

## 2. Gemini configuration

| Key | Default | Meaning |
|---|---|---|
| `llm.gemini.model` | `gemini-3.6-flash` (unchanged; **no permanent model chosen**) | sent as configured |
| `llm.gemini.timeout_s` | `30` (clamped 1–300; bad values fall back to 30) | per-request deadline (`HttpOptions(timeout=ms)`); no retry inside it |
| `llm.gemini.thinking` | `""` (model default) | level name or integer budget, **validated against the model before it is sent** |

Thinking validation: `flash-lite` → `minimal|low|medium|high`; other `gemini-3.x+` → `low|medium|high`; `gemini-2.5` → integer budget
(−1…32768); unrecognised models → nothing is sent. `minimal` is never sent to a model not known to take it. An unsupported value is
**not sent** (a warning is logged, `provider.thinking_warning` says why) so a config typo degrades to the model default instead of a 400.

Failure handling (`classify_failure`; the original `_classify_error` used for rotation is unchanged):

| Class | Trigger | Behaviour |
|---|---|---|
| rate_limit | 429 / RESOURCE_EXHAUSTED | rotate to next credential, cool this one (≥ 3600 s) – as before |
| auth | 401 / 403 | rotate, cool, never retry the same key – as before |
| timeout | deadline / `*Timeout` / 408 / 504 | **fail fast**: `ProviderUnavailable`, key not cooled, not rotated |
| not_found | 404 | fail fast, names the model |
| invalid_request | 400 (incl. bad thinking level) | fail fast |
| server | 5xx | re-raised unchanged → the agent's existing bounded retry |
| network | connection errors | re-raised unchanged → same |

No provider-level retry loop was added. `ProviderUnavailable` is fail-fast in the agent (unchanged). `available()` semantics, the
`CredentialPool` and its cooldown are unchanged, so a rate-limited Gemini still falls back to the next provider at the next selection.
Error text from the SDK is never placed in the raised message (it is logged as a class only).

## 3. Ollama (LocalProvider)

| Key | Default | Effect |
|---|---|---|
| `llm.local.think` | `false` | top-level `think` field. Measured for qwen3:8b: 2.7 s p50 off vs 9.45 s p50 on. `null` = do not send |
| `llm.local.num_ctx` | `8192` | `options.num_ctx` (prompt + tools ≈ 2.3k tokens) |
| `llm.local.keep_alive` | `"10m"` | top-level `keep_alive` |
| `llm.local.timeout_s` | `120` | read timeout (connect timeout fixed at 3 s) |

If a model does not support `think` (Ollama answers 400 mentioning it) the provider remembers that, omits it, and repeats the request
**once** – never a loop. A 404 means the model is not installed → `ProviderUnavailable`. `available()` now also reads `/api/tags` and
requires the configured model to be installed (read-only; **nothing is ever pulled or downloaded**), so a missing model falls through
to the next provider instead of failing at generation. No GPU assumption is made (no `num_gpu`, etc.). A malformed 200 body still
propagates as before (the agent's bounded retry).

## 4. Engine-protected roots (P1 T1.1; D-04, D-04b)
`void/security/protected.py` – a fixed, **add-only** set computed from code and environment (not from config, the model, memory
or a tool result): `~/.void` and the active state dir, the repo `config/`, `~/.ssh`, `~/.aws`, `~/.gnupg`, `.omniroute`, Windows
Credentials/Protect/Vault, browser profile stores, other users' profiles. Write-deny (mutation only): V.O.I.D's code, Windows,
Program Files, ProgramData. Enforced in `FileActions._confine` (the single choke point) and by filtering every walk.
Two independent fail-closed layers: **canonical** (UNC/device/`\\?\` rejection, alternate-data-stream rejection, trailing dot/space
stripping, `resolve()`, case-insensitive containment) and **identity** (`os.stat` device+inode of the candidate and its existing
ancestors vs the protected roots – defeats junctions, symlinks, reparse points – plus a bounded hard-link scan). Owner
`allowed_roots`/`protected_roots` behave as before and can only add. D-04b (agent-planted pairing window) is closed; its xfail is removed.

### Pairing window (F)
Verified: 300 s lifetime, single-use, replay rejected, wrong guess neither burns nor opens the window, token compared in constant
time, file only in the (now protected) state dir, atomic write. Two gaps found and fixed: a planted `expires_at` of `Infinity`/`NaN`
made a window that never expired, and a planted far-future expiry was honoured. Non-finite expiries are now unreadable and a window
claiming to outlive the configured lifetime (+30 s clock slack) is treated as expired and discarded.

## 5. Limitations
* The grammar is English and single-verb. "open notepad and …" or any paraphrase goes to the model path (still correct, just slower).
* Only exact catalog names match; "open word" works only if an entry is named exactly `Word`.
* The Gemini deadline is per request; there is no whole-turn budget, and a slow 5xx-retry loop in the agent is unchanged.
* A timeout does not cool Gemini, so the next command tries it again (and waits up to the deadline again) before any fallback.
  A short timeout-driven provider cooldown belongs with the conversation-level failover work.
* Hard-link detection covers the small secret directories only (browser/other-user trees are protected by name and directory identity).
* `denies_launch` is lexical on the resolved path (junction targets are followed by `resolve()`).
* The latency harness excludes a real model, STT, audio and the real launch.

## 6. Next phase (not started)
Conversational voice/session architecture (`docs/V2_VOICE_CONVERSATION_PROPOSAL.md`): a conversation session and VAD, streaming
turn-taking and barge-in, a TTS provider, runtime provider/voice switching, provider-level failover with a timeout cooldown, and the
final provider/voice decision. This phase leaves clean seams for them: `_NoProvider`/`run_direct` (model-free turns), the
classified provider errors and the validated provider options.
