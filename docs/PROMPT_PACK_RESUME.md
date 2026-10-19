# Resume: the prompt-pack work

Enough context to carry this on without re-reading the conversation that produced it. The two
documents it sits beside are the substance: `docs/PROMPT_PACK_SOURCES.md` (what the archive held, how
its prompts relate, and every conflict resolved *against* the packs) and
`docs/PROMPT_PACK_REQUIREMENTS.md` (the per-requirement ledger with the test or probe behind each
status). Read those for *why*; read this for *where things stand*.

**Branch:** `claude/gracious-gauss-u81whr`. **Pull request:** PRushi00/V.O.I.D#1 (draft).

---

## 1. What the task was

A ZIP of seven "Build Your Own Jarvis" prompt packs (111 pages, all read) was supplied with an
instruction to bring V.O.I.D to the state those documents describe. The packs build a localhost web
app from an empty folder. V.O.I.D is already a ~92 000-line local-first Windows assistant with its
own voice runtime, agent loop, risk gate, encrypted memory and 4 255 tests, and it covers most of the
packs' Part One in a stronger form.

So the work was reconciliation, not construction: take the laws the whole corpus agrees on and that
V.O.I.D genuinely lacked, build those, and leave the architecture alone. The six conflicts where the
packs were overruled (keys stay in the OS credential store, no second HTTP listener, the existing
voice runtime stays, the Singularity renderer stays, BM25 is reused, the persona block lives in
V.O.I.D's own layout) are in SOURCES §4 with the reasoning.

## 2. What exists now that did not

| Area | Where | How to see it work |
| --- | --- | --- |
| Live-chain preflight | `void/preflight/` | `python -m void preflight` |
| Notes vault, with absence as an answer | `void/vault/`, `void/actions/vault.py` | `vault.notes_root` in local config, then "what do my notes say about X" |
| Receipt judge | `void/orchestration/judge.py` | gated into `Agent._loop`; `tests/test_judge.py` |
| Model-id refusal | `void/providers/catalog.py` | "try on opus 5.5" → refused with the list |
| Focus sessions | `void/focus/` | "thirty minutes on this" |
| Website teardown | `void/teardown/` | `python -m void teardown diag`, then `run` |
| State files owner-only | `void/security/ownership.py` | `python -m void preflight` ("state file permissions") |

Four commits on the branch, each with a full message: the first tranche (preflight, vault, judge,
model refusal, the permissions fix), focus sessions plus the notes-scoped question plus preflight in
CI, the teardown kit, and a CI dependency fix.

## 3. Where the test numbers stand

Baseline before this work, in this container: **4 255 pass, 38 fail, 30 skipped, 2 xfailed.**
After it: **4 650 pass, 38 fail, 30 skipped, 2 xfailed** — the *same* 38, verified by diffing the
failure lists, not by counting.

Those 38 are environment-bound and none is a code defect: no Windows (`pywin32`, `pythoncom`), no
`xdg-open`, no audio or camera hardware, `faster-whisper`/`ctranslate2` not installed, and one
OpenTelemetry patch-version pin. The exact list is reproducible with:

```
python -m pytest -q 2>&1 | grep -E '^(FAILED|ERROR)' | sed 's/ - .*//' | sort
```

New test files: `tests/test_vault.py` (79), `tests/test_judge.py` (60), `tests/test_model_catalog.py`
(70), `tests/test_preflight.py` (28), `tests/test_focus.py` (91), `tests/test_teardown.py` (67).

`tests/guard_limits.json` `min_collected` was ratcheted to 4 700 (4 706 collected).

## 4. Defects this work found and fixed

1. **`~/.void/tasks.sqlite` was created mode 0644**, holding task goals, the owner's own sentences,
   model answers and tool output in plain text; the only `chmod` in the codebase was the TLS key's.
   Found by `void preflight` on its first real run, with every test passing. Fixed in
   `void/security/ownership.py`, applied at boot as well as at creation so installations that are
   already wrong get repaired.
2. **A paused focus session aged and could expire while paused** — `tick` moved the last-tick mark
   during a pause, so `resume` had no pause duration to give back.
3. **A focus session that ended itself** when its time was up could not be asked for its report card.
4. **The teardown read the page's own title *after* scrolling**, so a page entitled to rename itself
   reported the wrong one.
5. **`Vault.capture` flattened every capture to one line**, which turned a structured note into one
   unreadable paragraph.
6. **A spoken "switch to chat gpt" was stolen** by the brain route, which runs ahead of the
   website matcher: any sentence containing a model family word was claimed. It now claims an
   utterance only when the owner clearly named a *model* — a version number, the word
   "brain"/"model", or a name the catalogue really knows — so a bare family word falls through.
7. **`--remote-allow-origins=*`** on the investigation browser would have let any page that found the
   port drive it through the debugger. Removed after checking against a real browser that the client
   connects without it, because it sends no `Origin` header. The debugger port is now ephemeral by
   default rather than the pack's fixed 9333.
8. **CI had never been able to collect the suite**: the workflow installed `requirements.lock` only,
   which does not carry the MCP SDK, so `tests/test_mcp_server.py` failed to import and pytest
   aborted. Pre-existing on `main`; the workflow's own header said it had never run.

## 5. A correction, on the record

An earlier revision of the ledger declined the REA teardown on the grounds that `rea-agents@6.7.0`
no longer had the commands the pack describes. **That was wrong.** The package was installed in a
scratch directory and `rea --help` lists `list-browser-targets`, `inspect-web-page`,
`analyze-web-bundle` and `capture-web-screenshot`, with matching descriptions. The teardown was then
built and drilled. The correction is kept visible in `docs/PROMPT_PACK_REQUIREMENTS.md` §6 rather
than edited away, because a ledger whose wrong entries disappear is not evidence of anything.

## 6. What is blocked, and on exactly what

Named here so the next session does not re-derive it. None of these was stubbed, and no unverifiable
network client was written — shipping one that has never completed a request would be code claiming a
capability, which is the failure the receipts rule exists to prevent.

| Blocked | Needs |
| --- | --- |
| Retell telephony (Opus 09, 13–17; the three phone scripts) | An account, a purchased number, a cell to call. Also `api.retellai.com` is refused by this container's proxy (403 on CONNECT) |
| TypeSafe AI "Jev" (the whole Jev Part Two) | A key, and network access to `docs.typesafe.ai` / `api.typesafe.ai` — both proxy-refused. The prompt itself requires reading the live docs *before writing a line*, which cannot be done |
| Telegram (Opus 11, 14, 15; phone 04) | A BotFather token and the owner's chat id |
| Google Calendar/Gmail (Opus 10, 15) | A Google Cloud project, a consent screen in production, an OAuth client |
| The live Gemini call and the Models API lookup | A Gemini credential. Both preflight probes WARN honestly without one |
| Anything Windows-native | A Windows machine. The focus *sensor* in particular: the engine is verified to the second against a scripted desk, the reader needs a real foreground |

Two things are **declined with a reason**, not blocked: the teardown's on-screen panel and the focus
countdown card (display work that cannot be validated from here — the state both need is published),
and the Astra screen-watch organ (its payoff is a model call on a screenshot of whatever the owner
has open, which is the most privacy-expensive thing in the corpus for the least certain benefit).
The invoice engine is recorded in the ledger as a business feature rather than a capability, with the
one rule in it that generalises — never trust a model's arithmetic — written down.

## 7. If something here breaks

Read `void preflight` first, then the ledger row for the thing that broke. The harness is meant to
grow: **add a probe every time something breaks in the field**, which is the discipline four of the
seven packs insist on and the reason the permissions defect above was found at all rather than
discovered by someone else.

The one rule worth repeating out of all of it: an honest error beats a helpful guess. A near-miss
model name refuses rather than loading the nearest thing; a question the notes cannot answer says so
rather than improvising from a related note; a sensor that cannot see reports `blind` rather than
accusing the owner of drifting; and an answer claiming an action with no receipt behind it does not
reach the owner at all.
