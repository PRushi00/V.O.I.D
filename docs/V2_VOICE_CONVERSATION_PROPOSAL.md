# V.O.I.D V2 — Conversational Voice: Implementation Proposal

*Planning document only — 2026-09-21, repository `C:\V.O.I.D`, `main` @ `1b1af96`. No source, test, config or production state was changed to produce it.*
*Evidence labels: **[C]** read from the code/config in this repo · **[M]** measured on this machine (source stated) · **[V]** verified against external docs today · **[E]** engineering estimate/target · **[U]** unknown, must be measured.*

---

## 0. Reading guide and headline findings

The owner's vision (persistent conversational session, deterministic fast path, better/streaming TTS, provider routing, interruption, spoken style, minimal UI) is achievable **mostly by extending what exists**. Nothing needs to be rewritten. The five findings that most change the plan:

1. **Perceived latency is LLM-dominated, not TTS-dominated.** Real-runtime interaction 2026-09-21 15:15:59 **[M, n = 1, log timestamps]**: endpoint→first spoken word **14.56 s** = STT 1.20 s + LLM call 12.35 s + ~0.9 s other agent time + **0.09 s** to first audio. The "TTS ≈ 4.7 s" figure is **playback duration** of a 70-character reply, not time-to-first-audio (SAPI starts speaking ≈ 94 ms after the request). Cloud TTS or streaming TTS will improve voice *quality* and long-answer playback overlap; it will **not** fix the 14 s wait. The levers that do are (a) not calling the LLM for deterministic commands, (b) a faster model/no thinking for the calls that remain, (c) an instant acknowledgement.
2. **The infrastructure for most of this already exists** — `VoiceSession` generation tokens and stale-result dropping, PTT barge-in during speech, a pluggable TTS registry, a Silero VAD model bundled in the installed `faster-whisper`, a multi-key Gemini `CredentialPool` with 429 rotation, `Agent._run_call` as the single tool funnel, and pre-LLM routing hooks in `Assistant.run`.
3. **The approved V2.0 plan already contains a flagged deterministic fast path (T2.8)** and lists sentence-queued TTS, a Silero endpointer and barge-in as *future*. This vision therefore *extends* the approved scope and needs an explicit owner decision on ordering versus P1 (§20).
4. **Two real gaps found in the code** that the vision depends on: (a) each `Assistant.run(goal)` is a **fresh task with no conversation context** [C]; (b) **`KillSwitch.handle_command` (the spoken "VOID, STOP EVERYTHING") is never called by the voice runtime** — it has tests but no call site [C]. Spoken stop is therefore not wired today.
5. **"Open YouTube in Opera GX" needs a capability that does not exist.** `open_path` uses the system default browser and `launch_app` accepts no arguments [C]. That is a new, security-sensitive tool (launch a chosen app with a URL) and should not ride on the MVP.

---

## 1. Current architecture [C unless stated]

```
mic ─► AudioCaptureBroker (one physical owner, 16 kHz mono int16 frames, fan-out)
         ├─► Wake detector (Gen3 Whisper-encoder + ONNX; armed ONLY while session IDLE, +500 ms settle)
         └─► BrokerCapture (buffers frames) ◄─ _WakeEndpointer (energy RMS gate 500; lead grace 0.4 s;
                                                trailing silence 0.8 s ends utterance; no-speech 4 s; max 15 s)
wake ──► VoiceController._on_wake_detected ─► session.on_ptt_press(source="wake")   (same reducer as PTT)
VoiceSession reducer (pure): IDLE → LISTENING → CAPTURED → TRANSCRIBING → DISPATCHED → SPEAKING → IDLE
                              (+ ERROR, STOPPED[kill switch latch], CLOSED); generation token drops stale results;
                              PTT_DOWN while SPEAKING = barge-in (TTS_STOP + new generation + mic open)
STT: FasterWhisperSTT (small, int8, CPU, greedy, vad_filter) — one blocking decode per utterance
     ─► min-speech guard (1b1af96) ─► Assistant.run(transcript) under channel="voice"
Assistant.run: memory command? → memory-first recall route? → Agent.run(goal)   (fresh Task, fresh messages)
Agent: LLM call(s) via ProviderRegistry.select() (first available of gemini→local, per run)
       → tool call → _run_call (kill switch → tool.effective_risk → RiskGate.authorize → execute)
       → _deterministic_completion (one-shot terminal tools skip only the FINAL LLM call)
result.result (raw LLM text / tool summary line) ─► TTS (SAPI, single-slot replace semantics) ─► speakers
status phrases (constant) for AWAITING/BLOCKED/FAILED/PAUSED (D-13)
```

Session states drive the tray indicator (`state → colour/tooltip`) and the developer orb (`VoiceState → VisualMode`) through `VoiceStateBridge`.

**Measured on this machine** (prior + current-session logs; small n, not benchmarks):

| Segment | Value | Source |
|---|---|---|
| Endpoint silence wait | 0.8 s (config) + 0.4 s lead grace | [C] |
| STT decode (small, CPU int8) | 1.20 s for 1.65 s audio (RTF ≈ 0.7); p50 1.19 s, n = 11 | [M] log 15:16 / perf report |
| LLM call (Gemini) | 12.35 s (n=1 real); p50 6.4 s, n = 8 (mixed harness); V1 tool-call p50 2.92 s, n = 6 | [M] |
| Tool execution (`launch_app`) | 0.06 s | [M] perf, spec §2.3 |
| Time to first audio (SAPI) | ≈ 0.09 s | [M] `SPEAK_STARTED`→`TTS_SPEAK_STARTED` |
| Playback of 70 chars | 4.7 s | [M] |
| Endpoint → first spoken word | **14.56 s** (real, n = 1) | [M] |

---

## 2. Problems confirmed by code / evidence

| # | Problem | Evidence |
|---|---|---|
| P1 | One wake = one command; the wake detector is armed only in IDLE and disarmed during capture/speech | `runtime.py::_reconcile_wake`, `_begin_wake_capture` [C] |
| P2 | A wake capture ends on `no_speech` (4 s) or `silence` (0.8 s) and returns to IDLE → wake required again | `_WakeEndpointer` [C]; log 15:16:44 `no_speech` [M] |
| P3 | No conversational context — every utterance is a new Task with `[system, user]` only | `Agent.run` [C] |
| P4 | Deterministic commands still pay an LLM tool-selection round; `terminal_on_success` only skips the *final* call | `_deterministic_completion` [C] |
| P5 | "Opera GX" does not resolve by exact name; `Notepad` has 2 catalog entries; several apps have duplicates | Read-only `AppCatalog` probe: `find('Opera GX')→[]`, `find('Opera*')→[opera(exe), Opera GX Browser(lnk)]`, `find('Notepad')→2×exe`, `Discord→2`, `Cursor→3` [M] |
| P6 | Raw model/tool text is spoken; no voice-style instruction, no path/ID filtering | `SYSTEM_PROMPT` has no voice guidance; `_run_dispatch` speaks `result.result` [C] |
| P7 | TTS has a single replace-slot, no queue, no streaming, no cloud provider, factory takes no config | `SapiTTS`, `create_tts_provider` zero-arg factories [C] |
| P8 | Provider selection is per-run, first-available; single Gemini model; no deadline, no role-based model, no latency/health record; D-07 open | `ProviderRegistry.select`, `GeminiProvider.generate` [C]; strict xfail D-07 |
| P9 | Energy-gate endpointer would trigger on any noise in an always-on session | `_rms_int16 ≥ 500` [C] |
| P10 | Spoken kill phrase is not wired | `KillSwitch.handle_command` has no runtime caller [C] |
| P11 | Echo: TTS output reaches the laptop microphone; today handled by disarming wake during speech + drain before re-arm | `_reconcile_wake`, `_arm_wake_locked` [C] |
| P12 | STT runs on CPU although CTranslate2 reports 1 CUDA device (float16 supported); runtime DLLs/perf unverified | `ctranslate2.get_cuda_device_count()==1` [M]; `stt_device: cpu` [C] |

---

## 3. Desired architecture

```
                    ┌──────────── ConversationMode (owned by the controller) ────────────┐
                    │  STANDBY  ──wake──►  ACTIVE  ──"go to standby"/kill/shutdown──► STANDBY │
                    └────────────────────────────────────────────────────────────────────┘
mic ─► broker ─► [STANDBY: wake detector]  [ACTIVE: open-mic SpeechStartDetector (Silero VAD + pre-roll ring)]
                         │ utterance start (pre-roll seeded)
                         ▼
        VoiceSession per-utterance reducer (existing) : LISTENING → CAPTURED → TRANSCRIBING → DISPATCHED → SPEAKING
                         │ endpoint (VAD trailing silence) — never ends the SESSION, only the utterance
                         ▼
        min-speech guard ─► local STT (faster-whisper; CPU/CUDA by config)
                         ▼
   ┌──────────────────── IntentRouter (pure, deterministic, pre-LLM) ─────────────────────┐
   │ 1 voice control   ("go to standby")            → ConversationMode change (no task)     │
   │ 2 memory command / recall route (existing)                                              │
   │ 3 FAST PATH       (open/launch known app/site/folder) → Agent.run_direct → _run_call    │
   │ 4 else            → LLM route (session context + memory, ModelRouter, deadlines)        │
   └────────────────────────────────────────────────────────────────────────────────────────┘
        LLM path: Agent loop ─► _run_call ─► kill switch ─► effective_risk ─► RiskGate ─► tool   (unchanged authority)
                         ▼
   structured result (internal, untouched) ─► SpokenPresenter (pure) ─► SpeechSegmenter ─► SpeechQueue(gen-tagged)
                         ▼
   TTSProvider: ElevenLabs (HTTP PCM stream) ──fallback──► SAPI          ──► sounddevice playback
                         ▼  TTS_DONE (+ hangover, drain) → back to ACTIVE listening
```

Design rules: **reuse the existing reducer and generation tokens; add a mode layer above it, an activation source beside wake, and pure modules for routing/presentation; never a second authorization path.**

---

## 4. State machine

### 4.1 Mapping the owner's states onto the existing machine

The existing per-utterance reducer stays. `ConversationMode {STANDBY, ACTIVE}` is an **orthogonal flag** owned by the controller. The owner's list maps as follows — **no new reducer states are required**:

| Owner's state | Implementation | UI state |
|---|---|---|
| STANDBY | mode=STANDBY, session=IDLE, wake armed | STANDBY |
| ACTIVE_LISTENING | mode=ACTIVE, session=IDLE, open-mic detector armed | ACTIVE |
| CAPTURING | session LISTENING / CAPTURED | LISTENING |
| PROCESSING | session TRANSCRIBING / DISPATCHED (router, LLM, tool) | THINKING |
| SPEAKING | session SPEAKING | SPEAKING |
| INTERRUPTED | **transient**: SPEAKING + (PTT \| speech-start) → `TTS_STOP` + `NEW_GENERATION` → LISTENING; emitted as a perf event `interrupt` | LISTENING |
| RETURN_TO_LISTENING | **transition** SPEAKING → IDLE(ACTIVE) after `TTS_DONE` + hangover; re-arms open-mic detector | ACTIVE |
| SHUTDOWN | CLOSED (existing) | — |
| (existing) ERROR / STOPPED | unchanged; mic health orthogonal signal unchanged | ERROR / STOPPED |

### 4.2 Transitions (new/changed rows only)

| Event | From | To | Effects |
|---|---|---|---|
| wake | mode STANDBY, IDLE | mode ACTIVE, IDLE | disarm wake; speak "Yes?" (local, pre-synthesised); arm open-mic detector; **no capture window/timeout** |
| speech-start (VAD) | ACTIVE, IDLE | LISTENING | `NEW_GENERATION`; seed capture with pre-roll (≈0.3–0.5 s); attach endpointer |
| utterance-end (trailing silence) | LISTENING | CAPTURED → TRANSCRIBING | as today; `endpoint` perf event |
| silence with no speech | ACTIVE, IDLE | ACTIVE, IDLE | **nothing** — the detector simply keeps waiting; the session never times out on silence |
| STT result | TRANSCRIBING | DISPATCHED (or IDLE if empty/guarded) | empty/guarded → back to ACTIVE listening, no LLM |
| route = voice control (standby) | DISPATCHED | mode STANDBY, IDLE | cancel in-flight work (new generation), `TTS_STOP`, speak "Going to standby" (local), disarm open-mic, arm wake |
| route = fast/LLM result | DISPATCHED | SPEAKING (or IDLE if silent) | presenter → segmenter → queue |
| TTS start / chunk boundaries | SPEAKING | SPEAKING | queue advances only for the current generation |
| TTS done | SPEAKING | IDLE (mode unchanged) | hangover 400–700 ms + `drain()`, then detector re-armed (**half-duplex**: detector ignores frames while SPEAKING) |
| interruption (PTT; experimental voice) | SPEAKING | LISTENING | `TTS_STOP`, `NEW_GENERATION`, queue flushed; stale chunks/results dropped by generation |
| kill switch | any | STOPPED | unchanged (authoritative); mode forced STANDBY on re-arm |
| error / timeout | any | ERROR → IDLE | unchanged; mode preserved unless `session_error_standby` policy triggers |
| shutdown | any | CLOSED | unchanged |
| flood / safety valve (§11) | ACTIVE | STANDBY | spoken + UI notice |

Invariant tests: mode changes only via wake, the standby route, kill switch, shutdown or the safety valve; **silence never changes mode**; a stale generation can never change mode or speak.

---

## 5. Fast-path routing design

**Existing pieces to reuse:** `Assistant.run` already hosts pre-LLM routes (memory command, recall) [C]; `Agent._run_call` is the single funnel [C]; `AppCatalog` engine-owned app ids [C]; the approved plan T2.8 specifies `run_direct` and a flagged fast path [plan §T2.8].

**Design:** new pure module `void/core/fast_path.py` (grammar + resolver). It produces **an ordinary tool call** (`launch_app(name=<app_id>)` / `open_path(target=<engine-known URL or folder>)`); `Assistant.run` hands it to a new `Agent.run_direct(goal, tool, args)` that creates a normal Task and calls `_run_call`. Therefore kill switch, `effective_risk`, `RiskGate`, taint bookkeeping, perf events and (later) the CapabilityEngine apply **identically**. Static test: the fast-path module imports neither `RiskGate` nor tool implementations.

**Grammar (MVP, table-driven, normalised STT text):** strip fillers/politeness/address ("V.O.I.D", "please", "can you", "hey"), lowercase, remove punctuation. Intents: `open|launch|start|run? <name>`. `run` is excluded (implies commands). Compound utterances (`and`, `then`, `also`, `;`) → LLM. Any token from a deny-list (`delete remove erase format shell powershell cmd install uninstall kill terminate registry`) → LLM.

**Resolution (engine data only; nothing from speech becomes a path/command):**
1. Owner alias table `fast_path.aliases` (config; machine-specific, in `local_config.yaml`): `"opera gx" → "Opera GX Browser"`, `"notepad" → app_id`, `"file explorer"`, site aliases (`"youtube" → https://www.youtube.com`) and folder aliases (`downloads`, `documents`, `desktop`).
2. Else exact/normalised catalog match (no prefix/glob — the glob that matched `Opera*` is unsafe for speech).
3. Identical-name duplicates collapse only if same kind and same target; otherwise **ambiguous → fall through to the LLM** (evidence P5: Notepad ×2, Discord ×2, Cursor ×3).
4. Must be LOW risk (`effective_risk`) or fall through. `close app` is HIGH ⇒ never fast; the LLM/awaiting path applies and voice announces the constant phrase.

**Acknowledgement:** per-intent local template ("Opening Notepad.", "Done."), independent of `ToolResult.summary` (which is LLM-facing and may contain paths/URLs).

**Not in the fast path:** free-form paths or URLs from speech, `open file <name>` (needs search + disambiguation → LLM), deletes/writes, anything MEDIUM/HIGH, time/status (no tool exists; plan V2.2), *"open X in Opera GX"* (new capability, D4).

**Safety valves:** flag `fast_path.enabled` default **false** until an evaluation shows **0 wrong-app launches** over a noisy-STT variant table (plan G2 #7); telemetry `route{kind: fast|llm|standby|memory}`; fast-path miss costs nothing but the LLM round it would have paid anyway.

---

## 6. STT strategy

Keep `FasterWhisperSTT` behind the existing `STT` interface (local, per-utterance) — **no cloud STT in the MVP** (privacy/cost; §11).

| Change | Why | Cost/risk |
|---|---|---|
| **Silero VAD as the speech gate/endpointer** using the model already bundled in the installed faster-whisper (`silero_vad_v6.onnx`; `SileroVADModel.__call__(audio, num_samples=512, context_size_samples=64)`) **[C, M: present]** | The 500-RMS energy gate is unsuitable for always-on listening (P9). No new dependency (`onnxruntime` already present). | Wrapper + calibration on the SAPI corpus from the guard work; verify streaming use of the model API at implementation time **[U]** |
| Pre-roll ring buffer (≈0.3–0.5 s, RAM only) | First word after speech-start is not lost | Bounded memory; never persisted |
| Endpoint trailing silence 0.8 s → target 0.5–0.6 s | Directly removes ≈0.2–0.3 s from every turn | Clipping risk — measure before changing |
| Config-driven `compute_type` + `device`; measure `small` on CUDA float16 vs CPU int8 | STT is 1.2 s today; RTF 0.7 on CPU. CUDA device is visible **[M]** but cuBLAS/cuDNN runtime availability is **[U]** | Must not install into the live venv; owner decision D7 |
| `initial_prompt`/hotwords bias for "V.O.I.D" and configured app names | Better recognition of proper nouns | Verify param support in 1.2.1 **[U]** |
| Keep the min-speech guard | Prevents sliver hallucination reaching the agent | Already implemented |
| Streaming/partial STT, cloud STT | Larger latency win / accuracy | 🟡 later (plan V2.1 STT provider registry) |

---

## 7. TTS strategy

**Reuse:** the `TTS` interface (`speak/stop/is_speaking/close/set_rate`), `_ResilientTTS`, and `register_tts_provider`. Two small changes are needed: the factory must receive config (voice id, model, format), and a chunked/queued contract is needed because `SapiTTS.speak` **replaces** the current utterance (no queue) **[C]**.

**Architecture**
```
response text ─► SpokenPresenter ─► SpeechSegmenter ─► SpeechQueue(generation-tagged, bounded)
                                                          │ per chunk
                              ┌───────────────────────────┴───────────────────────────┐
                        ElevenLabsTTS (HTTP, PCM)                                   SapiTTS (existing)
                              └─────────── FallbackTTS(primary, secondary, breaker) ──┘
                                                          ▼
                                       sounddevice.RawOutputStream (playback thread)
```

**Integration choice — direct HTTP with `requests` + `sounddevice` raw PCM (recommended):**
- **[V] docs today:** `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream`, header `xi-api-key`, body `text`, `model_id`, `voice_settings`, `output_format` (raw `pcm_8000…pcm_48000` available, also MP3/Opus/µ-law), optional `previous_text/next_text` for prosody continuity across chunks.
- **[V]** A WebSocket `stream-input` API exists for text that arrives incrementally, with a buffering `chunk_length_schedule`; the docs themselves say HTTP is better when the full text is available up-front. **V.O.I.D's LLM providers return complete text** [C], so HTTP per sentence-chunk fits; WebSocket becomes relevant only after LLM token streaming (🟡).
- **Dependencies:** `requests` is already declared; `sounddevice`/`numpy` are already voice dependencies; raw PCM avoids needing an MP3 decoder. **Zero new dependencies.** The official SDK would add a dependency tree for no capability we need; `websockets` is installed only transitively (google-genai) and would need declaring if ever used.
- **Model ids are not hard-coded**: pick via config, list with `GET /v1/models` at setup (the streaming page names no low-latency model **[V]**). Time-to-first-audio, pricing and quotas are **[U]** — measure.

**Streaming-safety rules** (all enforced by the queue, not the provider):
1. *No truncation:* the segmenter emits only complete sentences (never splits on abbreviations, decimals, initials, paths, URLs, list numbering); first chunk may be a clause ≥ N chars to cut first-audio latency; the tail is always flushed on end-of-response.
2. *No overlap:* one playback thread, one active stream; chunks play strictly in order; the next chunk is fetched while the current plays (depth 1–2).
3. *No stale audio:* every chunk carries the generation captured at enqueue; the playback thread checks `gen == current` before **every** buffer write; `stop()` sets a cancel event, aborts the output stream, closes any in-flight HTTP response, and empties the queue.
4. *Kill switch / interruption:* both call `stop()` synchronously (existing `TTS_STOP` command); nothing can start after `close()` (existing SAPI semantics preserved).
5. *Fallback:* deadline before first byte (config, e.g. 1.5 s **[E]**) → speak that chunk via SAPI, mark cloud unhealthy for a cool-down (breaker). Mid-chunk failure after audio began → drop the remainder rather than double-speak; subsequent chunks use fallback.
6. *Secrets:* key in the OS store under a **reserved name** (`elevenlabs_api_key`; `set-key` extended; aligns with plan T1.8), never in config/logs/tests/prompts; tests use a local fake server.
7. *Egress policy:* cloud TTS receives **response text**, which may derive from memory. Default: never send text to cloud TTS if the run used a non-`cloud_ok` memory item (D5).

---

## 8. Provider strategy (LLM)

**What exists [C]:** `ProviderRegistry` picks the first available of `llm.primary`/`llm.fallback` **per run**; `GeminiProvider` with `CredentialPool` (ordered named keys in the OS store, 429 cooldown/rotation already implemented); `LocalProvider` (Ollama, qwen3:8b); no deadlines, no thinking control, no role-based model, no health/latency record beyond perf events.

**Key clarification:** multiple API keys for the same provider give **quota and 429 failover, not lower latency**; a 503 ("high demand", seen repeatedly today) is provider-wide and rotating keys does not help. The keys are already supported.

**Smallest strategy that materially helps (no new providers):**
1. Deadlines + error classes on Gemini (plan T2.2) — bounds the worst case (today up to 35.9 s single call **[M]**).
2. **Role-based model selection inside Gemini**: `tool_select`/`final_answer` use a low-latency model and reduced/no thinking; `reason` (open-ended, multi-step) uses the normal model. Which Gemini models and thinking settings are actually faster **must be measured** (plan T2.6 harness) — **[U]**.
3. Local fallback: qwen3:8b with `think:false` — measured 3.3–4.3 s warm, cold load ≈ 83 s **[M, spec §2.3]** (plan T2.3). Warm-keeping is a decision.
4. `ModelRouter` (plan T2.5) with breaker/health and per-call selection replaces per-run first-available and fixes D-07.
5. **Do not add** another cloud LLM until measurement shows a fast Gemini config cannot meet the budget.

Interface sketch (no code): `Route(role, provider, model, deadline_s, retries)`; `ProviderHealth(breaker, recent_latency)`; credentials by *name* only.

---

## 9. Conversational response strategy

Separate **internal structured results** (unchanged, stored, available to follow-ups) from **spoken presentation**.

1. **Voice-mode style:** a channel-aware suffix to the system prompt (voice channel only): short answers, no paths/IDs/URLs unless asked, no markdown, no reading lists longer than three items. (Prompt guidance is a hint; the deterministic layer below is the guarantee.)
2. **`SpokenPresenter` (pure function, new)** between result and TTS: strips markdown/code fences; replaces absolute paths, URLs, GUID/hex ids and long numerics by short spoken forms ("in your Projects folder", "a link"); truncates enumerations with a count ("and three more"); caps length and offers "want more detail?" for long content. **`allow_details=True`** when the *user's utterance* deterministically asks for it (`where`, `path`, `location`, `full address`, `read it out`) — then "It's located at C:\V.O.I.D."
3. **Engine-owned acknowledgements** for fast-path and status outcomes (templates/constants); `ToolResult.summary` stays LLM-facing.
4. **Session context** (in RAM, bounded ≈ last 6 turns / ~1.5 k tokens, cleared on standby/shutdown, never persisted): user text, spoken reply, and a minimal tool ledger (tool names + presenter-safe summaries) so "Tell me more about V.O.I.D" and "Where is it located?" work. Context is **untrusted data, tainted like tool output, and never carries an approval** (a spoken "yes" is just another goal — existing rule).
5. Nothing is deleted from internal state to make speech shorter.

---

## 10. UI strategy (minimal)

Today: tray indicator (state→colour/tooltip), `VoiceStateBridge` (signals only), developer orb/presenter mapping `VoiceState→VisualMode` [C]. Tests exist for tray, orb, singularity.

- Add a **pure** `ui_state_for(mode, session_state, mic_status)` beside `style_for_state` → six user-facing states: **STANDBY** (dim ring, blue-grey), **ACTIVE** (steady ring, cyan), **LISTENING** (bright, pulsing), **THINKING** (rotating arc, amber/violet), **SPEAKING** (green), **ERROR** (red; includes mic unavailable). Existing `stopped/closed/mic_*` keep their styles.
- Transport: emit synthetic state strings `standby` / `active` through the existing `on_state` channel — **no new bridge, no new widget, no dashboard**.
- Tooltip states the truth in plain words ("V.O.I.D — active, just talk", "standby — say 'Hey V.O.I.D.'").
- Developer orb: map `active` → calm LISTENING, `standby` → SLEEPING (no visual redesign).
- Tests: extend `test_tray_indicator.py`, `test_orb.py` with the new states; a completeness test asserting every reachable (mode, state) pair has a style.

---

## 11. Security, privacy and continuous listening

### 11.1 Places the new architecture could bypass a control, and the mitigation

| Risk | Where | Mitigation (enforced by test) |
|---|---|---|
| Fast path used as an authorization shortcut | `fast_path` → tool | Emits only an **ordinary call to `Agent._run_call`** via `run_direct`; static test: no `RiskGate`/tool imports; parity test vs. LLM route (same risk, same denial). LOW-only; else falls through |
| Spoken text becomes a path/URL/command | grammar | Args come only from engine catalog/alias/URL tables; free-form paths/URLs never from speech |
| "Delete this file" becomes deterministic | grammar | Deny-list + non-LOW ⇒ LLM route ⇒ RiskGate ⇒ `awaiting_confirmation`; voice announces the constant phrase and cannot approve |
| Conversational "yes" approves a pending action | session context | Approvals stay CLI-only (`owner_decision`); context never carries approval; explicit test (voice-channel "yes" after an `AWAITING` task changes nothing) |
| Standby phrase spoofed by tool output, memory, TTS echo | router | Matches **whole transcript only**, from the voice channel; half-duplex + drain prevents self-trigger; standby has **no authority** beyond voice mode and cannot touch the kill switch/RiskGate |
| Kill switch ignored while conversing | speaking / active | Kill switch latch remains authoritative in `_apply` from any state; TTS stopped; mode forced STANDBY on re-arm. **Spoken stop is not wired today (P10)** → D2 |
| Memory boundaries | context/fast path | Session context is RAM-only data; memory writes by voice remain `proposed`; recall route and taint unchanged; fast path never reads memory |
| Stale audio/response after interruption/kill | TTS queue | Generation-tagged chunks; per-write generation check; `stop()` aborts stream and HTTP |
| Filesystem protections | fast path folders | Alias-table folders only; `open_path` goes through the same file-layer confinement; D-04/D-04b remain open (P1 T1.1) — **do not broaden file authority before T1.1** |
| Cloud TTS exfiltrates memory-derived text | TTS egress | Cloud only for responses that used no non-`cloud_ok` memory (D5); presenter runs *before* egress |
| Secrets | ElevenLabs key | OS store, reserved name, never in config/logs/tests; content-free telemetry retained |

### 11.2 What stays local vs. goes to the cloud (defined)

| Data | Location | When |
|---|---|---|
| Raw microphone audio, pre-roll ring buffer, VAD, wake detection, endpointing | **Local, RAM only**, bounded, never persisted or uploaded (no cloud STT in MVP) | continuously in ACTIVE (VAD), STANDBY (wake) |
| STT | **Local** | starts only after VAD-confirmed speech ≥ `min_speech_ms` and an endpoint |
| Standby/fast-path/memory-command routing, presenter, segmenter | **Local** | before any cloud call |
| Transcript text → cloud LLM | **Cloud (Gemini)** | only if the router chose the LLM route: goal + fenced memory (`cloud_ok`) + bounded session context + tool schemas |
| Response text → cloud TTS | **Cloud (ElevenLabs)** | only when enabled, healthy, and policy allows (D5); else SAPI locally |

### 11.3 Always-on risks and guardrails
- **Background speech becoming commands** is the main new risk (no wake gating in ACTIVE): Silero VAD + min-speech guard + max utterance length; per-session **LLM-dispatch rate limit** (e.g. > N routed-to-LLM utterances/minute ⇒ spoken warning and auto-STANDBY **[E]**); visible ACTIVE state; instant standby/kill.
- **Cost control:** the same limiter; fast-path/standby/memory routes cost nothing.
- **Echo/feedback:** half-duplex default (detector ignores frames while SPEAKING + hangover + drain). Voice barge-in is *experimental, headset-only* (D8) because laptop speakers→mic echo needs acoustic echo cancellation (new dependency, 🟡).
- **Session lifetime:** owner requires no automatic end on silence; a configurable inactivity standby (`voice.conversation.idle_standby_min`, default **0 = never**) is offered as a safety option (D1).

---

## 12. Offline / online strategy

| Capability | Offline | Online |
|---|---|---|
| Wake, VAD, capture, endpointing | local ✔ | — |
| STT | local faster-whisper ✔ | (🟡) cloud provider via STT registry |
| Standby, memory commands, **fast path (apps/sites-as-default-handler/folders)** | ✔ deterministic | — |
| Reasoning | local Ollama qwen3:8b (`think:false`; cold load ≈ 83 s **[M]** — warm-keep is a decision) | Gemini via router |
| TTS | SAPI ✔ | ElevenLabs |
| Degradation | constant spoken notice ("I'm offline; I can open apps but not answer questions") — plan T2.7 | — |

**Interfaces required** (not built now): `STTProvider` (registry, plan V2.1), `TTSProvider` (+ `FallbackTTS`), `ModelRouter`/`ProviderHealth`, `ConnectivityMonitor` (plan T2.4), `IntentRouter`. Broad offline fallback is out of scope for the MVP; the router falling through to "unavailable" with a constant phrase is in.

---

## 13. Latency strategy (targets — **not** measurements)

| Segment | Today | Target | Lever |
|---|---|---|---|
| Wake → "Yes?" | wake trigger ≈ 0.45 s into phrase [M harness] | ≤ ~0.6 s after phrase end **[E]** | pre-synthesised local ack |
| Endpoint detect (trailing silence) | 0.8 s (+0.4 s lead grace) [C] | 0.5–0.6 s **[E]** | Silero VAD, measured clipping |
| STT | 1.2 s (1.65 s audio, CPU) [M] | ≤ 0.4–0.6 s GPU / ≤ 1.0 s CPU **[E]** | CUDA measurement; hotwords |
| Intent routing | — | ≤ 20 ms **[E]** | pure table matching |
| Deterministic execution | 0.06 s tool [M] | ≤ 0.2 s | existing `launch_app` |
| **Fast-path perceived (endpoint → first spoken)** | ≈ 14.6 s (LLM path) [M] | **≲ 1.5–2 s [E]** | skip LLM + local ack |
| LLM (tool select / short answer) | 2.9–12.4 s [M] | ≲ 3–4 s p50, hard deadline 8 s [E] (plan placeholders 8 s / 12 s) | fast model role, thinking off, deadlines, breaker |
| First TTS audio | SAPI ≈ 0.09 s [M] | SAPI ≤ 0.15 s; cloud ≤ 0.5 s **[U]** | first-chunk short, prefetch |
| **LLM-path perceived (endpoint → first spoken)** | 14.56 s [M, n=1] | **≲ 5–6 s p50, ≤ 12 s p95 [E]** | all of the above + ack-first "one moment" when the route is LLM |

Ack-first: an instant local earcon/short phrase when the router picks the LLM route removes the *silent* wait even when the answer takes seconds. Targets are placeholders to be replaced by the T2.6 measurement harness; no figure above is a guarantee.

---

## 14. File / component change list

| Component | Action | Reason |
|---|---|---|
| `void/voice/state.py` (`reduce_voice`) | **Keep**; at most one additive activation-source label; **no new states** | Mode is orthogonal; 100 % of existing reducer tests stay valid |
| `void/voice/session.py` | **Modify**: `source="open_mic"`; standby pre-router before `assistant.run`; speak via `SpeechQueue`; presenter hook | Same generation/stale-drop machinery |
| `void/voice/runtime.py` | **Modify**: `ConversationMode`, open-mic detector arm/disarm, half-duplex, wake ignored while ACTIVE, safety valve | Reuses monitor/reconcile pattern |
| `void/voice/adapters.py` | **Modify**: STT `device/compute_type` from config; optional chunked-speech contract on `TTS` | Small |
| `void/voice/vad.py` | **Add**: Silero wrapper, pre-roll ring, `SpeechStartDetector` | Always-on gating |
| `void/voice/router.py` | **Add**: voice-control phrases (standby; kill-phrase per D2) | Pre-LLM, deterministic |
| `void/voice/presenter.py`, `void/voice/speech.py` | **Add**: `SpokenPresenter`, `SpeechSegmenter`, `SpeechQueue` | Separation of internal vs spoken |
| `void/voice/tts.py` | **Modify**: config-aware factory, `FallbackTTS`, registry entry | Existing seam |
| `void/voice/tts_elevenlabs.py` | **Add**: HTTP PCM streaming provider | §7 |
| `void/core/fast_path.py` | **Add** (pure) | §5 |
| `void/core/agent.py` | **Modify**: `run_direct`, `context` parameter, voice-style suffix, router hook | Plan already lists these |
| `void/core/session_context.py` | **Add**: bounded in-RAM conversation context | P3 |
| `void/app.py` | **Modify**: route order (voice control → memory → fast path → LLM), channel plumbing | Existing hook |
| `void/providers/*` | **Modify** per plan T2.1–T2.5 (deadlines, roles, router) | P8 |
| `void/security/secrets.py`, `cli.py` | **Modify**: reserved `elevenlabs_api_key`, `set-key elevenlabs` | Secret hygiene |
| `void/voice/status_phrases.py` | **Modify**: new constants (yes/standby/one-moment/offline) | Constant phrases only |
| `void/ui/tray_indicator.py`, `orb.py` | **Modify**: new states | §10 |
| `config/default_config.yaml` | **Modify**: additive, all new features **off by default** | Rollback |
| `void/core/kill_switch.py` | Keep; **decide** call site for `handle_command` (D2) | P10 |

Unchanged on purpose: `RiskGate`, tool implementations, `CredentialPool` semantics, memory subsystem, capture broker, wake detector.

---

## 15. Dependencies

**None required for the MVP.** Already present: `requests`, `sounddevice`, `numpy`, `onnxruntime`, `faster-whisper` (Silero VAD asset), `PySide6`. Optional/later and *not recommended now*: `websockets` (transitive today; declare if WebSocket TTS is ever adopted), official ElevenLabs SDK (no capability gain), an AEC library for full-duplex barge-in (🟡, new native dependency). Any addition follows the existing policy: declared in `requirements*.txt`, pinned in `requirements.lock`, installed only after owner approval, never into the live venv during development.

---

## 16. Test strategy

- **State machine:** property tests over (mode × session state × event) — silence never changes mode; stale generation never changes state/mode/speech; kill switch and shutdown win from any state; existing 1,468-test baseline unchanged (guard).
- **VAD/endpointing:** reuse the SAPI corpus from the audio-guard calibration (22 legitimate clips, slivers, silence, noise, quiet speaker) — pass/fail thresholds recorded; seeded random noise; pre-roll correctness; no audio persisted (temp-dir scan test).
- **Fast path:** table tests (positive, negative, ambiguous, STT-noisy variants, compound, deny-list); engine parity vs. LLM route; static import test; flag-off = V1; **0 wrong-app launches** gate.
- **Router:** exact standby matching (embedded in longer text ⇒ not standby); kill phrase per D2; approvals cannot be spoken.
- **Presenter/segmenter:** golden tables (paths, URLs, GUIDs, markdown, lists, abbreviations/decimals, "where is it?" allow-details); no-truncation property (concatenated chunks == presented text).
- **TTS:** fake HTTP server (chunk timing, mid-stream failure, stall, 401/429); queue race tests (stop during fetch, stop during write, generation flip); fallback/breaker; kill-switch-during-speech; no key in logs/exceptions (canary secret).
- **UI:** pure-mapping completeness; tray/orb tests extended.
- **End-to-end:** the **WAV-feeder real-launcher harness** built during validation becomes a reusable script (real Gen3 wake, real Whisper, real memory, fake/real provider) for multi-turn scenarios incl. standby; plus **owner-attended physical checklist** (real mic) — labelled separately, never conflated with synthetic evidence.
- **Performance:** latency harness reporting p50/p95 per stage with content-free telemetry; target table above becomes acceptance thresholds only after measurement.
- **Security regression:** adversarial memory + obedient-provider run (existing method) executed through the fast-path and conversational routes.

---

## 17. Migration strategy

Additive config, every new capability behind a flag defaulting to **V1 behaviour**: `voice.conversation.enabled=false` (wake-per-command as today), `fast_path.enabled=false`, `voice.presenter.enabled=false`, `voice.tts_provider=sapi`, `llm.router.enabled=false`. No database/schema change (session context is RAM; task store unchanged). Roll out one flag at a time on the owner's runtime after that phase's gate; run the WAV-feeder harness before touching the live scheduled task; the live runtime is restarted only by the owner-approved mechanism.

## 18. Rollback strategy

Per-flag: set the flag false and restart the runtime (V1 path is the default code path and stays covered by the baseline suite). Per-phase: separate commits, revertable without touching earlier phases; pre-phase git tag. TTS: `voice.tts_provider: sapi` (and the fallback already covers cloud failure). Fast path: flag off ⇒ every utterance takes the V1 LLM route. Conversation mode: flag off ⇒ wake-per-command.

---

## 19. Scope classification

**🟢 IN SCOPE (MVP):** persistent conversational session with explicit standby · VAD-based utterance endpointing independent of session lifetime · half-duplex turn-taking with hangover/drain · fast deterministic routing for open/launch app/site/folder (flagged) · session context (RAM) · spoken presenter + voice-style prompt · TTS provider interface with segmenter/queue/cancellation + SAPI adapter + ElevenLabs HTTP provider + fallback · role-based Gemini model + deadlines + router slice (plan T2.1–T2.5) · minimal six-state UI · interruption/cancellation infrastructure with PTT barge-in and standby-during-speech · wiring of the spoken stop phrase (per D2) · measurement harness.

**🟡 LATER:** voice (acoustic) barge-in on speakers/AEC · streaming/partial STT · cloud STT provider · WebSocket/LLM-token streaming TTS · emotional/prosodic voice control · richer multi-turn reasoning/planning · time/status intents · "open X in browser Y" capability (needs URL policy T1.5 + a validated launch-with-argument tool) · phone voice continuity · GPU STT if measurement is inconclusive · warm-keeping the local model.

**🔴 DO NOT BUILD YET / OUT OF SCOPE:** arbitrary shell/PowerShell execution or free-form commands from speech · any change to `RiskGate`/kill switch semantics · voice-granted approvals · new cloud LLM providers "for variety" · vector/embedding memory · persistent raw-audio storage · always-on cloud STT · camera/screen/OCR · multi-agent orchestration · autonomous background operation · broad offline fallback beyond deterministic commands + local model.

---

## 20. Implementation phases

Ordering rationale: the owner's biggest pain is the 14–30 s wait on simple commands (fast path + acks), then session behaviour, then presentation and voice quality. The plan's security-first ordering (P1 T1.1 before broadening capability) is respected as a recommended gate, not a hard dependency, because the fast path adds **no new authority**.

| Phase | Deliverable | Depends on | Size | Acceptance |
|---|---|---|---|---|
| **V0** | Owner decisions D1–D12; optionally **P1 T1.1** (protected roots) first | — | S | decisions recorded |
| **V1** | **Measure before building**: reusable WAV-feeder E2E harness in `scripts/`; STT CPU vs CUDA; endpoint tuning candidates; Gemini model/thinking matrix (plan T2.6 slice); owner-attended real-mic baseline | — | M | latency table with p50/p95, real vs synthetic labelled |
| **V2** | **Fast path + ack** (`fast_path.py`, `run_direct`, alias table, templates, telemetry, eval) — flag off | V1 data, `_run_call` funnel | M | parity + 0 wrong-app launches; flag-off = V1 |
| **V3** | **Presenter + session context + voice-style prompt** | — | M | golden tables; "where is it located?" works; approvals never in context |
| **V4** | **Conversation mode**: Silero VAD/pre-roll/open-mic detector, `ConversationMode`, half-duplex, standby route, safety valve, UI states — flag off | V1 (VAD calibration) | L | scripted multi-turn harness passes; silence never ends session; physical check attended |
| **V5** | **TTS abstraction**: config factory, segmenter/`SpeechQueue`, SAPI adapter, ElevenLabs HTTP provider, fallback/breaker, key handling, egress policy | D5/D6 | L | race/stale/cancel tests; no key leakage; measured first-audio |
| **V6** | **Provider routing slice**: Gemini deadlines + role models + `ModelRouter` (plan T2.1–T2.5), degraded notices | V1 matrix | L | fault-injection table; D-07 xfail passes |
| **V7** | **Interruption**: PTT barge-in over queue, standby-during-speech, kill-during-speech; experimental voice barge-in (headset only, flag) | V4, V5 | M | cancellation tests; no speech after kill |
| **V8** | Enable flags on the live runtime one at a time; owner-attended acceptance | all | S | owner sign-off per flag |

V2 and V3 are independent and can be parallel; V6 can run early if LLM latency is the priority.

---

## 21. Open decisions for the owner

| ID | Decision | Recommendation |
|---|---|---|
| D1 | Should ACTIVE ever end by inactivity? | Default **never** (as requested) with an optional `idle_standby_min` and the flood safety valve on |
| D2 | Wire the spoken kill phrase? (`handle_command` is unused; with a stop PIN set, voice cannot supply the PIN) | Yes, wire "VOID, STOP EVERYTHING" as a pre-LLM route; define behaviour when a PIN is configured (allow voice stop, require the PIN only to *clear*) |
| D3 | May the fast path ship before the CapabilityEngine? | Yes for LOW open/launch only: it runs through the existing single funnel the engine will later delegate from |
| D4 | "Open YouTube in Opera GX" — new launch-with-URL capability? | Defer (🟡) until URL policy (T1.5); MVP opens sites in the default browser; confirm which browser is default |
| D5 | Cloud TTS egress of memory-derived text | Local SAPI for any response that used non-`cloud_ok` memory |
| D6 | ElevenLabs account, voice, model, cost ceiling | Owner supplies; model chosen from `GET /v1/models` + measurement |
| D7 | GPU STT (needs CUDA runtime libraries in the venv) | Measure first; install only with approval, never into the live venv during dev |
| D8 | Voice barge-in on laptop speakers | Not in MVP; allow headset-only experimental flag |
| D9 | What may enter session context (tool outputs? how many turns?) | Presenter-safe summaries only, ~6 turns, cleared on standby |
| D10 | Where the alias table lives | `config/local_config.yaml` (machine-specific, gitignored) |
| D11 | Development location: new branch vs `main` (old worktree is gone) | Small feature branches per phase, merged fast-forward after each gate |
| D12 | Ordering vs G0/P1 | Owner review of G0 first; T1.1 before V2 if broadening file authority; otherwise V1→V2 may start now |
| D13 | Wake phrase while ACTIVE | Ignore (or short ack); never restarts state |
| D14 | Ack wording/personality | Owner picks a short list; constants only |
| D15 | Endpoint silence target and acceptable clipping | Decide after V1 measurement |

---

## 22. Evidence limits (honest scope of this document)

- Latency numbers are from small samples on this laptop (n = 1 real interaction; synthetic-audio runs are labelled and never counted as physical-mic evidence). Targets are targets.
- ElevenLabs statements come from the docs fetched today; latency, pricing, model names and quotas were **not** verified.
- Silero streaming usage, faster-whisper hotword support, CUDA runtime availability and Gemini fast-model performance are **[U]** and assigned to phase V1 to measure.
- No code, config, credential, task row or Windows setting was changed while preparing this proposal.
