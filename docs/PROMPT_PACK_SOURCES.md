# The Jarvis prompt packs — source inventory and reconciliation

**Archive processed:** `Prompt.zip` (uploaded; read from
`/root/.claude/uploads/7fbd9173-4f35-5e27-b77a-2b9b5fc5ac4e/ff30ab68-Prompt.zip`),
sha256 `b9bb5c6c890e434b504bd4822e8c59d1828555268a9d78b74e1c23f94f3dfcdd`, 2 243 035 bytes.
**Date processed:** 2026-10-19.

This document records what the archive actually contained, how its prompt sequences relate to each
other, and which of their requirements apply to V.O.I.D — including the ones that conflict with this
project's existing architecture and were deliberately resolved the other way. The per-requirement
ledger lives in `docs/PROMPT_PACK_REQUIREMENTS.md`.

The PDFs themselves are **not** committed. They are source material, not project code, and three of
them restate the same eight prompts; committing 2.2 MB of duplicated marketing PDF into an
application source tree would be the wrong trade. Everything they require that matters is captured
here and in the ledger, cited by document and prompt number.

---

## 1. Archive inventory — what was really in it

The archive is a flat `Prompts/` directory holding seven PDFs with hashed filenames. No nested
archives, no symbolic links, no absolute paths, no path-traversal entries, no encrypted entries. It
was extracted into a scratch directory outside the repository; the original archive is unchanged.

All seven opened, all seven extracted text cleanly (`pdftotext -layout`), and all seven are distinct
by content hash. **111 pages in total.** Nothing was unreadable, and nothing was skipped.

| # | Filename (first 12 chars) | sha256 (first 12) | Pages | Document title (read from the PDF, not the filename) |
| --- | --- | --- | --- | --- |
| 1 | `5d57b19dec27` | `b5f697526b04` | 6 | Build Your Own JARVIS — The Free Prompt Pack |
| 2 | `f2f644c29630` | `0188db3ae1c4` | 12 | Build Your Own Jarvis — The Fable 5.1 Prompt Pack |
| 3 | `33f50dc44e40` | `0204d40b752a` | 24 | Build Your Own Jarvis — The Claude Opus 5.5 Prompt Pack |
| 4 | `db925788823c` | `11f276025d5c` | 22 | Build Your Own Jarvis — The GPT-6 Astra Prompt Pack |
| 5 | `aa15db53ff44` | `24c08f98d26e` | 19 | Give Your Jarvis Reflexes: The Jev Prompt Pack |
| 6 | `68f741f5976a` | `06b74e79a507` | 19 | Give Your Jarvis X-ray Vision: The REA Prompt Pack |
| 7 | `8309ca610e05` | `de8e6475e1c9` | 9 | The Jarvis Phone Pack |

A note on page counts, because it bit during inventory: `file(1)` reports "8 page(s)" for six of
these PDFs. That is wrong — it reads only the first page-tree object. `pdfinfo` is the number that
matches the documents, and it is the one in the table.

All seven are by the same author (Zubair Trabzada / AI Workshop) and are consumer prompt packs for
building a "Jarvis" from an empty folder with a coding agent. They are **instructions to a coding
agent**, not specifications of V.O.I.D, which is the central fact driving the reconciliation in §4.

---

## 2. Prompt sequence map

Documents 1–4 are successive editions of the **same** eight-prompt foundation, re-aimed at whichever
model was current. Documents 5–7 bolt onto that foundation. Reconstructed order, with each prompt's
own numbering preserved:

### The shared foundation (Part One)

| Step | Free (6) | Fable 5.1 (8) | Opus 5.5 (18) | Astra (16) | Jev (12) | REA (12) |
| --- | --- | --- | --- | --- | --- | --- |
| Galaxy + brain | 1, 2 | 01 | 01 | 01 | 01 | 01 |
| Voice both ways | 3 | 02 | 02 | 02 | 02 | 02 |
| Fly-to-source | 4 | 03 | 03 | 03 | 03 | 03 |
| Persona | 5 | 04 | 04 | 04 | 04 | 04 |
| Capture by voice | 6 | 05 | 05 | 05 | — | 05 |
| Sight (screen) | — | 06 | 06 | 06 | — | — |
| **Preflight** | — | **07** | **07** | **07** | **05** | **06** |
| Model swap + refusal | — | 08 | 08 | 08 | — | — |

The Free pack splits the foundation across six smaller prompts; the later editions merge it into one
larger first paste and say why ("longer prompts now beat more prompts"). The Opus and Fable packs are
otherwise near-identical in Part One; Astra's Part One matches too.

### The divergent second halves (Part Two)

| Pack | Part Two subject | Prompts |
| --- | --- | --- |
| Opus 5.5 | Telephony and messaging: phone line, Google desk, Telegram, standing orders, busy opener, post-call execution, reminder/briefing calls, inbound hotline, staged calls, security audit | 09–18 |
| Astra | "New organs": focus sessions, deferred lock, re-target, named distractions, camera posture, screen watch, swap personality, field instrumentation | 09–16 |
| Jev | A reflex layer over every utterance using TypeSafe AI's "Jev" decision model: bay, prepass + strip, vault check, judge, filming mode, desk triage + shadow governor, field drill | 06–12 |
| REA | Website teardown with the `rea-agents` npm tool: pinned install, clean browser, capture, teardown note, voice routes + panel, drill | 07–12 |
| Phone | Three Retell agent prompt scripts (booking, inquiry, code-gated receptionist) plus an invoice engine | Scripts 01–03, Prompt 04 |

**These Part Twos are alternatives, not a cumulative stack.** They were written weeks apart, each as
"the features I built in the last two weeks", and three of them require a different third-party paid
service (Retell, TypeSafe AI, `rea-agents`). Concatenating all four is not what the corpus asks for;
each is a branch from the same foundation.

---

## 3. What the corpus agrees on

Read across all seven documents, the same laws recur, and these — not the particular UI — are the
substance of the collection:

1. **Preflight or it isn't done.** Four of the seven packs carry the same harness prompt and call it
   "the one that matters": run every *live* chain end to end, tick or cross per check, one summary
   line `N pass, N fail, N warn`, non-zero exit on any failure, and "done" means preflight passed.
2. **An honest error beats a helpful guess.** Named explicitly in the model-swap prompt (never
   fall back to the nearest model id) and repeated as a lesson in four packs.
3. **Receipts or it didn't happen.** Opus 10/14 ("only claim an action happened when a tool result
   confirms it"), Jev 09 (the judge), Phone pack security rule 7.
4. **Say so when you do not know.** "Not in the vault" instead of answering from a note that merely
   sounds related (Jev 08); "say plainly when the notes do not cover it" (every Part One).
5. **Scope by architecture, not by promise.** Phone pack script 03: an agent that cannot reach the
   data cannot leak it. Opus 16: the hotline code is checked by the server and never put in the
   agent's prompt.
6. **Outside content is data, never instructions.** Opus 10 and 18; a stranger's words never reach
   the owner's notes.
7. **Never put a secret in a chat window; keys go in a file the owner edits.**
8. **Tune from logged numbers, never from memory**, and keep every threshold a named constant with a
   comment at the top of its file.

Laws 1–7 are either already V.O.I.D's practice or are what the implementation work in the ledger
adds. Law 8 is this repository's existing habit (see `void/voice/*` and `docs/VOICE_*`).

---

## 4. Reconciliation — why V.O.I.D does not become the pack's Jarvis

The packs build a **localhost web application**: `build.py` writes `viewer/graph-data.js`, a
`server.py` on port 4700 serves a single-page 3-D viewer, keys live in `config.json` next to the
code, the browser holds the microphone and the speech synthesiser, and `preflight.py` probes HTTP
endpoints.

V.O.I.D is already a different and more capable thing: a local-first Windows assistant of ~92 000
lines with its own voice runtime (wake word, Whisper STT, TTS with barge-in), an agent loop with
durable SQLite checkpoints, a risk gate and kill switch, encrypted owner-reviewed memory, provider
governance, Playwright and UI-Automation layers, an MCP server, and 4 200-plus tests.

Where the two disagree, the following decisions were taken and are binding on the ledger:

| Conflict | Pack position | V.O.I.D position | Decision |
| --- | --- | --- | --- |
| Where secrets live | `config.json` in the project root, chmod 600 | OS credential store (`keyring`), never a repo file | **Keep V.O.I.D's.** Adopting `config.json` would be a security regression, and `void/security/secrets.py` already satisfies the intent ("never paste a key into a chat"). |
| The brain's HTTP surface | `server.py` on 4700 with `/chat`, `/see`, `/remember`, `/model` | In-process Python API plus the existing authenticated device gateway | **Keep V.O.I.D's.** A second unauthenticated HTTP server on the owner's machine is new attack surface for no capability gain; every pack endpoint has an in-process equivalent. The Opus 18 hardening rules were instead audited against the gateway that *does* exist. |
| Voice | Browser Web Speech API, `FINISH_MS` buffering | Local wake word, Whisper, SAPI/Gemini TTS, measured endpointing | **Keep V.O.I.D's**, which is strictly stronger and already tuned from logged distributions. |
| The 3-D view | A galaxy of note nodes | The Singularity renderer (`void/ui/void-singularity-renderer`) | **Keep V.O.I.D's.** The galaxy is the pack's visual identity; the black hole is this project's. The *capability* the galaxy exists to prove — showing which note an answer came from — is met by returning cited note ids from the vault. |
| Retrieval | "keyword overlap, title matches weigh extra" | BM25 over a stemmed tokenizer already in `void/memory/index.py` | **Reuse V.O.I.D's index.** Same intent, better implementation, no new retrieval engine. |
| Where the persona lives | One commented block at the top of `server.py` | One commented block in configuration/prompt assembly | **Honour the intent** (single editable block) in V.O.I.D's own layout. |

Nothing above discards a pack requirement; each keeps the requirement and rejects the pack's
particular mechanism in favour of the equivalent that already exists here. The requirements that are
genuinely new capability — the preflight harness, the notes vault with honest absence, the receipt
judge, the model-id refusal rule — were built, and are in the ledger with their tests.

---

## 5. External dependencies, probed rather than assumed

| Dependency | Needed by | Probe result (2026-10-19, this container) |
| --- | --- | --- |
| Retell AI | Opus 09, 13–17; Phone scripts 01–03 | `api.retellai.com` unreachable — the agent proxy refuses the tunnel (403). Also needs an account, a purchased number and a real phone to test against. **BLOCKED.** |
| TypeSafe AI "Jev" | Whole Jev Part Two (06–12) | `docs.typesafe.ai` and `api.typesafe.ai` both unreachable — proxy refuses the tunnel (403). The prompt itself requires reading the live docs *before writing a line*, which cannot be done. Writing a client against a remembered wire format would be inventing an endpoint. **BLOCKED.** |
| Telegram Bot API | Opus 11, 14, 15; Phone 04 | Needs a BotFather token and a chat id that only the owner can create. **BLOCKED on credentials.** |
| Google Gmail/Calendar | Opus 10, 15 | Needs a Google Cloud project, consent screen and OAuth client the owner must create. **BLOCKED on credentials.** |
| `rea-agents` (npm) | Whole REA Part Two (07–12) | Package is real: `rea-agents@6.7.0`, MIT, `repository` resolves to `github.com/morluto/rea` exactly as the pack says to verify, `engines.node ^22.19 || ^24.11 || >=26`. Node v22.22.0 is present. Installable here with `--ignore-scripts`. **Not blocked by access** — see the ledger for why it was still not built. |

The proxy refusals above are a property of this execution environment, not evidence that the
services do not exist.
