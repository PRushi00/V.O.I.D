# V.O.I.D — V1

A local-first personal AI assistant for Windows. **V1 goal:** prove the concept
end-to-end — you give a natural-language goal, V.O.I.D plans it, acts on your
machine, checkpoints its work, and you can stop it at any time.

This is the V1 vertical slice: the real agent loop, real Windows file/app
control, a replaceable AI-provider layer (Gemini + local fallback), durable
task checkpoints, a risk/confirmation gate, and an always-available emergency
stop — plus a minimal circular desktop widget.

---

## Quick start

```powershell
# 1. From the V.O.I.D folder, install dependencies
pip install -r requirements.txt

# 2. Prove the whole loop works right now, with no API key:
python examples/demo_offline.py

# 3. Store your Gemini key securely (Windows Credential Manager, not the repo):
python -m void set-key gemini

# 4. Give V.O.I.D a real goal:
python -m void "find my cybersecurity notes"
python -m void "open my resume and tell me how long it is"
python -m void "launch Cursor"

# 5. Launch the circular widget instead of the terminal:
python -m void ui
```

Emergency stop, from any terminal (or the UI's STOP button):

```powershell
python -m void stop          # halts running tasks at the next checkpoint
python -m void clear-stop    # resume normal operation
```

Other commands: `python -m void tasks` (list tasks),
`python -m void resume <task-id>` (resume an interrupted task),
`python -m void set-pin` (set an emergency-stop PIN).

Check that the installation actually works, end to end:

```powershell
python -m void preflight          # runs every LIVE chain; exit 2 on any failure
python -m void preflight --offline # skip the probes that need a credential or the network
python -m void doctor              # read-only health of a RUNNING instance; repairs nothing
```

`preflight` executes: it builds a real Assistant, indexes the real notes vault, makes one real
minimal provider call, asks the Models API for the configured model by id, writes and removes a real
probe note, and engages and clears the kill switch in a scratch directory. It never sends anything,
launches anything or touches your memory store. It ends with one line — `N pass, N fail, N warn` —
and "done" means that line has no failures in it.

---

## How it's put together

Every layer is replaceable — that's the point.

```
void/
  config.py            Config loading (YAML defaults + gitignored overrides)
  app.py               Assistant facade — wires everything together
  cli.py / __main__.py Command-line interface
  security/
    secrets.py         API keys/PINs in the OS credential store (never in repo)
    risk.py            Risk levels + the owner-confirmation gate
  providers/           Swappable AI brains (speak one neutral interface)
    base.py            LLMProvider interface + neutral message/tool types
    gemini_provider.py Gemini (default V1 brain)
    local_provider.py  Ollama on the RTX 5070 (offline fallback)
    registry.py        Picks the first available provider (primary -> fallback)
  actions/             What V.O.I.D can DO on the machine
    files.py           search / read / write / delete-to-Recycle-Bin (confined)
    apps.py            launch apps, open files/folders/URLs
    registry.py        exposes tool schemas to the brain, dispatches calls
  core/
    agent.py           the loop: plan -> act -> observe -> repeat, with retries
    task.py            Task model + SQLite checkpoint store (resumable)
    kill_switch.py     "VOID, STOP EVERYTHING" — in-process + cross-process file
  ui/
    widget.py          minimal always-on-top circular widget (PySide6)
  vault/               the owner's own markdown notes, retrievable and cited
  focus/               the accountability timer, and the identities it never stores
  teardown/            website teardown: a clean browser, REA, and a cited note
  preflight/           the live-chain harness behind `python -m void preflight`
```

### The notes vault

Point V.O.I.D at a folder of markdown notes and it can answer from them and say which note each
answer came from. Set it in `config/local_config.yaml` (never the tracked default):

```yaml
vault:
  notes_root: "C:/Users/you/Documents/Notes"
```

The root must sit inside `security.allowed_roots`; it is read through the same confined file layer as
every other file access. With no root set, the notes tools answer honestly that none is configured
rather than guessing where your notes are.

The property that matters most: when the notes genuinely do not cover a question, the vault returns
**no excerpts at all** and says so, instead of handing the model its best six near-misses to
improvise from. See `void/vault/__init__.py`.

### Receipts, or it didn't happen

Before an answer written by a model reaches you, it is screened against this turn's tool receipts. If
it claims V.O.I.D just sent, called, booked, paid, posted or deleted something and no tool in that
turn produced that effect, the sentence is replaced with an honest one and the original is kept in
the task's message history. Five of those six claims can never be backed, because V.O.I.D has no
capability that sends, calls, books, pays or posts. See `void/orchestration/judge.py`.

### Changing which model thinks

```
"which model are you using"        -> the exact id and provider
"switch to gemini 3.5 flash"       -> swapped, for this process only
"try on opus 5.5"                  -> refused, with the list of what IS available
"go back to your normal brain"     -> back to the configured model
```

A name that does not land on an exact id V.O.I.D knows about is **refused**, never approximated — a
loose matcher that keeps "opus" and throws away "5.5" costs you an hour of testing the wrong brain.
The known set comes from your configuration, so adding a model you have verified is one line
(`llm.known_models`). Swaps are runtime only: a restart always returns to the configured model.

### Focus sessions

```
"thirty minutes on this"            -> starts a session
"I'm working on the thumbnail sprint"  (the answer to "what are we focusing on?")
"give me fifteen seconds"           -> quiet
"it's okay, I'm doing research"     -> excuses the detour and refunds its seconds
"lock on this tab"                  -> move the target to where you are now
"how long is left"                  -> the countdown, on target minutes, detours
"end the session"                   -> the report card
```

It observes and accounts; it enforces nothing — there is no capability in the layer that can close,
block or hide a window. A session started while you are looking at V.O.I.D locks **nothing** until
you settle somewhere else, because that is the one window you are guaranteed to leave.

The privacy property is structural, not a promise: the name of the site or application you drift into
is read, turned into a sentence, spoken, and dropped. Neither the session state nor the ledger has a
field that can hold it — not even a hash — and the tests prove it with a canary label that appears
nowhere else in the repository. `focus.name_the_distraction: false` derives no label at all. See
`void/focus/__init__.py`.

The tab lane needs `browser.enabled`; without it a session locks the application only and says so. If
the foreground cannot be read at all, the session is `blind`: it counts nothing and accuses you of
nothing.

### Taking a website apart

Off by default. Turn on `teardown.enabled`, then:

```powershell
python -m void teardown diag                 # can it run here, and what is missing
python -m void teardown install              # installs the pinned kit, on purpose, verifying it first
python -m void teardown run igloo.inc --question "how does the scroll work?"
```

It opens a **brand-new browser profile** with none of your logins in it, lets REA watch the page for
seconds while V.O.I.D scrolls it, reads the code and the browser's own asset log, names the stack from
licence banners (which survive minification, so the versions are exact rather than guessed), writes a
note into your vault, and deletes the profile. Every line of evidence says who saw it — REA, V.O.I.D's
own browser read, or both — and the note ends with what the evidence *cannot* show.

V.O.I.D never installs the kit by itself: `teardown install` is your deliberate act, it checks the
package's repository URL before fetching anything, and it passes `--ignore-scripts` so no lifecycle
script of a third-party package runs on your machine. The observation is read-only by construction —
the DevTools client has no click, no typing and no navigation.

Point it at public sites, open-source code and your own projects.

### Security & autonomy (V1)

- **File operations are confined** to `security.allowed_roots` in the config
  (default: your home folder). V.O.I.D cannot act outside them.
- **Deletes go to the Recycle Bin**, never a hard unlink — recoverable.
- **Risk gate:** low/medium actions run autonomously; high-risk actions
  (deleting files) require your confirmation. When run unattended with no way
  to ask, high-risk actions are refused, not guessed.
- **Emergency stop** is checked before every step and every tool call, and can
  be triggered from another process via a stop-file (the seed of cross-device
  stop later).
- **Secrets** live in the Windows Credential Manager via `keyring`, never in
  the repo or plaintext config.
- **V.O.I.D's own state is owner-only.** The state directory is created 0700 and
  every file in it 0600 (`void/security/ownership.py`), re-applied at every
  start so an installation created before that was true is repaired. The task
  database holds your goals and sentences in plain text; `void preflight` checks
  the modes and fails loudly when they are wrong.

### Configuration

Defaults live in `config/default_config.yaml`. To override anything, create
`config/local_config.yaml` (gitignored) — e.g. to change the Gemini model,
the allowed roots, or to require a stop PIN.

---

## What is tested vs. what needs the laptop

Run the suite: `python -m pytest`

**Reproducible environment (V2.0).** The suite is validated with CPython 3.14 and the exact
package versions in `requirements.lock` (a read-only freeze of the tested environment;
`requirements.txt` stays the minimum-version source of truth):

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.lock
    .venv\Scripts\python.exe -m pytest --junitxml=junit.xml
    .venv\Scripts\python.exe scripts/check_test_guard.py --junit junit.xml

The tests are hermetic (in-memory keyring, sandboxed home directory, no network, no model
download, no microphone), so they never touch your real `~/.void` or Credential Manager.
`scripts/check_test_guard.py` fails if any of the 883 V1 baseline tests stops being
collected, if the test count drops, or if skips/xfails exceed the ceilings in
`tests/guard_limits.json`. CI (`.github/workflows/tests.yml`) runs the same steps on
`windows-latest`; it has not yet run on GitHub.

**Covered by automated tests (platform-agnostic, 56 tests passing):**
file search/read/write/confinement, delete-to-trash, risk gate, task
checkpoint/recovery, kill switch (incl. cross-process file + PIN auth),
provider selection/fallback, Gemini & Ollama message/tool translation, and the
full agent loop (tool execution, checkpointing, risk denial, stop, step cap)
driven by a scripted fake brain.

**Needs validation on your Windows laptop (not runnable in headless CI):**
the live Gemini call (needs your key + network), the local Ollama call (needs
Ollama running), Windows-native app launching / `os.startfile`, and the PySide6
widget (needs a display).

Nothing here claims to work that wasn't run. The offline demo exercises the
entire loop end-to-end today; the live pieces above are the ones to confirm
on the machine.
