# OpenAI GPT-5.6 Sol — primary online brain

**Endpoint: FreeModel's OpenAI-compatible gateway** — `https://work.freemodel.dev/v1` (Chat Completions at
`/v1/chat/completions`), model `gpt-5.6-sol`. The five `OPENAI_API_KEY*` credentials are FreeModel/WorkBuddy keys, not
api.openai.com keys (the first live attempt against api.openai.com returned `invalid_api_key`).

**Status: implemented and mock-tested. Live: the primary credential authenticates (GET /v1/models → 200, `gpt-5.6-sol` listed) but
chat completions returned HTTP 401 `{"error":"Insufficient balance"}` — that key's FreeModel balance is spent.** A successful
`gpt-5.6-sol` completion has therefore NOT yet been observed. See "Live validation".

## Architecture
```
Agent → OpenAIProvider.generate()  (gpt-5.6-sol, Chat Completions over `requests`; returns DATA only)
          └─ tool proposal → Agent._run_call → RiskGate → Capability   (unchanged; the provider never executes or authorizes)
Ollama (LocalProvider) = offline/degraded brain.   Deterministic fast path = 0 model calls, 0 API calls.
```
**Update: the default primary is now Gemini (`gemini-3.8-flash`, see `docs/GEMINI_3_8_FLASH.md`); this provider stays registered and configured
(`llm.openai.model: gpt-5.6-sol`, `timeout_s: 60`) but is not in the default order.** Select it with `llm.primary: openai` in
`config/local_config.yaml`. Original text: default order was `llm.primary: openai`, `llm.fallback: [local]`.
No model switching happens at runtime; selection is per command, as before: OpenAI if a usable credential exists, else Ollama.

## Credentials (same model, five slots)
`OPENAI_API_KEY` (primary), `OPENAI_API_KEY_BACKUP_1..4`. Read on demand from the environment (fallback: the OS keyring under the
lower-cased slot name), held by the existing `CredentialPool` (names + cooldowns only). Values are never stored, logged, put in
config/SQLite/telemetry, or placed in exception text. The API host is fixed in code (`https://work.freemodel.dev/v1`); config cannot
redirect a key to another host; the constructor accepts an endpoint only if it is https on an allow-listed host (`work.freemodel.dev`,
`api.openai.com`), and redirects are never followed. Safe status: `OpenAIProvider.credential_status()` (slot label, configured?, cooling down?).
Note: environment variables set at User scope reach only processes started afterwards — restart V.O.I.D / its terminal.

## Primary/backup policy
The pool always offers the first slot that is not cooling down, so healthy traffic uses only the primary (no round-robin).
One `generate()` makes **at most 5** HTTP attempts, each slot once.

| Failure | Category | Action | Cooldown of that slot |
|---|---|---|---|
| 401 | auth | rotate | 1 h |
| 401/402/429 whose body says the balance/quota is spent (FreeModel: `{"error":"Insufficient balance"}` with HTTP 401) | quota | rotate | 1 h |
| 403 (not region block) | forbidden | rotate | 1 h |
| 429 `insufficient_quota` / billing | quota | rotate | 1 h |
| 429 rate limit | rate_limit | rotate | `Retry-After` (default 60 s, max 1 h) |
| timeout | timeout | **no rotation**; `ProviderUnavailable` (fail fast) | none |
| network error, 5xx, unreadable 200 body | network / server / malformed_response | **no rotation**; transient error → the Agent's existing bounded retry | none |
| 400 / 413 / 422 | invalid_request | **no rotation**; `ProviderUnavailable` | none |
| 404 / `model_not_found` | unsupported_model | **no rotation**; `ProviderUnavailable` naming the model | none |
| 403 region block | other | **no rotation** | none |

Outcomes above the provider (RiskGate denial, tool-argument validation, capability failure, protected-root denial, kill switch,
policy rejection) never reach it, so they cannot spend a credential (tested through a real `Agent`). Cooldowns are in-memory, so a
restart clears them; no request is ever made merely to probe a key. When every slot is cooling down `available()` is False and the
next command selects Ollama.

## Telemetry / logs
`provider_call` event: provider, model, slot label, attempt, duration, ok, category (enumerations/numbers only). The provider's
error message is never read (it can echo a key fragment); only its sanitised `type`/`code` identifiers are used.

## Live validation
Observed against `work.freemodel.dev` with the PRIMARY credential only (no credential value was printed):
* `GET /v1/models` → 200, 3 models, `gpt-5.6-sol` present → the key authenticates and the model id is accepted by the catalogue.
* `POST /v1/chat/completions` (model `gpt-5.6-sol`, one short message) → **401 `{"error":"Insufficient balance"}`**, ~4 s, JSON content type.
  FreeModel returns errors as a bare string (not OpenAI's `{"error":{type,code,message}}`) and reports a spent balance as 401.
  V.O.I.D now maps that phrase to the `quota` category (still rotates to the next credential, 1 h cooldown) without ever
  exposing the text.
Still to observe once a credential with balance is used: a completed chat, tool calling, latency. Command (primary only, safe output):
`python scripts/bench/openai_smoke.py --reps 3`; with `--allow-backups` it applies the normal policy (primary first, then backups only after a
credential-specific failure).

## Data boundary (what crosses to the FreeModel gateway)
Every request body is exactly `model`, `messages`, `tools` (tested); headers are `Authorization` and `Content-Type` only. Concretely a
request can contain:
| Content | Sent? | Notes |
|---|---|---|
| V.O.I.D system prompt | yes | fixed text |
| Tool schemas (names, descriptions, parameter schemas) | yes | needed for tool calling |
| The owner's goal / conversation turns | yes | including any file or app names the owner says |
| Tool outputs (file listings, file contents read by a tool, app names, search results) | yes | so paths, filenames and file contents the model asked a tool to read cross the boundary; they are framed as untrusted data |
| Memory context | **filtered** | the provider counts as *cloud* (`name != "local"`), so memory items marked sensitive or `cloud_ok=0` are withheld; unchanged |
| API keys, RiskGate state/thresholds, kill-switch state, protected-root list, pairing token, state-dir contents | **no** | never placed in messages; protected roots also stop file tools reading them, so their contents cannot be tool output either |
| Task database / checkpoints | no | only the messages of the current task are replayed |
The fast path sends nothing (no request at all). FreeModel's published privacy policy (as relayed by the owner, not re-verified here)
says it logs request metadata (timestamps, model, token counts, latency) and does not persistently store prompts/responses unless a
logging feature is enabled. That is the provider's statement, not a V.O.I.D guarantee, and it is a different data-handling posture from
direct OpenAI access; sensitive material must keep relying on V.O.I.D's own controls (memory `cloud_ok`, protected roots), or on Ollama.

## Limitations
* Failover to Ollama happens at the next command's selection, not inside a failing run (unchanged architecture). With invalid keys the
  first command after start fails after 5 quick 401s, then Ollama serves until the 1 h cooldown ends.
* Chat Completions is used; `gpt-5.6-sol` parameter compatibility (e.g. reasoning effort, output-token caps, tool calling) is unverified live, so none is sent.
* Streaming is not used (later voice phase).
