# Architecture map: where the prompt-pack work sits

The directive asks for an architecture map alongside the source inventory and the requirement
ledger. This is it: what was added, where each piece attaches to the existing engine, what it is
allowed to touch, and the three paths worth tracing end to end.

The governing constraint is in `PROMPT_PACK_SOURCES.md` §4 — the packs describe a localhost web app
built from an empty folder, and V.O.I.D is a mature Windows assistant. Nothing here replaces an
existing system. Every piece is either a new leaf hanging off a seam that already existed, or a gate
inserted into a funnel that already existed.

---

## 1. What was added, and what it depends on

Nineteen modules, 6 496 lines. The dependency column is what each imports **from `void.*`** — not
the standard library — because that is what says how entangled a module is.

| Module | Lines | Depends on (internal) |
| --- | --- | --- |
| `orchestration/judge.py` | 271 | **nothing** |
| `providers/catalog.py` | 362 | **nothing** |
| `security/ownership.py` | 103 | **nothing** |
| `vault/__init__.py` | 637 | `memory.index` (BM25) |
| `vault/ask.py` | 87 | nothing |
| `actions/vault.py` | 175 | `vault` |
| `focus/commands.py` | 172 | nothing |
| `focus/lines.py` | 108 | nothing |
| `focus/reader.py` | 210 | nothing |
| `focus/__init__.py` | 651 | `focus.lines`, `focus.reader`, `security.ownership` |
| `ui/focus_card.py` | 252 | `focus`, `runtime.diagnostics` |
| `teardown/address.py` | 113 | nothing |
| `teardown/fingerprints.py` | 161 | nothing |
| `teardown/browser.py` | 519 | nothing |
| `teardown/__init__.py` | 705 | `browser.safe_url`, `teardown.fingerprints`, `teardown.panel` |
| `teardown/panel.py` | 353 | `teardown` (one constant), `runtime.diagnostics` |
| `perception/stillness.py` | 329 | `perception.screen` |
| `billing/__init__.py` | 476 | `artifacts`, `security.ownership` |
| `preflight/__init__.py` | 812 | *(deliberately everything — see §4)* |

**Three modules import nothing of V.O.I.D's at all.** That is not an accident and it is worth
preserving:

* `judge.py` is a pure function over a list of receipts. It cannot reach a provider, a tool or the
  filesystem, so nothing a model says can make it lenient and nothing it does can have a side
  effect. It is the component with the most authority over what the owner is told, and it has the
  least access of anything in the tree.
* `catalog.py` decides whether a spoken model name resolves. Its known set is handed in from
  configuration, so it cannot acquire a model id by observing one.
* `ownership.py` is four filesystem calls and a status enum.

The one module that depends on everything is `preflight`, and that is its entire purpose.

---

## 2. Where each piece attaches

Five seams, and no new ones.

### a. The route chain in `Assistant.run` — four new routes

`run` is an ordered chain of deterministic matchers, each returning `None` to pass the goal on. Four
were inserted. **Order is load-bearing**, and the reasons are in the code:

```
kill switch
  → control command          (stop / approve / deny)
  → memory command
  → brain command            NEW  providers/catalog.py
  → focus command            NEW  focus/commands.py
  → teardown route           NEW  teardown/address.py + teardown/panel.py (the reader)
  → named conversation
  → website route
  → fast path                (applications and folders)
  → reference route
  → conversation route
  → vault route              NEW  vault/ask.py + actions/vault.py
  → recall route
  → the model
```

Two placements were argued over and one was a defect:

* **brain before website.** "Switch to chat gpt" is a *website* request; "switch to opus 5.5" is a
  brain request. The brain route originally claimed any sentence containing a family word and broke
  the first. It now claims only an utterance that names a version, says brain/model, or resolves in
  the catalogue.
* **teardown before website**, so "take apart stripe.com" is not read as "open stripe.com".
* **vault after every matcher that could claim the words**, so "open my notes folder" stays a
  folder, and **before** recall, because "my notes" is an explicit scope the owner asserted while
  recall routing is a heuristic.
* **the reader before the address parse** inside the teardown route, so "show me the last teardown"
  is answered from the vault and never touches the kit.

### b. The agent's completion path — one gate

`judge.screen` sits at exactly one place: `core/agent.py:1195`, immediately before the final answer
reaches the owner.

```
model produces final text
  → judge.screen(final, receipts collected this turn)
      → task.result = ruling.answer      (what the owner is TOLD)
      → task history keeps the original  (the audit trail keeps both)
```

The receipts come from the single execution funnel `Agent._run_call` already writes, so the judge
reads a ledger it cannot write. That is why a model cannot forge one.

### c. The file layer — one hardening call

`security/ownership.py` is called wherever V.O.I.D creates its own state: the state directory, the
task/state/memory databases, the perf log, the stop file, and the invoice ledger. Applied **at
creation and again at boot**, so an installation that is already wrong gets repaired rather than
merely not made worse.

### d. The Qt runtime — two read-only observers

`VoidRuntime` gained two polling timers, both modelled on the existing kill-switch watch:

```
VoidRuntime.start()
  → killswitch watch   (existing)
  → focus card         NEW  ui/focus_card.py       500 ms
  → screen watch       NEW  perception/stillness.py  5 s
```

Neither can act. `poll_focus_card` calls `FocusEngine.state()` and nothing else — a test asserts
that only `state()` is ever reached — because a panel that could start or end a session would be a
second control path over the owner's attention. `poll_screen_watch` calls `tick()`, and the watch
has no provider to call.

### e. The artifact layer — reused, not duplicated

`billing` produces an `ArtifactPlan` and hands it to the existing `artifacts.generate` /
`inspect_artifact`. It inherits that layer's honesty rule for free: the file is re-opened and
reported on rather than assumed.

---

## 3. Boundaries — what each new piece may not do

These are the properties tests defend, stated once:

| Piece | May not |
| --- | --- |
| `judge` | reach a provider, a tool or the filesystem; be made lenient by anything a model says |
| `catalog` | approximate a model name, or learn an id it was not configured with |
| `vault` | return an excerpt when coverage is ABSENT; report a capture as written when it was not |
| `focus` | hold an application name, window title, host or URL — as text **or** as a hash |
| `focus_card` | drive the session it displays; render an identity (inherits `SessionState`'s shape) |
| `teardown/browser` | click, type or navigate — the CDP client has no such verb |
| `teardown/panel` | start, stop or alter a capture; break one by raising |
| `stillness` | see more than 576 block means; keep anything; write anything; call a model |
| `billing` | let a model's arithmetic reach the document; send anything |
| `ownership` | pretend POSIX mode bits describe NTFS access |

Four of these are enforced **structurally** rather than by review — the type has nowhere to put the
forbidden thing — and the tests assert the structure, not the behaviour:

* `SessionState` and `WatchState` contain only booleans, numbers and a closed-set phase string, so
  a card or a watch built from them cannot display an identity. The tests fail if a free-text field
  is ever added.
* `stillness` and `billing` are checked by **walking their syntax trees** for imports and calls, not
  by grepping their text — both files are full of prose explaining why they send nothing, and a grep
  cannot tell an explanation from a call. An earlier revision of those tests failed on their own
  docstrings.

---

## 4. `preflight` is deliberately the opposite shape

Every other module minimises what it touches. `preflight` maximises it: twenty probes that build a
real Assistant, index the real vault, write and remove a real note, engage and clear the kill switch
in a scratch directory, run a deliberately-wrong invoice through the real engine, and ask the Models
API for the configured model by id.

Order matters — cheap structural checks first, so a broken installation fails fast and legibly,
with the two that need the network last:

```
assembly → source on disk → secrets/permissions → engine protection → confinement
  → task store → kill switch → risk gate → judge → model refusal → invoice arithmetic
  → vault (index, question route, capture) → memory → focus → teardown (kit, routes, no stray browser)
  → optional layers → provider credential → model reachable
```

It is the answer to "done means preflight passed": the hermetic suite proves the units, and this
proves the chains are wired together in a fresh checkout.

---

## 5. The three paths worth tracing

**"What do my notes say about the retainer?"**

```
run → vault route (ask.py recognises the scope)
    → actions/vault.py search_notes (LOW risk)
    → vault.search() → BM25 over the indexed notes → Verdict
       FOUND  → excerpts + citable ids
       ABSENT → no excerpts at all, and an instruction not to improvise
```

**"Take stripe.com apart"** (via `python -m void teardown run`)

```
teardown.capture()
  → safe_url gate → CleanBrowser (fresh profile, ephemeral debugger port)
  → page facts BEFORE scrolling      ─┐
  → REA attaches                      │ eight steps, each emitted through
  → REA watches WHILE V.O.I.D scrolls │ panel.emitter(on_step) → text + panel
  → bundle read → snapshot → assets   │
  → fingerprints over captured source ─┘
  → digest → model writes the note → vault.capture → profile deleted
```

**An invoice**

```
model → strict JSON (descriptions, quantities, unit prices only)
  → build()  recomputes EVERY figure; the model's are compared and recorded as discrepancies
  → plan_for() → artifacts.generate → inspect_artifact   ← re-opened and verified
  → ledger.record()                                      ← only after verification
  → Issued(delivered=False)                              ← and nothing can set it true
```

The invoice ordering is the one to keep: render and verify **before** writing the ledger, so a
failed render never burns a number and leaves a gap an accountant has to explain.

---

## 6. What is deliberately absent

No new HTTP listener (the packs' `server.py` on 4700 has no place here — see SOURCES §4). No second
BM25 scorer. No second voice runtime. No second state machine for the orb. No credential in a config
file. No network client for a service this container has never reached — twelve requirements are
BLOCKED rather than stubbed, because code that has never completed a request would be code claiming
a capability, which is the failure the receipts rule exists to prevent.
