# Automatic application discovery and resolution

Date: 2026-09-23 · Base commit 1b1af96 (working tree, uncommitted)

V.O.I.D does not hold a list of installed applications. It discovers them from Windows, keeps that catalog fresh by
itself, and resolves what the owner *says* to exactly one of them — or to none. Installing a new application
requires no code change, no configuration and no prompt change.

```
VOICE / STT
   ↓
fast-path grammar          void/core/fast_path.py      "open <name>", nothing else
   ↓
name normalisation         void/actions/app_names.py   pure: case, punctuation, spacing, vendor words
   ↓
catalog + resolution       void/actions/computer.py    discover → cache → refresh → match
   ↓
validated app_id           AppCatalog.validate         the identity is the ENGINE's, never speech or model text
   ↓
RiskGate / CapabilityEngine  (unchanged)
   ↓
launch                     void/actions/apps.py        no shell, no command string
   ↓
"Opening WhatsApp."
```

No model is involved anywhere in that path.

## 1. Discovery sources

`RealWindowsBackend.discover_apps()`, read-only, in this order:

| Source | Kind | Launch identity |
|---|---|---|
| `App Paths` registry (HKLM + HKCU) | `exe` | resolved executable path |
| Start Menu `.lnk`, system-wide and per-user | `lnk` | the shortcut |
| a small PATH alias set (notepad, calc, explorer, …) | `exe` | resolved executable |
| shell `AppsFolder` namespace (Microsoft Store / MSIX) | `uwp` | AppUserModelID |

All four use pywin32/`winreg`/`glob`, already dependencies. **Nothing new was added.** The registry is opened
`KEY_READ` and never written.

On this machine: 195 applications, of which 51 are Store apps that none of the path-based sources can represent.

## 2. Automatic refresh

Three mechanisms, cheapest first — no background service, no watcher thread.

1. **TTL (10 min) + a cheap change signal.** Past the TTL the catalog does *not* rebuild; it reads
   `discovery_fingerprint()` — directory mtimes of the Start-Menu trees plus the `App Paths` key counts. Measured
   **4.8 ms, against 332 ms for a rediscovery (69×)**. Unchanged fingerprint → keep the catalog, restart the clock.
2. **Rebuild on a miss.** A name that matched *nothing* may be an application installed since the last build, so a
   miss forces a real rediscovery (rate-limited to once per 15 s). A miss was already headed for the slow path, so
   this is free — and it is what makes a just-installed application work on the *first* command instead of after a
   restart. It also covers Store apps, which touch neither the Start Menu nor `App Paths`.
3. **`invalidate()`** — the explicit refresh mechanism; the next lookup rediscovers.

Removal is handled at the other end: every launch revalidates (§6), so an uninstalled application is refused
immediately rather than waiting for a refresh.

## 3. One entry per installed application

Discovery returns the same application several times — Discord ships a shortcut in two Start-Menu folders, Excel has
a shortcut *and* a registry entry. Before this change that made the *exact* name ambiguous, so **"open Discord"
silently fell through to the model** and, while Gemini was returning 503s, failed outright.

Two records are the same application when **the normalised display name AND the program agree** — the target's
filename without extension, or the AppUserModelID for a Store app. Two *different* programs that merely share a
display name do **not** collapse: they stay a genuine ambiguity, which is also what stops a planted shortcut from
taking over a trusted name (§7).

Within a group the launch identity is chosen deterministically, never arbitrarily:

`Start-Menu shortcut > Store app > bare executable`, then a logon-`Startup` copy loses to the ordinary shortcut,
then the shallower path, then the path itself.

Result on this machine: **211 records → 195 applications, 0 duplicate names** (was 11). Every record stays
resolvable by its own `app_id`, so an id already handed out keeps working.

## 4. Normalisation

`void/actions/app_names.py` — pure functions, no OS, no I/O, no model.

* separators (`- _ / \ . , : ( ) [ ] ® ™ …`) become spaces; case folded; whitespace collapsed;
* `+`, `#`, `&`, `'` are **kept** — dropping them would make `Notepad` and `Notepad++` compare equal;
* nothing is stripped for being "common": `Windows Security` never becomes `Security`;
* runs of two or more single-character words are rejoined, because speech-to-text spells acronyms out
  (`opera g x` → `opera gx`). Display names practically never contain adjacent one-letter words, so this only ever
  reassembles what a transcript took apart;
* `squash()` additionally removes the spaces, which settles *how a name is spaced* (`Whats App` ↔ `WhatsApp`).

## 5. Matching hierarchy

`AppCatalog.resolve_name()`, first tier that matches anything at all wins:

| # | Tier | Example |
|---|---|---|
| 1 | `exact` normalised name (after the alias table) | `opera-gx browser` → `Opera GX Browser` |
| 2 | `spacing` — the same name spaced differently (identity, not partial) | `whats app` → `WhatsApp` |
| 3 | `prefix` — the leading **whole words** of one installed name | `opera gx` → `Opera GX Browser` |
| 4 | `publisher` — the name without its leading vendor word | `teams` → `Microsoft Teams` |

A tier matching **more than one** application ends the search as `ambiguous`; falling through to a weaker tier
after a strong one was ambiguous would be guessing by another name.

There is **no fuzzy matching at all** — no edit distance, no substring containment, no scoring. `opora gx` does not
resolve; it misses. `gx browser`, `browser`, `oper`, `operagx` and `gx opera` do not resolve. `open Studio` with
several `… Studio` applications installed resolves to nothing, exactly as the brief requires.

`find_app` (the model-facing tool) falls back to the same hierarchy when exact/glob finds nothing, so the model is
told about `Opera GX Browser` rather than concluding the application is not installed. It still reports ambiguity
as ambiguity.

### Aliases

Three entries, all for one irregular spoken form (`vs code` → `visual studio code`). Everything else resolves from
the installed display name. A test pins the table at ≤ 12 entries, because an alias database would defeat the point
of discovering applications automatically.

### Ambiguity

Never guessed, never launched. The fast path answers deterministically — *"I found more than one match: Android
Studio, Visual Studio. Which one do you mean?"* — which is faster than a model round trip and is the only answer
available at all when the cloud is down. Names are sanitised exactly like any spoken reply; no plan is produced, so
nothing can start.

## 6. Launch identity and security

* The launch target is always an **engine-chosen `app_id`** resolved from the catalog. Speech and model text can
  only ever *name* an application; neither can supply a path, a command or a Store id.
* `launch_app` still refuses an arbitrary path, a display name, or anything not in the catalog/alias map.
* `validate()` before every launch: the target still exists; for a Store app the AppUserModelID is still well
  formed; **for a shortcut, that what it points at still exists.** Live validation found `open Discord` producing a
  UAC dialog and `WinError 1223` because both its shortcuts were uninstall leftovers pointing at a removed
  executable. Resolving a shortcut costs ~7 ms — affordable once per launch, but not for all ~110 shortcuts on
  every rediscovery (832 ms), so it is done here rather than at build time. An *unreadable* shortcut is unknown,
  not missing: it is still accepted.
* Launching is `os.startfile(lnk)`, `Popen([exe])`, or `Popen(["explorer.exe", "shell:AppsFolder\\<AUMID>"])` —
  argv lists only. **No `shell=True` anywhere, no command strings, no `eval`/`exec`.**
* `is_app_user_model_id` still accepts only `Publisher.App_hash!Entry`, checked at discovery, at validation and
  again immediately before launch.
* Every fast-path launch still runs through `Agent._run_call` → kill switch → risk level → `RiskGate.authorize` →
  telemetry. The fast path decides *what* to call, never *whether* it may run.
* **Catalog poisoning**: anything able to write a Start-Menu shortcut can put a trusted name on another program.
  De-duplication does not help it — the name and the program must both agree — so a planted shortcut stays a second
  application, the name becomes ambiguous, and nothing launches. (Write access to the user's Start Menu already
  implies code execution at logon; this is inherent to using Windows' own application list.)

## 7. Performance (real catalog, 195 applications)

| | |
|---|---|
| cold discovery (once per process, or after a real change) | **332 ms** |
| change signal instead of rediscovery | **4.8 ms** (69× cheaper) |
| warm resolution | **0.017–0.019 ms** |
| whole fast-path decision (grammar + resolution) | **0.026–0.049 ms** |
| shortcut validation, once per launch | ~7 ms |
| catalog + indexes in memory | ~121 KiB |

The filesystem is never scanned per command: **discover → cache → resolve**.

## 8. Measured before / after

| Command | Before | After |
|---|---|---|
| `open WhatsApp` | fast path | unchanged |
| `Hey V.O.I.D., open WhatsApp` | **missed entirely** (the wake word was only recognised spelled "void") | fast path, 0 model calls |
| `open Opera GX` | fast path, ~1 s | unchanged |
| `open Opera-GX` / `Opera G X` / `Opera  GX` | model | fast path |
| `open whats app` | model | fast path |
| `open Discord` | **ambiguous → model → 503** | resolves; refused cleanly as uninstalled (no UAC prompt) |
| `open teams` | model | fast path |
| duplicate names in the catalog | 11 | 0 |
| ambiguous name, cloud down | hard failure | a spoken question |
| newly installed application | needed a restart | found on the next command |

Live, through the real `Assistant` on this machine, with a provider that raises if touched: WhatsApp stopped, then
`"Hey V.O.I.D., open WhatsApp"` → **new process 22304, 0 model calls**; `"Hey V.O.I.D., open Opera GX"` → 7 new
`opera.exe` processes; `"open File Explorer"` → 9.9 ms.

## 9. Limitations

* Store applications are not covered by the cheap change signal (they touch neither the Start Menu nor
  `App Paths`); they are picked up by the rebuild-on-miss and by the TTL rebuild.
* Discovery was validated on one machine (195 applications, 51 of them from the Store).
* A shortcut's target is validated at launch, not at discovery — so a stale entry is still *listed* until it is
  used. Validating all ~110 shortcuts on every rebuild costs 832 ms and was judged not worth it.
* `AppCatalog.find_name_prefix` is no longer used by production code (`resolve_name` supersedes it). It is kept as
  the raw-name prefix primitive with its own contract tests rather than deleted.
* Speech-to-text mishearing a name entirely (`"Open Opera GX"` → `"Open all projects."`) is unaddressed and still
  goes to the model.
