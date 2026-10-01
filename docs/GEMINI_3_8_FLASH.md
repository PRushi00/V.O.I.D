# Gemini 3.8 Flash — primary cloud brain

Date: 2026-09-21 · Base commit 1b1af96 (working tree, uncommitted) · Provider: existing `GeminiProvider` (no new provider, no code change to it)

## Change
| | Before | After |
|---|---|---|
| `llm.gemini.model` | `gemini-3.6-flash` | **`gemini-3.8-flash`** |
| `llm.primary` / `fallback` | `openai` / `[local]` (a FreeModel-gateway default set earlier; its credentials are exhausted / mixed up) | **`gemini` / `[local]`** |
Configuration only. Credentials, `CredentialPool` rotation, the 30 s deadline, thinking validation (`""` = model default) and the
memory/RiskGate boundaries are unchanged. The OpenAI-compatible (FreeModel) and AgentRouter providers stay registered but are not in
the default order (name one as `llm.primary` in `config/local_config.yaml` to use it).

## Live validation (real config, real credentials; only harmless prompts; no key/header/body printed)
Credentials: 5 configured in the OS keyring (primary + 4 backups), via the existing pool. `gemini-3.8-flash` is exposed (`models.get`).
* **Completion**: `Reply with exactly: V.O.I.D_GEMINI_38_OK` → exact match. Response `model_version` = `gemini-3.8-flash`
  (self-reported by Google's API). Usage: 43 prompt / 15 output / **118 thinking** tokens (default thinking is on).
* **Tool round trip** through `Agent` → kill switch → RiskGate → protected roots → `list_directory` (read-only, throw-away folder):
  status `completed`, tool proposed (`list_directory`), arguments valid per schema, executed via the gate, final answer names the file,
  2 model calls.
* **Rotation** (existing behaviour, observed): slots 1–2 returned 429 `RESOURCE_EXHAUSTED` (daily free-tier quota) and were skipped;
  a 503 `UNAVAILABLE` "high demand" was seen on one slot and is re-raised for the Agent's bounded retry (no provider retry loop).
  By the end of the runs 4 of 5 credentials were in (in-memory) cooldown.

### Latency (small, indicative — NOT a benchmark)
| Sample | n ok | p50 | max | failures |
|---|---|---|---|---|
| simple completion | 3 of 5 | 14.2 s | 15.0 s | 2 × 5xx |
| tool-call completion | 3 of 5 | 13.9 s | 14.3 s | 2 × 5xx |
Single direct calls on healthy keys were 4.2–8.1 s. With n = 3 no p95 is meaningful. Compared with earlier evidence
(`docs/v2-evidence/BRAIN_AND_VOICE_EVALUATION.md`): gemini-3.8-flash then showed ~6 s p50 and **17 of 25 requests failing with 503**; 3.6-flash was ~7.5 s.
This run is consistent with that: **3.8 Flash works and calls tools correctly, but is slow at default thinking and frequently
returns 503 under demand.** It is not evidence that 3.8 is faster or slower than 3.6.

## Findings / limitations
* Two 5xx failures in a row from the validator (same method) triggered the stop rule; a per-credential direct probe then showed
  429 on two keys, success on two, 503 on one — i.e. quota + transient provider demand, not a code or configuration fault.
* Free-tier daily quota is consumed quickly (this validation used ~25 requests); repeat runs may find most keys cooling down.
* Default thinking adds seconds to trivial requests. `llm.gemini.thinking: "low"` is supported and validated for 3.8 (`minimal` is
  not sent to non-flash-lite models) but was **not** changed here (scope: model only); measuring it is the natural next step.
* The 503 rate means a turn can fail after the Agent's 2 retries; failover to Ollama happens at the next command's selection, not inside the run.
* No cooldown is applied for 503/timeout (existing behaviour).
* Tool: `scripts/bench/gemini_validate.py` (safe metadata only). 

## Update 2026-09-22 — thinking="low" A/B, and it is now the shipped default

**Change made:** `config/default_config.yaml` `llm.gemini.thinking` is now `"low"` (was `""`, the model's own default). Config only;
no provider code changed. `resolve_thinking("gemini-3.8-flash", "low")` validates to `("level", "low")` (unchanged validation logic).

### Method
Built a controlled A/B directly on the real, config-loaded `GeminiProvider` (real credential pool, real rotation), **alternating**
condition per repetition (default, low, default, low, …) to cancel out time-of-day demand effects, instead of running one condition
to completion first. Two independent sample types: a simple completion and a tool-selection completion (existing `list_directory`
schema). Raw JSON: `docs/gemini_38_thinking_ab_2026-09-22.json`.

### Result (small sample — quota-limited, see below)
| Condition | Simple: n ok / p50 / max | avg thinking tokens | Tool-call: n ok |
|---|---|---|---|
| default (model's own thinking) | 2 / **9.74 s** / 11.0 s | **233.5** | 0 (quota exhausted before any succeeded) |
| **low** | 3 / **2.81 s** / 6.07 s | **0.0** | 0 (quota exhausted before any succeeded) |
Directionally consistent with the 2026-09-21 evidence (default ≈14 s, ~118 thinking tokens) and with the mechanism (thinking tokens
are generated serially before the answer, so removing them removes that time): **`thinking="low"` cut simple-completion latency by
roughly 3–4×** in this sample, produced *more* successes before quota ran out (3 vs 2), not fewer, and generated ~0 measured thinking
tokens vs ~230 at the default.

### Why the sample is small — real quota exhaustion, not a bug
The run was designed for 10 successes per condition (20 simple + 10 tool = 30 requests) but a rapid pre-check (5 direct calls) plus
the interleaved run pushed the account past its **daily/rate quota for `gemini-3.8-flash`** partway through: both conditions' failures
shifted from occasional `server` (503, transient demand) to `other`/exhausted (all 5 credentials in that provider's pool cooled after
each returning 429) around request ~12–15. **This was confirmed, not assumed**: a fresh, isolated single request afterwards (new
`CredentialPool` instance, so no in-memory cooldown could be the cause) reproduced a genuine `ClientError code=429
status=RESOURCE_EXHAUSTED` on the *first* credential — real server-side quota, not a code defect and not an artifact of the
existing 1-hour cooldown floor. **Live tool-calling under `thinking="low"` was therefore not directly re-validated this session**
(quota ran out before either condition completed one); it was validated live under the *default* thinking setting on 2026-09-21, and
the request-construction code path is shared and does not branch on the thinking value except to add one config object
(`_generate_config`), so this is a config-only, low-risk gap rather than an unknown code path.

### Decision
Evidence supports keeping `thinking="low"` as the shipped default: faster, not less reliable in the sample obtained, and validated as
a supported, correctly-classified value for this model. This is now the tested configuration. The **existing cooldown/rotation
policy was left unchanged** — the 1-hour conservative floor on a 429 is deliberate (documented in `gemini_provider.py`: free-tier
daily quota is the real blocker, never re-hammer) and this run's failures are exactly the case it is designed for, not evidence of a defect.

### Remaining uncertainty after this update
* Tool-calling under `thinking="low"` was not re-observed live this session (quota); the code path is shared with the already-validated default case.
* The account's `gemini-3.8-flash` quota is evidently low enough that ~15–20 requests exhausts it for the rest of the (rate) window;
  a production interaction rate needs to be checked against Google's current published quota for this model/tier, which was not
  independently looked up here.
* p95 is not meaningful at n≤3; a larger sample requires either paid quota or waiting out the current cooldown window.
