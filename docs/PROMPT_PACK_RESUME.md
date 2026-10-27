# Resume: the prompt-pack work

Enough context to carry this on without re-reading the conversation that produced it. The two
documents it sits beside are the substance: `docs/PROMPT_PACK_SOURCES.md` (what the archive held, how
its prompts relate, and every conflict resolved *against* the packs) and
`docs/PROMPT_PACK_REQUIREMENTS.md` (the per-requirement ledger with the test or probe behind each
status). Read those for *why*; read this for *where things stand*.

**Branch:** `claude/gracious-gauss-u81whr`. **Pull request:** PRushi00/V.O.I.D#1 (draft).

---

The other durable artifacts the directive asks for:

| Document | What it is for |
| --- | --- |
| `PROMPT_PACK_SOURCES.md` | the archive inventory, the prompt-sequence map, and every conflict resolved against the packs |
| `PROMPT_PACK_REQUIREMENTS.md` | the requirement-to-evidence ledger: every requirement, its status, and the test or probe behind it |
| `PROMPT_PACK_ARCHITECTURE.md` | where each new module attaches, what it may not do, and the three paths worth tracing |
| `PROMPT_PACK_DECISIONS.md` | the consequential decisions, what was rejected, and what would change each one |
| **this file** | where the work stands, the numbers, the defects found, and what is blocked on what |

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
| Teardown panel and reader | `void/teardown/panel.py` | `python -m void teardown last`; `run` prints the eight steps live |
| Focus countdown card | `void/ui/focus_card.py` | starts with a session; tints amber on drift |
| Screen-watch organ | `void/perception/stillness.py` | `screen.enabled` **and** `screen.watch_enabled`, both false by default |
| Invoice engine | `void/billing/` | `python -m void invoice "two days for Acme at 450 a day"` |
| State files owner-only | `void/security/ownership.py` | `python -m void preflight` ("state file permissions") |

Each commit on the branch carries a full message: the first tranche (preflight, vault, judge, model
refusal, the permissions fix), focus sessions plus the notes-scoped question plus preflight in CI,
the teardown kit, a CI dependency fix, the three real defects behind CI's twelve, and the three
previously-declined items above.

## 3. Where the test numbers stand

Baseline before this work, in this container: **4 255 pass, 38 fail, 30 skipped, 2 xfailed.**
After it: **4 897 pass, 28 fail, 34 skipped, 2 xfailed; 4 961 collected.** Those 28 are a *subset* of the baseline 38 -
ten were fixed along the way (see the CI table below) and **no new failure appeared**, established
by diffing the before and after lists rather than by comparing their lengths.

**After the eighth pack (§8):** `4 979 pass, 19 fail, 35 skipped, 2 xfailed; 5 035 collected`, with
`origin/main` measured in a clean worktree in the *same* environment as the control: `4 910 pass,
19 fail, 35 skipped`. The two failure lists are **identical in both directions** - nothing new,
nothing lost - and the pass count is up by exactly the 69 cases the egress suite adds. The failure
count reads lower than the 28 above only because that run had three of CI's requirement files
installed that the earlier one did not; it is the *diff against a same-environment control* that
carries the meaning, never the absolute number. `min_collected` ratchets 4 920 -> 4 989: the previous
floor plus exactly what this pass added, which keeps the margin the last pass chose rather than
inventing a new one.

The remaining failures are environment-bound and none is a code defect: no Windows (`pywin32`,
`pythoncom`), no `xdg-open`, no audio or camera hardware, `faster-whisper`/`ctranslate2` not
installed, and one OpenTelemetry patch-version pin. The exact list is reproducible with:

```
python -m pytest -q 2>&1 | grep -E '^(FAILED|ERROR)' | sed 's/ - .*//' | sort
```

### What CI says, which is a different machine

`tests.yml` runs on `windows-latest`, so its red is not this container's red. The two sets barely
overlap: Linux fails for want of Windows, Windows fails for want of hardware and credentials.

Before this branch the workflow could not collect the suite at all - it installed
`requirements.lock` only, which has never carried the MCP SDK (defect 8 below). The first run that
executed gave **16 failed / 4 680 passed**. Four were mine and were fixed. I then reported the
remaining twelve as environment-bound, which **was wrong**: investigating them one at a time instead
of as a group found three real defects behind them.

| Was | Now | Why |
|---|---|---|
| 3 × `ProviderUnavailable` | **fixed** | An application-catalog failure was cancelling the FOLDER lookup two lines before it happened (defect 9), and two tests created their probe folder outside `security.allowed_roots`, so the catalog rightly refused and they had never passed anywhere |
| 4 × no audio device | **3 fixed, 1 gated** | `list_devices` reported an empty inventory as a FAILURE, which made the agent's completion guard refuse (defect 10). Absence is now an answer |
| 2 × no camera | **gated** | Genuinely needs a camera; split so the part CI *can* check - that the probe answers rather than raising - runs everywhere |
| 2 × no installed application | **1 fixed, 1 gated** | One asserted a reply wording the engine does not use on a machine with no messaging application, so it failed exactly where the answer was right |
| 1 × wake `_cpu_threads` | **fixed** | `voice.wake_cpu_threads` reached the Gen 3 detector and not the one that ships (defect 11) |

So four genuine preconditions remain, each skipping only where its own device is absent and still
running where it is present. The `hardware` marker is registered and documented as "skip in CI" but
**nothing ever implemented it**; blanket-skipping all nine marked tests would have turned five that
pass on windows-latest into skips, losing coverage CI really provides, so it was not used that way.

**Confirmed on windows-latest: 4 948 passed, 0 failed, 15 skipped, 2 xfailed.** All twelve of the
original failures are fixed, including the two folder tests, which did turn out to need nothing but
a desktop opener Windows already has. The suite also now runs to 100%: an earlier head crashed a
third of the way through, which is defect 13 below.

New test files: `tests/test_vault.py` (80), `tests/test_judge.py` (60), `tests/test_model_catalog.py`
(70), `tests/test_preflight.py` (28), `tests/test_focus.py` (95), `tests/test_teardown.py` (67),
`tests/test_focus_card.py` (48), `tests/test_teardown_panel.py` (57), `tests/test_stillness.py` (37),
`tests/test_invoice.py` (75).

`tests/guard_limits.json`: `min_collected` ratcheted to 4 920 (4 961 collected, so 41 of margin -
the floor is deliberately not flush against the count). `max_skipped` raised 6 -> 15, derived rather
than guessed: the 10 that windows-latest already skipped, plus the 4 per-device precondition gates
below, plus the one POSIX file-mode test that cannot run on NTFS. I set it to 14 first and CI
rejected it — the fifteenth skip was one I had added myself, in a later commit, and not recounted.
The ceiling is deliberately exact rather than padded, which is why it caught that. **Correction:** two earlier PR comments said this needed raising "from 4
to 6". It was already 6 on `main`, and 6 was already below the 10 windows-latest skips - the ceiling
had simply never been evaluated, because pytest failed first on every run and the guard step never
executed.

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

9. **An application-catalog failure cancelled the FOLDER lookup.** `_decide_phrase` returned
   "discovery" the moment `catalog.resolve_name` raised, two lines before it would have tried the
   folder index - which is a separate catalog with its own guard and needs nothing from the
   application backend. So on any machine whose app backend is unavailable, "open my `<name>`
   folder" fell through to the model: the exact 41-second round trip that route exists to remove,
   and invisible on a desktop where the backend answers. Fixed in `void/core/fast_path.py`.

10. **An empty device inventory was reported as a FAILURE.** `Reading` exists to separate "nothing
    is attached" from "nothing could be read" - its own docstring says so - and `list_devices` threw
    that away. The cost was not cosmetic: a failed tool makes the agent's completion guard report an
    unresolved execution failure, so on a machine with no sound card or camera "what is attached?"
    could not be answered at all, which is the one kind of machine where the answer is interesting.
    Failure is now reserved for the case where every source asked for was genuinely unreadable.
    Same rule as the notes vault: absence is an answer.

11. **The always-on wake preprocessor's thread bound was not pinned.** `voice.wake_cpu_threads`
    reached the Gen 3 detector and not the one that actually ships: `OpenWakeWordDetector`
    constructed `Model()` with no `ncpu` at all. One is openwakeword 0.6.0's own default, so this
    changed no behaviour - it PINS it, so a release that relaxed that default could not quietly put
    an ONNX spin-wait on every core, which `docs/VOICE_LATENCY_2026-09-23.md` measured at 20.6 cores
    of idle load. Verified against the real 0.6.0 source, because the package has no Linux wheel for
    current CPython.

12. **A card's placement could stop it appearing at all.** Found by writing `FocusCardView`'s tests
    against injected fakes rather than a display: `_place()` sat inside the same `try` as `show()`,
    so an unresolvable screen - an unplugged monitor, a compositor mid-restart - produced no card
    instead of a card in the wrong corner.

13. **A panel constructed a QWidget with no QApplication, which ABORTS the process.** Not a test
    failure - a crash. CI stopped a third of the way through the suite with exit 1, no failure
    summary and no traceback, because Qt's "Must construct a QApplication before a QWidget" is a
    fatal abort rather than a Python exception, so the try/except around the construction could not
    catch it. Invisible on Linux, where the PySide6 import raises first and IS caught. Found by
    mapping the progress percentage back to the collection order. Both view factories now return
    None under pytest and require an *existing* QApplication, following
    `void/ui/tray_indicator.py`. It mattered beyond the tests: `VoidRuntime._start_focus_card` ran
    with nothing guaranteeing an application existed.

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
| A real `generateContent` through the harness | Nothing from the owner. The credential is present and valid (`list-keys` reports `environment`; `model reachable` passes live), but the configured `gemini-3.8-flash` is returning `503 UNAVAILABLE / high demand`. `gemini-3.5-flash` generates fine, so this is model capacity, not the key — preflight now WARNs rather than blaming the credential (P1.23) |
| The latency budget (local/07) | A microphone, a speaker and a provider credential. Preflight already times every probe, so only the three numbers are missing |
| Anything Windows-native | A Windows machine. The focus *sensor* in particular: the engine is verified to the second against a scripted desk, the reader needs a real foreground |

**The three items this document previously listed as declined have been built**, on the owner's
explicit instruction: the teardown panel and reader (P4.18), the focus countdown card (P5.11) and
the screen-watch organ (P5.7). Two of those three declines turned out to be wrong rather than
cautious — once the logic was separated from the painting, almost all of the new tests needed no
display at all, and writing a view against injected fakes found a real defect (12 above). The third,
the screen watch, is answered in its design rather than waved away: it sees a 32×18 grid of block
means, keeps nothing, writes nothing, has no provider to call, needs two switches that both default
to false, and decides only WHEN the screen has been still — never what is on it. The original
reasoning is kept in the ledger beside the new status rather than deleted.

The invoice engine (P6.4) is built too, and **the ledger has no DECLINED rows at all**. With the
eighth pack's nineteen rows added (§8), the ledger stands at **50 VERIFIED, 13 BLOCKED,
17 ALREADY_SATISFIED, 12 PARTIAL, 4 INAPPLICABLE, 2 CONFLICTING — 98 rows**. Its **delivery
leg is still blocked** on Telegram and is reported as such rather than stubbed — `delivered` is
false, no code path can set it true, and a syntax-tree walk asserts the module imports no transport
and calls nothing that sends. What is built is the part that generalises: the model describes the
work, Python recomputes every figure and overrides the model's, and the override is recorded on the
invoice rather than quietly applied.

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


---

## 8. The eighth pack — "Run Jarvis Fully Local"

Supplied separately, after the first seven had shipped and merged. Inventory and provenance are in
SOURCES §1b; the nineteen rows are REQUIREMENTS §9; the reasoning is DECISIONS §G.

**What it changed: one thing, and it was worth it.** Prompt 08 asks for proof that nothing left the
machine. V.O.I.D made that claim in a dozen places and could not answer it: sixteen modules here can
open a connection and nothing knew. `void/egress/` closes it — a watcher at `socket.socket.connect`
(so no call site was edited and none can drift out of coverage), a drill that passes only when the
off-machine list is empty, and an honest list of what still reaches outward derived from the tree's
syntax trees rather than written by hand. 69 test cases, two new preflight probes,
`python -m void egress [--drill]`. Preflight goes 15 pass -> **17 pass, 0 fail, 9 warn**.

**What it confirmed, which is worth more than a feature.** Two of its retrospective lessons describe
fixes this codebase had already made and already measured — the local brain that thinks itself to
death (answered by `local_provider.think`, with the numbers in its docstring) and browser speech
recognition as the hidden cloud (V.O.I.D has never had an ASR path that leaves the machine). An
outside author on different hardware from a different premise arriving at the same two conclusions
is the strongest evidence in the corpus that they are real.

**What is left open on purpose.** The pack also prescribes retrying once with a bigger budget when a
local model returns empty. The switch half of that is built and measured; the retry half is not, and
was deliberately not guessed at — it cannot be exercised against a real Ollama here, and a silent
retry doubles the worst case for an uncertain gain. Recorded as P7.8 with the reversal condition in
DECISIONS §G2. **This is the one item in this pass that wants an owner's decision rather than a
credential.**

**What it did not move.** Its galaxy viewer, port-4700 server and `config.json` keys were refused for
the same reasons as the first seven (SOURCES §4). Its voice engine is Apple-Silicon-only and
V.O.I.D is a Windows assistant. Its latency budget needs a microphone, a speaker and a credential,
so it is BLOCKED rather than printed as zeroes.
