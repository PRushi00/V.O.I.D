# Prompt-pack requirement ledger

What the seven Jarvis prompt packs asked for, what V.O.I.D does about each one, and the evidence.
The archive inventory, the prompt-sequence map and the architectural reconciliation that this ledger
depends on are in `docs/PROMPT_PACK_SOURCES.md`; read that first, because several statuses below are
"already satisfied by something V.O.I.D does differently" and the reasoning lives there.

Status words mean exactly one thing each, and the same ones `docs/V3_REQUIREMENTS.md` uses:

| Status | Meaning |
| --- | --- |
| **VERIFIED** | Implemented, integrated, tested, and exercised against the real thing |
| **ALREADY_SATISFIED** | V.O.I.D already met the requirement before this pass; evidence named |
| **PARTIAL** | Implemented and tested, but some part is unproven or deliberately narrower |
| **BLOCKED** | Cannot be done here, with the specific missing thing named |
| **DECLINED** | Deliberately not built, with the reason named |
| **INAPPLICABLE** | The requirement is about a mechanism V.O.I.D does not have and should not grow |

Evidence classes, used in the notes: **EXPLICIT** (a pack states it), **INFERRED** (derived from
several pack statements), **UNVERIFIED** (code exists, behaviour not proven), **CONFLICTING** (sources
or the existing architecture disagree).

Sources are cited as `pack/prompt`: `opus/07`, `jev/09`, `phone/script-03`, and so on.

---

## 1. Part One — the shared foundation

| # | Requirement | Source | Status | Where, and the evidence |
| --- | --- | --- | --- | --- |
| P1.1 | Index a folder of markdown notes; a node per note with a title, its folder as a group, a ~700-character excerpt, and links by title mention or `[[wikilink]]` | free/1, fable/01, opus/01, astra/01, jev/01, rea/01 | **VERIFIED** | `void/vault/__init__.py`; 40 tests in `tests/test_vault.py`; probed live by `void preflight` ("vault index") |
| P1.2 | Every node has a numeric id equal to its index, because later features look notes up by index | opus/01 ("CRITICAL") | **VERIFIED** | `Note.id`; ids follow sorted relative paths so two machines agree, and a capture appends rather than renumbering — `test_ids_are_the_index_...`, `test_the_numbering_does_not_depend_on_the_order_...` |
| P1.3 | Answer a question from the notes: score every note, weight title matches higher, take the top 6, return the ids used | all Part Ones | **VERIFIED** | `Vault.search`, `TOP_K=6`, `TITLE_WEIGHT=2.5`; reuses the existing BM25 in `void/memory/index.py` rather than adding a second scoring engine (see SOURCES §4) |
| P1.4 | Say plainly when the notes do not cover it; never answer from a note that merely shares a topic | all Part Ones; jev/08 "not in the vault" | **VERIFIED** | `Verdict.ABSENT` returns **no excerpts at all**, and the tool summary instructs the model accordingly — `test_a_question_the_notes_do_not_cover_is_absent_and_returns_no_excerpts`, `test_the_absent_verdict_reaches_the_model_as_an_instruction_not_as_a_failure`; probed by preflight ("vault honesty") |
| P1.5 | Keys never in a chat window; the owner types them into a file themselves | all packs | **ALREADY_SATISFIED** | Stronger than asked: keys live in the OS credential store (`void/security/secrets.py`), never in a repo file. CONFLICTING with the packs' `config.json`; resolved in favour of V.O.I.D's — SOURCES §4 |
| P1.6 | A server bound to loopback, serving only the viewer folder | opus/01, opus/18 | **INAPPLICABLE** | V.O.I.D is not a web app. A second unauthenticated HTTP listener would be new attack surface for no capability gain; the authenticated device gateway (`void/device/gateway.py`) is the one listener and was audited instead (§5 below) |
| P1.7 | Voice both directions, with a buffered finish window, interrupt words that bypass the buffer, and a status line | free/3, fable/02, opus/02, astra/02, jev/02, rea/02 | **ALREADY_SATISFIED** | `void/voice/` — local wake word, Whisper STT, TTS with barge-in, measured endpointing (`docs/VOICE_ENDPOINTING_V5_2026-10-01.md`); 99 voice + 35 interruption tests. The packs' browser Web Speech API is strictly weaker |
| P1.8 | A mute flag so a background tab never speaks, checked in the one function that speaks | opus/02, astra/13 | **ALREADY_SATISFIED** | One speaking path (`void/voice/adapters.py` TTS) with the kill switch and `status_phrases` gating it; there are no tabs |
| P1.9 | Prove where the answer came from: fly to the source node, light the cluster, hold still for small talk | free/4, fable/03, opus/03, astra/03, jev/03, rea/03 | **PARTIAL** | The *capability* — knowing and reporting which notes an answer used — is VERIFIED (`Recall.ids`, `node_ids` in the tool payload). The camera dive is the packs' galaxy UI; V.O.I.D's visual identity is the Singularity renderer (SOURCES §4), so no camera move was built. INFERRED that the citation, not the animation, is the requirement |
| P1.10 | One editable persona block | free/5, fable/04, opus/04, astra/15 | **PARTIAL** | `void/core/agent.py` holds one `SYSTEM_PROMPT` constant, which is the single-block property the packs ask for. V.O.I.D deliberately has no butler persona and no "sir": the engine-authored sentences are plain by design, and inventing a character was out of scope for this pass |
| P1.11 | Grow the notes by voice: write a real markdown file into `captures/`, dated, and make it retrievable **without re-running the indexer** | free/6, fable/05, opus/05, rea/05 | **VERIFIED** | `Vault.capture` writes through the confined file layer and appends to the live index — `test_a_captured_note_is_retrievable_by_the_very_next_question`; probed by preflight ("vault capture"), which captures for real and removes the note |
| P1.12 | Never let a capture silently fail | fable/05, opus/05 | **VERIFIED** | `CaptureResult.ok` false always carries a reason and nothing is added to the index — `test_a_capture_that_cannot_be_written_says_so_instead_of_pretending`, `test_a_failed_capture_reaches_the_model_as_a_failure` |
| P1.13 | "remember that …" is the capture trigger phrase | free/6, opus/05 | **CONFLICTING → kept as is** | That phrase is already bound, with tests, to V.O.I.D's encrypted owner-reviewed memory (`void/memory/`), which is the stronger store for a fact about the owner. The vault gets a tool (`write_note`) the model calls when asked to write a note. Changing the phrase's meaning would break a tested, shipped behaviour for no gain |
| P1.14 | Sight: share a screen, capture one frame **at the moment of asking**, answer about what is on it, and say when the share has ended | fable/06, opus/06, astra/14 | **ALREADY_SATISFIED** | `void/perception/screen.py` (native GDI capture, 1707×1067 in 24 ms) + `void/actions/screen.py`; separate `screen.enabled` / `screen.allow_cloud_analysis` switches and `ProviderPolicy` at egress. Captures on demand, so there is no cached-frame failure mode to guard |
| P1.15 | **The preflight harness**: run every LIVE chain end to end, a tick or a cross per check, one summary line `N pass, N fail, N warn`, non-zero exit on any failure | fable/07, opus/07, jev/05, rea/06 — all four call it "the one that matters" | **VERIFIED** | `void/preflight/__init__.py`, `python -m void preflight`; 16 probes; 28 tests in `tests/test_preflight.py`. Ran for real: **16 pass, 0 fail, 4 warn** on a configured installation. It found a real defect on its first run — see §4 |
| P1.16 | Preflight asks the Models API for the configured model **by id**, and fails with the id in the message | opus/07, rea/06 | **VERIFIED (code) / BLOCKED (live)** | `_check_model_is_reachable` calls `client.models.get(model=…)` through the provider's own credential pool. Not exercised live here: no Gemini credential in this container, so the probe correctly WARNs |
| P1.17 | Preflight proves the served files match the files on disk | fable/07, opus/07 | **VERIFIED** | Translated to V.O.I.D's version of the same failure: a second copy of the `void` package earlier on `sys.path`, so edits change nothing — `_check_source_is_what_runs`, `test_a_second_copy_of_the_package_on_the_path_would_be_caught` |
| P1.18 | Preflight proves the secrets file is not reachable, and this check must fail loudly | fable/07, opus/07, opus/18 | **VERIFIED** | Three separate probes, because each has a different fix: `local_config` is gitignored, no key-shaped value is in configuration, and every state file is owner-only. The third one failed on the first real run |
| P1.19 | Swap the model at runtime by voice; a restart returns to the configured model | fable/08, opus/08, astra/15 | **VERIFIED** | `void/providers/catalog.py` + `Assistant._brain_command`; nothing is written down, so a restart reverts — `test_nothing_about_a_swap_is_written_down` |
| P1.20 | **The refusal rule**: keep an explicit set of real ids; a near miss REFUSES and names what is available; never fall back to the nearest match | fable/08, opus/08 ("and I mean this one") | **VERIFIED** | `ModelCatalog.resolve`; the known set is built from configuration, never from a shipped catalogue of other companies' model names, so no id is in it because something guessed. 59 tests in `tests/test_model_catalog.py`, including every near-miss form; probed by preflight ("model refusal") |
| P1.21 | Adding a model is one line | opus/08 | **VERIFIED** | `llm.known_models: {gemini: ["gemini-3.6-flash"]}` in `config/local_config.yaml` — `test_adding_a_verified_model_is_one_line_of_configuration` |
| P1.22 | Show the current brain on screen; read "claude-opus-5-5" as "Opus 5.5" | opus/08 | **PARTIAL** | "which model are you using" answers with the exact id and provider. No chip was added: V.O.I.D's surfaces are the orb and the tray, and a pretty-name renderer for ids this installation does not have would be decoration |

## 2. Opus pack Part Two — telephony and messaging

Every requirement in this section needs an account, a credential, a phone or a tunnel that only the
owner can create, and in two cases the network path is refused by this container's proxy (SOURCES
§5). None was faked, and no unverifiable network client was written: shipping a Retell or Telegram
client that has never completed one request would be "implemented but unverified" code claiming a
capability, which is the failure mode the packs' own receipts rule exists to prevent.

| # | Requirement | Source | Status | What is needed to unblock it |
| --- | --- | --- | --- | --- |
| P2.1 | A phone line of its own; `setup_phone.py` idempotent with `--dry`; `call_me(headline, notes)`; poll for call end rather than trusting a webhook | opus/09 | **BLOCKED** | A Retell AI account, a purchased number, a cell number to call, and network access to `api.retellai.com` (the proxy refuses the tunnel here, 403) |
| P2.2 | Google calendar and mail, drafts only, OAuth with PKCE, refresh token chmod 600, tool loop capped at six steps | opus/10 | **BLOCKED** | A Google Cloud project, an OAuth consent screen in production, and a client id/secret the owner creates. Note the drafts-only rule matches V.O.I.D's existing stance exactly: `void/actions/messaging.py` refuses a control that would act rather than open |
| P2.3 | Outside content is data: a tool result may never be an instruction, and a turn that read mail may not then delete anything | opus/10 | **ALREADY_SATISFIED** | `Agent._untrusted()` frames every tool result before a model sees it; `void/memory/scope.py` taints a run that has seen tool output so memory proposed from it is quarantined |
| P2.4 | Receipts: only claim an action happened when a tool result confirms it | opus/10, opus/14, phone/security-7 | **VERIFIED** | This is the one requirement in Part Two that needed no external service, and it is now `void/orchestration/judge.py` — §3 below |
| P2.5 | Telegram bot, pairing code minted only at the desk, ten-minute life, single use, five wrong guesses and it dies; voice notes transcribed; one push per card | opus/11 | **BLOCKED** | A BotFather token and the owner's chat id. The pairing discipline asked for here is the same shape as `void/device/pairing.py`, which already implements a short-lived single-use window with attempt limits |
| P2.6 | "Call me" is not "call me if": standing orders arm an urgent line and never dial; the dial route has its own refusal pattern; urgent means once with a cooldown | opus/12 | **INAPPLICABLE while P2.1 is blocked** | There is no dial route to gate. The transferable discipline — classify a control intent deterministically, before any model, and make the destructive reading require explicit words — is already V.O.I.D's, in `void/orchestration/commands.py` |
| P2.7 | The busy opener: listen first, apologise in one breath, headline then two facts, never ask "is now a good time", end the call in the same breath as the sign-off | opus/13 | **BLOCKED** | Depends on P2.1 |
| P2.8 | After a call, extract only the owner's instructions as strict JSON; a check and the action it gates are ONE instruction; one confirmation, delivered once | opus/14 | **BLOCKED** | Depends on P2.1 |
| P2.9 | Reminder calls and a morning briefing that survive a restart | opus/15 | **PARTIAL** | The scheduling half exists and is V.O.I.D's own: `void/maintenance/schedule.py` claims its week in the state database so a second pass cannot happen, and `void/runtime/scheduled_task.py` registers with Task Scheduler. The *call* is blocked on P2.1 |
| P2.10 | An inbound hotline; caller ID is a hint and never proof; the code is checked by the server and never appears in the agent's prompt; constant-time compare; lockout after three misses; strangers get a receptionist and reach nothing | opus/16, phone/script-03 | **BLOCKED (telephony)** / **ALREADY_SATISFIED (the principle)** | `void/device/auth.py` and `void/device/pairing.py` already do server-side constant-time verification with attempt lockout, and `void/a2a/__init__.py` is the untrusted-caller boundary that decides and returns but never executes. "Scope by architecture, not by promise" is the rule V.O.I.D is built on |
| P2.11 | Stage an urgent call for the camera; be honest that it is staged; a dry flag in preflight | opus/17 | **BLOCKED** | Depends on P2.1 |
| P2.12 | Security pass: loopback only, Host and Origin checks, body caps, nosniff, no framing, secrets 600, untrusted content rendered as text, pinned CDN versions, a confirmation covers one action | opus/18 | **VERIFIED, as an audit** | §5 below. The one finding with a code change is the state-file permissions defect in §4 |

## 3. Jev pack Part Two — the reflex layer

| # | Requirement | Source | Status | Note |
| --- | --- | --- | --- | --- |
| P3.1 | A reflex bay over TypeSafe AI's "Jev" decision model; read `docs.typesafe.ai/llms.txt` **before writing a line**; `ask()` never raises and falls back to the previous rule; every call logged with its full answer distributions | jev/06 | **BLOCKED** | Both `docs.typesafe.ai` and `api.typesafe.ai` are refused by this container's proxy (403 on CONNECT), so the live documentation the prompt requires cannot be read and the API cannot be reached. Writing a client against a remembered wire format would be inventing an endpoint, which is the one thing the prompt is most insistent about not doing. Needs: network access to those hosts, and a TypeSafe key |
| P3.2 | A prepass over every utterance — lane, complete, addressed, stakes, urgent — with named gates, and a live strip showing each probability | jev/07 | **BLOCKED** | Depends on P3.1. V.O.I.D's deterministic equivalents already exist for two of the five: "lane" is `void/orchestration/routes.py` + `void/core/fast_path.py`, and "complete" is the measured endpointing in `void/voice/` |
| P3.3 | The vault check: does the vault *directly* contain the answer; FOUND / ABSENT / UNSURE / NAMED with named gates; phonetic matching so a misheard note name still finds its note | jev/08 | **VERIFIED, deterministically** | Built without Jev. `Vault.search` returns exactly those four verdicts from retrieval evidence, and the gates (`COVERAGE_FOUND`, `COVERAGE_ABSENT`, `KEEP_RATIO`) are named constants whose docstring says plainly that they are **evidence ratios, not probabilities from a judge model** — a made-up probability would have been worse than an honest ratio. `phonetic_key` folds exactly what the pack names, plus dropping a non-initial "h" so that its own example ("school" for "Skool") works |
| P3.4 | The judge: before any spoken answer, block a claim of a completed action that no receipt supports, and say one honest line instead | jev/09 | **VERIFIED, deterministically** | `void/orchestration/judge.py`, gated into `Agent._loop`. Needs no decision model: V.O.I.D already has the authoritative record of which tools ran, written by the single execution funnel. 59 tests; probed by preflight ("receipt judge") |
| P3.5 | The judge demo: run a canned fabricated line through the real judge, labelled as such, because a good brain will not fabricate on request | jev/09 | **VERIFIED** | That is precisely what `_check_judge` does in preflight, and what `tests/test_judge.py` does 16 times over |
| P3.6 | Filming mode: wake word off, answer only when clearly addressed, with a drill over twelve sentences | jev/10 | **BLOCKED** | The "addressed" and "narrating" signals are Jev calls (P3.1). V.O.I.D's wake word is local and already has a standby phrase; a deterministic addressed-detector was not invented, because the pack's own numbers are the evidence for its gates and those numbers come from the service |
| P3.7 | Desk triage and a shadow governor that logs its disagreement with the existing rule for a week before it decides anything | jev/11 | **BLOCKED** | Depends on P3.1. The shadow-first discipline is worth keeping in mind for any future scorer |
| P3.8 | A field drill and a log watcher that print one line per call in English | jev/12 | **PARTIAL** | `void preflight` is the drill for the chains that exist, and `void perf report` already reads the structured log. Nothing reflex-specific to watch while P3.1 is blocked |

## 4. The defect this work found

`void preflight`, on its first real run against a live installation, reported:

```
[FAIL] state file permissions  readable by other users: tasks.sqlite is 0o644 - chmod 600 them
```

That was true, and it mattered. `~/.void/tasks.sqlite` holds task goals, the owner's own sentences,
model answers and tool output **in plain text**, and the only `chmod` anywhere in the codebase was
the one `void/device/cert.py` applies to its TLS private key. Every one of the 4 255 tests passed
while it was wrong, which is exactly the class of failure the packs built the harness for.

Fixed in `void/security/ownership.py` and applied at every point V.O.I.D creates its own state: the
state directory (0700), the task database, the structured state database, the memory database and its
directory, the performance log and the stop file (0600). Applied **at boot as well as at creation**,
because an installation that predates the fix already has a 0644 file on disk and only re-applying
the mode repairs it. Best effort and never fatal; on Windows it reports `not_applicable` rather than
pretending, because mode bits do not describe access on NTFS.

Covered by `tests/test_preflight.py::test_the_state_directory_and_its_files_are_created_owner_only`,
`::test_an_installation_that_is_already_wrong_is_fixed_at_boot`, and
`::test_windows_is_not_pretended_about`.

## 5. The Opus 18 security audit, line by line

`opus/18` is an audit prompt, so it was run as an audit against the surfaces V.O.I.D actually has —
the device gateway, the MCP server, the file layer and the credential store — rather than against a
web server it does not have.

| Rule | Finding |
| --- | --- |
| Bind loopback only | **PASS.** `void/device/gateway.py` binds the configured host (default loopback) and is off by default; `device.enabled` gates it |
| Refuse a foreign `Host` header (DNS rebinding) | **PASS.** The gateway authenticates every request with a paired device secret; an unauthenticated request reaches no capability regardless of Host. `tests/test_device_gateway.py` and `tests/test_gateway_availability.py` cover the refusals |
| Require our own `Origin` on every POST | **N/A.** The gateway is not a browser surface and sets no cookies, so there is no ambient credential for a cross-site POST to ride on; the device secret is the credential and a foreign page does not have it |
| Cap request bodies, set socket timeouts | **PASS.** `_BoundedServer` caps concurrent handler threads and the gateway caps bodies — see `void/device/gateway.py` |
| Serve only known file types, never backups | **N/A.** The gateway serves no static files |
| Secrets and state owner-only, at boot and after every write | **FIXED.** §4 |
| `.gitignore` covers `config.json*` rather than `config.json` | **PASS,** for the equivalent file: `local_config*` is ignored, and preflight now checks it |
| Nothing from outside is code | **PASS.** `Agent._untrusted()` on every tool result; `void/voice/status_phrases.py` speaks fixed phrases for failure statuses so untrusted text is never read aloud; `AgentResult.engine_authored` is the narrow exception and is set only where every word is engine-owned |
| Pin an exact version of every dependency | **PASS.** `requirements.lock`, and `requirements/v3-*.txt` pin exact versions; `tests/test_v3_interop.py::test_the_dependency_manifests_match_what_is_installed` enforces it |
| A confirmation is narrow: an agent path after "do it" still gets no shell, no delegation, no money tools | **PASS.** There is no shell tool at all (`tests/test_memory_is_not_authority.py` asserts its absence by name), no payment capability, and `void/security/consequential.py` draws the confirmation boundary |
| Nothing a stranger says reaches the owner's notes | **PASS.** `void/a2a/__init__.py` decides and returns but never executes; a tainted run's memory proposals are quarantined |

## 6. REA pack Part Two — website teardown

| # | Requirement | Source | Status | Note |
| --- | --- | --- | --- | --- |
| P4.1 | Install `rea-agents` outside the project, pinned, `--ignore-scripts`, after confirming the repository URL | rea/07 | **DECLINED for this pass** | Not blocked by access: the package is real (`rea-agents@6.7.0`, MIT, `repository` resolves to `github.com/morluto/rea` exactly as the pack says to verify) and Node v22.22.0 is present. Declined because the published version is five major versions beyond the one the pack's commands describe (`list-browser-targets`, `inspect-web-page`, `analyze-web-bundle`), so the integration would have to be designed against whatever 6.7.0 actually exposes rather than against the source document — and because V.O.I.D already has a Playwright browser layer and a research engine that would need reconciling with it first. This is the one item in this ledger that is a scope decision rather than a blocker, and it is the obvious next piece of work |
| P4.2 | A brand-new Chrome profile per teardown, loopback debugging port, always closed and deleted in a `finally` | rea/08 | **DECLINED with P4.1** | V.O.I.D's existing browser layer deliberately does the opposite by default — it attaches to the owner's running browser (`void/browser/existing_session.py`) because reusing their real session is the capability. A clean-profile mode would be an addition, not a change |
| P4.3 | REA observes and never drives: no clicks, no typing, no logins, no changes | rea/07, rea/Part-Two laws | **ALREADY_SATISFIED as policy** | `void/browser/safe_url` gates every URL (12 hostile URLs tested) and `_click_risk` puts a consequential click behind confirmation |
| P4.4 | Every claim cites its evidence; guesses are labelled as guesses; keep the "who captured this" attribution straight | rea/09, rea/10 | **ALREADY_SATISFIED** | `void/research/` requires a URL on every `Finding` — there is no unattributed constructor — and the engine returns excerpts rather than summarising |
| P4.5 | The teardown note is born in the galaxy through the same `addNote` Total Recall uses | rea/10 | **PARTIAL** | The mechanism exists and is generic: `Vault.capture` indexes live, so any future teardown writer has one function to call |
| P4.6 | The drill leaves no trace: snapshot, run, restore, delete the test note | rea/12 | **VERIFIED, as a principle applied to preflight** | `void preflight` captures a real note and removes it, and reports loudly if the removal fails — `test_the_probe_note_is_removed_from_the_owners_vault` |

## 7. Astra pack Part Two — focus sessions and the other organs

| # | Requirement | Source | Status | Note |
| --- | --- | --- | --- | --- |
| P5.1 | Focus sessions: a server-side timer, lock the frontmost app and (for a browser) the tab by URL **host**, read the frontmost app with a fresh query every tick | astra/09 | **DECLINED for this pass** | Buildable here in principle — `ComputerActions.get_active_window` already reads the frontmost window — but it is a large subsystem (four of the pack's prompts) that exists in only one of the seven packs, and the core laws the corpus agrees on were the better use of this pass. Named here so it is a decision on the record, not an omission |
| P5.2 | Privacy structurally: identities are compared and discarded inside the reader; the state the client sees is a whitelist of booleans and counters; a test proves no app or tab identity can appear in it | astra/09, astra/12 | **ALREADY_SATISFIED as a pattern** | This is how `void/state/store.py` is already built — it holds knowledge about the computer and refuses to hold secrets — and how the perf log is already written (counts, durations and content-free codes only). The discipline would carry straight into P5.1 |
| P5.3 | Name the distraction out loud and store it nowhere; prove it with a test using a label that appears nowhere else | astra/12 | **DECLINED with P5.1** | The test technique is worth stealing when P5.1 is built: a canary string that appears in no canned text is the only honest way to prove a label was not persisted |
| P5.4 | The deferred lock, the re-target, the card trap | astra/10, astra/11 | **DECLINED with P5.1** | — |
| P5.5 | Camera posture nudges, with frames never leaving the machine | astra/13 | **ALREADY_SATISFIED in substance** | `void/vision/` is deny-by-default with four independent controls and keeps frame statistics local; `camera.allow_cloud_analysis` is a separate decision from having a camera at all |
| P5.6 | The ear law: no organ may turn the microphone on | astra/13 | **ALREADY_SATISFIED** | The microphone is owned by one capture broker (`void/voice/capture_broker.py`); no capability opens it. `tests/test_audio_guard.py` and `tests/test_capture_broker.py` |
| P5.7 | Screen watch: diff a thumbnail locally, think only when something has been still too long | astra/14 | **DECLINED with P5.1** | The capture half exists (`void/perception/screen.py`, 24 ms); the stillness loop does not |
| P5.8 | Instrument before you fix: a diag endpoint of booleans only, a probe page, a whitelisted ledger | astra/16 | **ALREADY_SATISFIED, and extended** | `void doctor` (read-only health), `void perf report` (p50/p95 latency tables) and now `void preflight` (live chains). The "booleans and statuses only, no identities" rule is already how the heartbeat record is written |

## 8. Phone pack

| # | Requirement | Source | Status | Note |
| --- | --- | --- | --- | --- |
| P6.1 | The booking, inquiry and receptionist agent scripts | phone/script-01..03 | **BLOCKED** | They are Retell agent prompts; without P2.1 there is no agent to paste them into. Their *rules* are recorded in SOURCES §3 because several are general: never accept changed terms on the owner's behalf, never give payment details, confirm honestly when asked whether you are an AI |
| P6.2 | Scope by architecture: the inbound agent answers only from a small curated pack and cannot reach the owner's notes, mail or files | phone/script-03 | **ALREADY_SATISFIED as the governing principle** | Named in SOURCES §3 law 5; it is what `void/a2a/__init__.py`, `void/providers/policy.py` and the deny-by-default capability switches implement |
| P6.3 | Deny before the model: refuse identifier and payment questions with a hard-coded check **before** any AI reasons about them | phone/security-4 | **ALREADY_SATISFIED** | `void/orchestration/commands.py` classifies control intents before any model sees the words; `void/security/protected.py` refuses credential stores and vaults at the file layer; `void/security/secretscan.py` scrubs key-shaped strings |
| P6.4 | The invoice engine: strict JSON from the model, **arithmetic recomputed in Python and overriding the model's number**, a numbered ledger, an HTML document printed to PDF, and a delivery that is never claimed unless it happened | phone/04 | **DECLINED for this pass** | V.O.I.D already generates DOCX/PPTX/XLSX/PDF through `void/artifacts/` and inspects every document it writes before reporting success, so the document half has a home. The engine itself is a business feature rather than a capability, the delivery leg is blocked on Telegram, and the one rule in it that generalises — never trust a model's arithmetic — belongs in whatever builds it. Recorded rather than quietly dropped |

## 9. What to check first

A reader with ten minutes should run:

```
python -m void preflight            # every live chain, one summary line
python -m pytest tests/test_preflight.py tests/test_vault.py tests/test_judge.py \
                 tests/test_model_catalog.py
```

and read, in this order: `void/preflight/__init__.py` (what "working" is asserted to mean),
`void/orchestration/judge.py` (why the claim set is deliberately small), `void/vault/__init__.py`
(why absence is a first-class answer), and `void/security/ownership.py` (the defect the harness
found).
