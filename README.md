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
```

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
