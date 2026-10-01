# V.O.I.D brain / voice evaluation harness (evaluation only)

Development tooling for choosing V.O.I.D's primary model and voice. It is **not** imported by V.O.I.D, not collected by pytest
(`testpaths = tests`), and adds no dependency. It reuses V.O.I.D's real system prompt, tool schemas, memory-block format,
`GeminiProvider` translation/credential rotation/error classification and `LocalProvider` translation.

| File | Purpose |
|---|---|
| `evalset.py` | 37 V.O.I.D-specific prompts in 10 categories with deterministic checks (tool name, JSON-schema argument validity, keywords, concision, authority-claim regexes). |
| `llm_bench.py` | Runner + report. Streaming calls (TTFT + total), token usage, p50/p95, per-error-class reliability. |
| `remote_candidates.py` | OpenAI-compatible and Anthropic candidates (also usable through any OpenAI-compatible gateway via `base=`). Self-tested against a local mock server only; **never run against a real endpoint** (no keys were available). |
| `voice_script.py` | The standardized 11-line voice audition script + scoring rubric. Identical text for every provider. |
| `voice_bench.py` | Sample generator + latency probe for Gemini TTS and local SAPI (evaluation only, not a TTS adapter). |
| `mock_check.py` | Self-test of the remote candidates against a mock server with a fake key. |
| `fastpath_latency.py` | Controlled local latency harness: model path vs the deterministic fast path for "open <app>" (routing / execution / total local ms and model-call count). Injected transcript, scripted model, stubbed launch - not mic-to-speaker. |

## Safety properties
- The harness never executes a tool; tools are only *offered* to the model.
- Keys are read in-process (keyring / env) and only placed in a request header; results contain model output, timings and token
  counts. Error messages are scrubbed of key-shaped strings. Run in a throw-away `HOME` so `~/.void` is never touched.
- A model proposing a destructive call is scored as an *unsafe proposal*; in V.O.I.D the engine/RiskGate would still decide.

## Candidate spec
`gemini:<model>[:think=minimal|low|medium|high|<budget>]`, `ollama:<model>[:think=on|off][:ctx=N]`,
`openai:<model>;base=<url>;effort=<low|...>`, `anthropic:<model>;base=<url>;extra=<json>` (use `;` when a value contains `:`).

## Commands
```
python llm_bench.py list-candidates
python llm_bench.py probe  --candidate gemini:gemini-3.6-flash
python llm_bench.py run    --candidate gemini:gemini-3.6-flash:think=low --out results.jsonl --reps 3
python llm_bench.py run    --candidate ollama:qwen3:8b:think=off:ctx=8192 --out results.jsonl --lat-only --reps 3
python llm_bench.py report results.jsonl [more.jsonl] --fails
python voice_bench.py sapi --out voice_out
python voice_bench.py gemini --model gemini-3.1-flash-tts-preview --voices Kore,Charon --out voice_out
```
Run latency comparisons **sequentially** and at more than one time of day: provider-side latency was seen to swing from ~1 s to
>20 s within minutes on the same model and prompt.
