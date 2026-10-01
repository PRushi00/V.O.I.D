# AgentRouter provider — validation report

Date: 2026-09-21 · Repository base commit: 1b1af96 (working tree, uncommitted) · Requested model: `gpt-5.6-sol`

**AgentRouter is a third-party OpenAI-compatible gateway/proxy (https://agentrouter.org/v1). It is not OpenAI.** V.O.I.D names it
`agentrouter` everywhere (provider name, credential, telemetry, messages).

## Status: NOT READY as the primary brain — the live gate has not been passed
The integration is implemented and fully mock-tested, but **no AgentRouter credential exists on this machine**, so no authenticated
request has been made. `AGENTROUTER_API_KEY` is absent from the process, User and Machine environments and from the OS keyring
(`agentrouter_api_key`). The five `OPENAI_API_KEY*` variables are FreeModel keys; they are deliberately **not** read or sent to
AgentRouter (sending a credential to a host it was not issued for would be a credential leak).

## What was verified live (no credential involved)
* `GET https://agentrouter.org/v1/models` **without any Authorization header** → HTTP 401 in ~0.45 s, `application/json`, body
  `{"error":{"message":"unauthorized client detected, contact support …"},"message":"UNAUTHENTICATED","success":false,"type":…}`.
  So the host is reachable over TLS, and errors use a different shape from OpenAI's. The message *"unauthorized client detected"*
  suggests the gateway restricts which clients may call it; **this is unverified** and could not be tested without a key. If a valid
  key still gets this response, the fix is not to spoof another client's identity — it is to ask AgentRouter support (or the owner) which
  clients are permitted.

## Not yet observed (needs the key)
authentication, `gpt-5.6-sol` listed in `/models`, a real completion, the response's `model` field, tool calling, structured
arguments, latency. **Verified model identity: unknown / not independently verified** (requested model: `gpt-5.6-sol`). Even after a
successful call, the `model` field a gateway returns is self-reported; it cannot prove which backend served the request.

## Architecture
* `void/providers/agentrouter_provider.py` — a thin identity subclass of the existing OpenAI-compatible client
  (`OpenAIProvider`): name `agentrouter`, one credential slot `AGENTROUTER_API_KEY` (environment, then keyring key
  `agentrouter_api_key`, held by the existing `CredentialPool`), fixed endpoint, host allow-list of exactly `agentrouter.org`
  (https only, no userinfo, redirects never followed). No HTTP logic is duplicated.
* One credential ⇒ no rotation. A credential-specific failure (401/403/quota/429) cools that credential (1 h; rate limit uses
  `Retry-After`, default 60 s) so `available()` is False and selection falls to the next provider; other failures end the call.
* `config/default_config.yaml`: `llm.agentrouter.model: gpt-5.6-sol`, `timeout_s: 60`, **no key**. It is registered but **not in
  the default provider order** (now `gemini` → `local`); select it explicitly with `llm.primary: "agentrouter"` (+ `fallback:
  ["local"]`) in `config/local_config.yaml`. Exactly one primary at runtime, Ollama the explicit fallback; no runtime provider choice.
* Shared client additions: `list_models()` (`GET /models`, one request, no rotation), broader recognition of spent-balance
  errors (`insufficient_*quota|balance|credit`, HTTP 402, bare-string bodies), messages that name the provider (`_display`).

## Error classification (shared client)
| Condition | Category | Behaviour |
|---|---|---|
| 401 | auth | credential cooled; call ends (single key) |
| 403 | forbidden | same |
| 402, or a body saying balance/quota is spent | quota | same, 1 h |
| 429 | rate_limit | same, `Retry-After` (default 60 s) |
| timeout | timeout | `ProviderUnavailable`, no cooldown |
| connection error, 5xx, unreadable body, redirect | network / server / malformed_response / other | transient error → the Agent's existing bounded retry, no cooldown |
| 400/413/422 | invalid_request | `ProviderUnavailable` |
| 404 / model_not_found | unsupported_model | `ProviderUnavailable` naming the model |
Provider error text is never read into exceptions/logs/telemetry (it can echo a key fragment); only status and sanitised type/code.

## Security boundary
Body sent = `model`, `messages`, `tools`; headers = `Authorization`, `Content-Type` only. Tool calls returned by the gateway are
untrusted proposals: they go through `Agent._run_call` → kill switch → RiskGate → protected roots → capability (tested with a HIGH-risk
tool the gate denies, and a write into the protected state dir). Tool output re-enters the conversation framed as untrusted. The provider counts as
a cloud provider for memory (`name != "local"`), so sensitive / non-cloud memory is withheld; normal memory that is `cloud_ok` **is**
sent to any cloud provider today — this task did not change that policy. Known third-party risk: the gateway sees every prompt,
tool schema and tool output; its data-handling terms are unverified.

## Live validation tool
`python scripts/bench/gateway_validate.py --provider agentrouter [--n 5] [--json out.json]` — models check → exact-reply completion
(`V.O.I.D_PROVIDER_TEST_OK`) with the response `model` field → a real Agent tool round trip on one read-only tool in a throw-away folder
(`list_directory`) through the RiskGate → a small latency sample. It stops at the first blocked step and prints only safe metadata. It is
exercised offline by `tests/test_agentrouter_provider.py`. To run it, set `AGENTROUTER_API_KEY` at User scope and start a **fresh** terminal.

## Limitations
* Chat Completions only; no streaming; no reasoning-effort/token settings are sent (unverified for this gateway).
* Failover to Ollama occurs when the next command selects a provider, not inside a failing run (existing architecture).
