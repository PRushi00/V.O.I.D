# Structured state, weekly maintenance, preferences and preference-aware routing

What this milestone added, and — more usefully — what it deliberately did not.

## 1. The persistence boundary

V.O.I.D now has two local SQLite databases, and the split between them is the whole design:

| | `memory.sqlite` (`void/memory/`) | `state.sqlite` (`void/state/`) |
| --- | --- | --- |
| Holds | what V.O.I.D remembers **about the owner** | what V.O.I.D knows **about the computer** |
| Encryption | AES-256-GCM, key in Windows Credential Manager | none — because nothing in it is sensitive |
| How things get in | owner says so, or a proposal the owner accepts | observed by a scan, or set as a preference |
| Lifetime | durable until corrected or forgotten | replaced by the next scan; snapshots pruned to 8 |
| Existed before? | **yes, unchanged** | no |

**No migration was performed, because none was needed.** Inspection found `void/memory/store.py`
already a versioned SQLite schema (`memory_items`, `memory_events`, `schema_meta` with a version and a
generation counter) with AES-256-GCM payloads whose AAD binds `item_id|kind|origin|status|sensitivity|
cloud_ok`, an append-only event log for provenance, and explicit remember / propose / accept / reject /
correct / forget. The structured-persistence requirement was already met for memory. Manufacturing a
migration would have risked the owner's encrypted data to achieve nothing, so the existing store was
left untouched — not one line changed.

What was genuinely missing was a durable home for operational facts about the machine. Those had no
store at all, or lived in configuration where they could not be queried or revised.

### What `state.sqlite` refuses to hold

`looks_secret()` runs on every value written. A preference value or an observation field matching a
credential shape — `sk-…`, `AIza…`, `ghp_…`, a JWT, a PEM header, `password:`/`api_key=` — is refused
(preference) or dropped (observation field) rather than stored. A plain SQLite file is the wrong place
for a secret, and "we only ever put metadata in it" is a weaker guarantee than a check.

## 2. Sunday maintenance

```
trigger (any)  ->  run_if_due()  ->  claim the week  ->  collect scopes
                                                             |
                      prune <- save snapshot <- diff vs last complete snapshot
                                                             |
                                          promotion filter -> propose (owner still accepts)
```

### Scopes are a closed set

`SYSTEM_BASIC`, `APPLICATIONS`, `DEVICES`, `STORAGE_METADATA`, `NETWORK_METADATA`,
`BROWSER_METADATA`, `SECURITY_CONFIGURATION` run by default. `SERVICES` is implemented but **excluded
from the default run**: its weekly churn is mostly Windows updating itself, and it is the scope most
likely to drift toward privileged inspection. There is no `EVERYTHING` scope and no recursive file walk.

Every collector reuses an existing V2 observation function (`void/system/host.py`,
`devices.py`, `network.py`, `wmi.py`) and the existing `AppCatalog` — no second way to look at the
machine was added.

### What is collected, and what is not

Metadata only: names, identities, versions, presence, counts, booleans. Measured on the owner's
machine: **258 observations across 7 scopes** (202 applications, 27 devices, 5 browsers, 14 security
switches, 6 network interfaces, 3 system, 1 storage).

Deliberately absent, by construction rather than by policy note:

- an application's **launch path** — not needed to know it exists, and a privacy leak in a plain file
- **allowed/protected root paths** — recorded as a *count*, so "a root was added" is visible without
  where it points
- network **addresses** — the interface existing and being up is what a route can act on
- and nothing from any scope reads a file, a message, a cookie, a token or the credential store

### Running once a week is a database fact

The week key is **the date of the preceding Sunday** (`2026-10-04`), claimed with a `UNIQUE` insert.
A second trigger finds the week taken and does nothing; two racing processes resolve to exactly one
claim (tested with six threads on a barrier).

This started as the ISO week and that was wrong in a way worth recording: an ISO week runs Monday→
Sunday, so Sunday is its *last* day — a forced run on Monday the 5th would have shared a key with
Sunday the 11th and silently cancelled that Sunday's real pass. Anchoring backwards means a mid-week
run claims the Sunday already gone and cannot consume the next one.

### Interruption

A killed run leaves a `running` claim, not corrupt data — every write is a single transaction.
`release_stale_runs()` reclaims a claim older than an hour by marking it `failed`, and `begin_run()`
takes over a `failed` week. It takes over **only** `failed`, never a live `running` one: an earlier
version used "anything not `ok`", which let a second caller steal the week from a run still working —
two simultaneous passes, the exact thing the unique key exists to prevent.

### Noise control

Volatile fields (`free_gb`, `uptime_s`, `bytes_sent`, `percent`, …) are recorded but excluded from both
the diff and the snapshot digest. A week in which only free disk space moved produces **zero** changes.
The first snapshot is a baseline, not a week in which 202 applications were installed at once.

## 3. The memory-promotion boundary

This is the rule the module exists to hold:

> **Computer knowledge is not user memory.**

`PROMOTABLE` is exactly two change kinds — `browser_installed` and `browser_removed` — because a
browser appearing or disappearing is the one weekly observation genuinely about *how the owner works*
rather than about the disk: it may mean the browser V.O.I.D was told to prefer is gone.

Everything else stays system state. "Chrome is installed" → Application Registry. "Bluetooth device
XYZ was seen" → Device Registry. Neither becomes memory.

Even a promotable change only becomes a **proposal**, through the existing
`MemoryService.propose(..., tainted=True)` path, which still requires the owner to accept it and still
applies the existing memory policy. Maintenance has no call to `remember()` or `accept()` — asserted by
test, with a fake memory service that raises if either is touched. A run is capped at **3** proposals
however eventful the week: a weekly job that could fill the review queue would make the queue useless.

## 4. Preferences

Structured, persistent, provenanced — and never permissions.

```
preferences(key, value, source, explicit, confidence, active, created_at, updated_at)
preference_revisions(id, key, value, source, explicit, action, at)
```

`explicit` separates "the owner said so" from "V.O.I.D noticed a pattern", which is what keeps
**installed / used / preferred** three different things. Nothing in this milestone infers a preference;
the column exists so that a future inference cannot masquerade as an instruction.

Keys are the existing closed set (`browser`, `editor`, `terminal`, `music`, `mail`, `messaging`); an
unknown key is refused by the CLI and ignored by the registry. Deactivation keeps the history.

```bash
python -m void prefs list
python -m void prefs set browser "opera gx"
python -m void prefs unset browser
python -m void prefs history browser
```

Opera GX is represented as **data** — `preferences.browser` in `config/default_config.yaml` — not as
routing logic. There is no `if website: launch Opera` anywhere.

## 5. Preference-aware routing

Integrated into the **existing** `RouteResolver`. No second routing engine: preferences arrive as
`WorldState.preferences`, which `routes.py` already consulted, and the new part is that an utterance
can contradict them.

Precedence in `WorldState.preferred()`, highest first:

1. **this utterance's override** — `overrides`, from `void/orchestration/overrides.py`
2. **the owner's stored preference** — `state.sqlite`, then `config` as the shipped default
3. nothing — the resolver falls back on reliability and cost, as before

Security does not appear in that list **because it is not in competition with it**: a route whose
capability is absent is dropped from the field *before* scoring, and risk is a scoring term. So neither
an override nor a preference can select a route the machine cannot run or policy forbids — they only
choose among routes already permitted. Tested directly.

### Overrides without special cases

The only domain knowledge is a vocabulary, and for browsers it is
`void/browser/playwright_adapter._BROWSERS` — the same table the browser layer uses to *launch* one. So
"Edge" is recognised because V.O.I.D genuinely knows how to drive Edge, not because a branch was
written for it. An alias (`microsoft edge` → `edge`) resolves to the table's own spelling and yields
nothing on a machine without that browser: an override chooses between browsers V.O.I.D can drive, it
cannot name one into existence.

Recognised: `in X`, `with X`, `using X`, `via X`, `use X for …`. Deliberately refused, because a false
positive sends the owner's work to the wrong application:

| utterance | result |
| --- | --- |
| `Open Gmail in Edge` | `{"browser": "edge"}` |
| `Use Opera for this` | `{"browser": "opera"}` |
| `Open the invoice in Documents` | `{}` — a folder, not an application |
| `Edge is slow, open Gmail` | `{}` — a mention, not a choice |
| `open it in the background` | `{}` |

An override is **not** remembered as a new default: saying "in Edge" once does not change which browser
V.O.I.D reaches for tomorrow. That is why `overrides` and `preferences` are separate fields rather than
one merged map.

Existing-state reuse is unchanged — a tab already open in the preferred browser still outscores a
launch, and reference resolution ("open this chart") is untouched.

## 6. Observability

Reuses the existing `void/perf` stream; no second telemetry architecture. The `maintenance` event is
registered in the allowlist (`void/perf/schema.py`) with `op` ∈ {started, scope_started,
scope_completed, scope_failed, snapshot, diff, memory_candidate, finished, failed} plus counts, the
scope **name**, the week key and an exception **class**. Never an application name, a device name, a
path or an observation — the questions it answers are "did it run, what did it look at, how much
changed", and none of those need the contents.

## 7. Scheduling

Deliberately **no new scheduler and no Windows task was registered.** The runner self-gates, so any
trigger works and firing it often is harmless:

```bash
python -m void maintenance run          # runs only if due (Sunday, not yet done)
python -m void maintenance run --force  # try now; still at most once per week
python -m void maintenance run --dry-run --scopes BROWSER_METADATA
python -m void maintenance status
python -m void maintenance changes
```

`void/runtime/scheduled_task.py` already registers Windows tasks, but its triggers and description are
hardcoded for the voice runtime and that file is **frozen** for this milestone, so it was neither
modified nor reused with a misleading description. Pointing Task Scheduler at
`void maintenance run` is a one-line owner action; the weekly semantics are already guaranteed in SQL.

## 8. What was not built

- **No migration** — the memory store already satisfied the requirement (§1).
- **No second scheduler**, no server database, no vector store, no ORM, **no new dependency**
  (`sqlite3` is standard library).
- **No voice/runtime change** — 29 files hash-verified byte-identical before and after.
- **No preference inference** — the `explicit`/`confidence` columns exist for a future one; nothing
  writes an inferred preference today, so a single observation cannot become a permanent preference.
- **No F0 feature.** Searching source, docs, workspace, tests, comments and all commit messages for
  `F0`, `F-0` and `F 0` found only "F0 space" (fundamental frequency, in a wake-word training note),
  `RTF 0.9` (real-time factor), and minified-JS variable names. The project numbers work `D-NN`,
  `P0`–`P5`, `T0.x`, `9A`–`9C`; there is no F series. Recorded as
  **`F0_STATUS = NO_VERIFIED_REQUIREMENT_FOUND`**, not invented.
