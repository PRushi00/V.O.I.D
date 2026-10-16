# Entity routing: four kinds of thing, and the phrasing people actually use

Why "Open my Studies folder" took 41 seconds, why "Open YouTube" had no route at all, and why a
folder V.O.I.D had just found could not be found again.

## 1. The shape of every one of these bugs

V.O.I.D already knew the answer. Something stopped the question reaching the part that knew it, so
the sentence fell through to a model — which added latency, variance, and in one case a confident
claim that was not true.

| request | before | after |
| --- | --- | --- |
| `Open my Studies folder` | 41.0 s | **0.13 s** |
| `Open my Vibecoding folder` | 40.7 s | **0.04 s** |
| `Open the folder you just found` | "could not find the folder" | **0.16 s** |
| `open the vibe coding folder you found` | 16–26 s via the model | **0.25 s** |
| `Open YouTube` | failed / unreliable | **2.9 s, verified** |
| `Open YouTube in Opera GX` | failed | **1.7 s, verified** |
| `Open Wikipedia about AI` | sometimes a *false* success | **11.6 s, verified** |
| `Open my personal chat` | invented a contact called "Personal" | **asks whose, 0.08 s** |

All measured on the owner's machine, before and after.

## 2. Four entity kinds, not one

Treating every noun phrase as an application is the single mistake underneath most of this. The
resolver now has a provider per kind, and each one is a vocabulary rather than a branch:

```
APPLICATION  -> void/actions/computer.AppCatalog        (installed, Start-Menu, App Paths)
FOLDER/FILE  -> void/actions/folders.FolderCatalog      (bounded index, identity matching)
WEBSITE      -> void/orchestration/websites.WEBSITES    (a table row, or a hostname spoken aloud)
CONVERSATION -> void/orchestration/messaging.MESSAGING_APPS
```

Nothing new was built for applications or folders — both catalogs already existed and were already
fast. What was wrong was the grammar in front of them.

## 3. The launch grammar could not read the natural phrasing

`_LAUNCH` dropped only `the`, and its trailing-noun list held only `app|application|program`. So:

```
"open Studies"            -> studies            resolved in 0.01 ms
"open my Studies folder"  -> "my studies folder"  matched nothing -> model -> 41 s
```

Both are now handled, and the second reading mechanism that already existed for `whats app` does the
work: `open my Studies folder` yields **both** `studies` and `studies folder`, so a folder genuinely
called "Work Folder" is still reachable.

### A possessive still means "mine to find"

There was an existing rule, with a test: *"Something belonging to the owner is theirs to find, even
when no file-ish noun is present."* `open my dashboard` must reach the model. That rule and this fix
only look like they conflict — the deciding factor is in the rule's own wording:

| request | noun present? | outcome |
| --- | --- | --- |
| `open my Studies folder` | yes — `folder` | catalog resolves it |
| `open my dashboard` | no | the model owns it |
| `open that thing` | no | the model owns it |

So a possessive is **recognised** always and **dropped** only when an entity noun says what kind of
thing is meant. That is why the determiner is captured rather than skipped.

## 4. Websites had no route

A website is not an application, so the matcher correctly answered "nothing called YouTube is
installed" and the model was left to invent a URL. Meanwhile the browser layer worked the whole time:
`PlaywrightBrowser.navigate` loaded YouTube in 7.4 s and reported the title.

Two ways in, and deliberately no third:

1. a row in `WEBSITES` — canonical name, spoken aliases, URL, and an optional search URL so
   "Open Wikipedia about AI" becomes a search rather than a homepage;
2. something the owner said that is **already** a hostname — `open reddit.com`.

An unknown bare word yields nothing. Guessing `https://<word>.com` would let a mishearing send the
browser to a domain nobody chose.

### The TLD list is an allowlist, and that is the point

The first version accepted any 2–24 letter final label, which made `open notepad.exe` look like an
address — a filename turned into a destination by a parser. A denylist of file extensions cannot be
used instead: the project's own `fast_path._EXTENSIONS` contains **`com`**, which is both a DOS
executable suffix and the commonest TLD there is. Erring towards rejection costs nothing here; the
model simply keeps the sentence.

### "in Opera GX" is a separate decision

Which browser to use is already decided by `overrides.py` for the `browser` role, so the phrase comes
off before the site name is matched — otherwise "YouTube in Opera GX" looks like the name of a site.
`in this browser` / `in the current browser` name no particular browser and mean "wherever you would
normally put it", so the stored preference decides.

### Success means the browser says so

```python
verified, why = self._verify_page(target)   # compares HOSTS, read back from the browser
```

Hosts rather than whole URLs, because sites redirect and append parameters. When the browser cannot
say, that is reported as not confirmed — never as success. **This is the fix for the fabricated
"I have opened the Wikipedia page"**: with `browser.enabled: false` the route is dropped before
selection and the answer is `FAILED` with the reason, because no model is involved to claim otherwise.

## 5. A discovered folder is now remembered

`open_path` records every path it opens into the existing `RecentThings`, so the verified path is
available next turn:

```
turn 1  "Open my Vibecoding folder"          -> opens C:\Vibe Coding\VibeCoding
turn 2  "Open the folder you just found"     -> 0.16 s, no search, no model
```

It used to appear to work only while the Explorer window stayed open, because the window list offers
a candidate of its own; closing it lost the reference.

Two refinements were needed to make the follow-up phrasings land:

- **"you just found" is not a name.** Left in the filler set, those words became a *qualifier*
  nothing could match.
- **"VibeCoding" and "vibe coding" are the same folder.** `_name_score` gained the "same name spaced
  differently" tier, taken from `app_names.squash` — the definition the application and folder
  catalogs already share, not a third copy.

### A named thing must match by name

`Open my Flurbleglorp folder` offered "VibeCoding" and "Studies": two folders that matched the
*kind* and nothing else, scored on recency, and tied into a question about the wrong things. When the
owner names something, kind agreement alone is no longer identification.

Ambiguity between things of the right kind is answered with the resolver's own question, instantly,
rather than handed on — the model's version was "I cannot proceed without knowing which folder you
are referring to" after 15.8 s, which is the same question asked worse and slower.

## 6. "My personal chat" named nobody

It resolved to a contact called "Personal" and asked which application to find them in — V.O.I.D
inventing an identity out of an adjective. Describing words (`personal`, `private`, `work`, `main`,
`family`, …) are no longer contacts, so the answer is the minimum necessary question: *whose
conversation?* What "my personal chat" means is something only the owner can say.

`Open Rushi's WhatsApp chat` still fails — honestly, in 1.0 s: *"I could not find Rushi in WhatsApp's
visible list."* WhatsApp exposes 116 named controls to UI Automation and none of them is a chat-list
row. That is reported, not worked around.

## 7. Every terminal state says something

`task.result` was only ever set on COMPLETED, so a FAILED, PAUSED or CANCELLED task returned
`result=None` and vanished from the CLI and the widget. The voice session was saved by its own
constant phrases; every other surface was not. The engine's own `task.error` is now preferred when
there is one — "Reached max_steps (12) without finishing" is useful, "that did not work" is not —
and those strings are written by the engine, never by a model or a tool.

## 8. What was investigated and deliberately not changed

- **The two `pythonw.exe` processes are not a bug.** PID A is
  `C:\V.O.I.D\.venv\Scripts\pythonw.exe` whose `OriginalFilename` is `py.exe` — the CPython **venv
  launcher** — and PID B is its child, running the real interpreter at
  `...\Python314\pythonw.exe` with the models loaded (3.7 GB against the launcher's 1.9 MB). The
  parent of A is `svchost.exe`, i.e. Task Scheduler. One logical instance, standard Windows venv
  behaviour, and the single-instance mutex is held by the interpreter that owns the microphone.
  Nothing was changed.
- **Opera GX needs no path configuration.** It resolves through the Start-Menu `.lnk` and the
  un-versioned `opera.exe`, so the version directories (`136.0.6008.67`, `.76`) cannot destabilise
  it. No absolute path was hardcoded.
- **"GX browser" still does not resolve**, and that is deliberate — `app_names.py` says so in a
  comment: "Opera" is excluded from the vendor list because it is also the product, and stripping it
  would let "gx browser" resolve to a name the owner never said. `Opera GX`, `opera gx` and
  `Opera GX browser` all work.
- **No filesystem index was added.** `FolderCatalog` already scans 244 directories in 0.28 s and
  answers in 0.01 ms; the problem was never the index.

## 9. Why the same sentence worked typed and failed spoken

Typing "Open YouTube" worked and verified. Saying *"Hey V.O.I.D., open YouTube."* answered **"That
task failed, please check the cmd line for details."** Three separate causes, plus a deployment one.

### 9.1 The running process had none of this code

The voice runtime is long-lived: Task Scheduler started it at **16:00:01**, and
`void/orchestration/websites.py` was written at **19:40** the same day. Python imports a module once;
it does not hot-reload. So the live voice process was executing the code from before the website
route existed, and "open YouTube" fell through to the model exactly as it had.

**This is the first thing to check whenever voice and text disagree.** Nothing in the repository is
wrong in that state, and no amount of reading the code reveals it — only the process start time does:

```powershell
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Select ProcessId, CreationDate
```

The runtime is restarted through its own task, never by killing it:

```
schtasks /End /TN VOID_VoiceRuntime
schtasks /Run /TN VOID_VoiceRuntime
```

### 9.2 The address was being treated as part of the goal

Speech-to-text writes `Hey V.O.I.D., open YouTube.` The launch grammar had stripped that prefix for
itself since V1 — and nothing else had:

| parser | `Hey V.O.I.D., open this chart.` | why |
| --- | --- | --- |
| `fast_path` | correct | has `_PREFIX`, including the dotted spelling |
| `websites` | **no site at all** | knew the literal `void`, not `V.O.I.D.` |
| `reference` | qualifier `'hey v.o.i.d'` | kept the words, so a *named* thing matched nothing |

Typing never hit any of this, because nobody types "hey void". The prefix is now one public
definition — `fast_path.strip_address_prefix` — used by all three, for the same reason `squash` and
`bare_word` are shared: every parser sees the same transcripts, and a second copy would drift.

### 9.3 The engine's reason was discarded before it was spoken

`status_phrases.py` speaks a fixed phrase for the attention-needing statuses, and its security
reason is sound: text a model, tool, page or window produced must never be spoken, or whatever wrote
it gains a voice channel. But the engine had already composed *"I can reach YouTube, but browser
automation is switched off"* — and `session.py` threw it away for the generic phrase.

The rule is now **narrowed, not relaxed**. `AgentResult.engine_authored` is set only where every
part of the sentence came from engine constants, an engine vocabulary table, or the owner's own
words. Absence means untrusted, so anything older or third-party still gets the constant phrase, and
a tool summary never reaches the speaker. Four places set it; `_execute_conversation`'s tool text
deliberately does not.

One consequence worth stating: the unconfirmed-navigation message no longer names the host the
browser reported. That host is external text, so it stays on `task.error` for the command line and
out of the sentence the owner hears.

### 9.4 Verification was reading the wrong tab

`_verify_page` called `browser.read()`, which returns whichever page is **current**. With two tabs
open it compared the wrong one and declared a successful navigation unconfirmed — *"open Wikipedia
about artificial intelligence"* was reported unverified while the page was loaded, because a YouTube
tab from the previous command answered the read.

The `navigate` tool already reports `{"url", "title"}` for the page it produced. That is the
evidence now; `read()` remains the fallback for a route that reports no URL of its own, such as
activating an existing tab.

### 9.5 What was confirmed about the two processes, again

Unchanged from §8, re-measured: launcher 2.1 MB parented by `svchost.exe`, child 3.3 GB running the
real interpreter. The voice task's `Last Result: -2147020576` (`0x800710E0`, "request refused") is
what `MultipleInstancesPolicy: IgnoreNew` returns when the 15-minute recheck trigger fires while an
instance is already running — the duplicate-suppression working, not a failure.

## 10. Test state must not reach the real desktop

The owner saw V.O.I.D resolving
`C:\Users\nanda\AppData\Local\Temp\pytest-of-nanda\pytest-953\test_memory_then_filesystem_th0\work\aurora`
as if it were a folder on their computer.

**It was not a database leak.** The suite isolates those correctly - `make_assistant` points
`app.state_dir` at `tmp_path` - and a search of the production `state.sqlite`, `memory.sqlite`,
`tasks.sqlite` and `recent_apps.json` found **zero** occurrences of `pytest-of-nanda`. That is what
ruled persistence out and pointed at the desktop.

**It was the desktop.** `open_path` really called `os.startfile`, so the suite left genuine File
Explorer windows standing on pytest temporary folders. Reproduced precisely: before
`test_memory_then_filesystem_then_launch` no window matched "aurora"; afterwards there was one,
*"aurora - File Explorer"*, whose location was
`file:///C:/Users/nanda/AppData/Local/Temp/pytest-of-nanda/pytest-954/.../work/aurora`.

And the live runtime reads the window list as **observed truth**: `referents.window_candidates`
offers every open window as something the owner can refer to, and `WorldState.running_apps` is built
from the same list. A stray window from a test is therefore indistinguishable from a folder the owner
opened. It was caught happening - a reference question listed *"ZqParityProbe - File Explorer"*, a
pytest fixture folder, among the owner's real choices.

### The fix is a boundary, not a blacklist

An autouse fixture in `conftest.py` makes `os.startfile` and `webbrowser.open` inert for the whole
suite. A production guard that refused paths under `%TEMP%` was rejected: it would break a folder the
owner genuinely keeps there, it would not stop the next side effect that is not a path, and it would
leave the suite still reaching out of its sandbox. A test asserts that no production module mentions
any test-only name.

Two things are deliberately **not** patched, because they are code under test rather than a way out:

- `subprocess.Popen` - `subprocess.run` is built on it, and three tests spawn `tests/_memory_child.py`
  to prove memory survives a real separate process.
- `AppActions._default_launch` / `RealWindowsBackend.launch` - the first version patched these and
  broke `test_computer.py`, which patches `Popen` itself and then asserts the exact argv
  `_default_launch` builds. A launch cannot reach the desktop anyway without a resolved target from a
  real catalog, and a test checks that `launch_app` refuses everything else.

`@pytest.mark.real_os_launch` opts a test back in. It is currently unused.

**Measured after**: the full 4317-test suite leaves 0 windows naming a pytest path, 0 File Explorer
windows at all, and only the owner's own browser process.

## 11. Using the browser the owner already has open

Opera GX was running; "Open YouTube" worked, in a *second* V.O.I.D browser with none of their logins.

### Three approaches, measured before anything was written

| | result |
| --- | --- |
| **CDP attach** | Opera GX *does* speak CDP - with `--remote-debugging-port` and its own `--user-data-dir` it reported `Chrome/152.0.7977.120`, protocol 1.3. But a port **cannot be retrofitted onto a running browser**: relaunching with the flag only signalled the existing instance, the browser process was unchanged and 9222 stayed closed. An open CDP port also has no authentication - any local process gets the owner's cookies and sessions. |
| **Windows UIA** | **Not viable.** UIA finds the Opera window and reads **zero** elements; Chromium does not publish its accessibility tree by default. No address bar, no tab strip. |
| **OS default handler** | **Works.** Opera GX is the default `https` handler, and handing it the URL added only renderer processes - the browser process was unchanged. |

### What was built

The third, with precedence that keeps the strongest option first:

1. **CDP, when something is listening** - it can read the page back and confirm the host.
2. **The owner's own session** - hand the URL to the existing `open_path` tool, so the kill switch
   and `RiskGate` apply unchanged. No port is opened, no browser flag is changed, no profile is read.
3. **The controlled browser** - unchanged fallback, fully verifiable.

Reuse requires **both** that the shell would land in the intended browser (matched with `squash`
against the owner's own `UserChoice` ProgId - no second ProgId table) **and** that the browser is
already running. So "in Edge" while Opera is the default correctly does *not* reuse.

### Two measurement mistakes, both pinned as tests

- **"Lowest pid" is not "the browser."** Windows reuses pids, so a helper started later took a lower
  number and the check reported a brand-new session where there was none. The browser is the process
  with **no `--type=`** argument.
- **A new browser process is not a second browser.** Windows starts a transient `opera.exe` to carry
  the URL, which signals the running instance and exits - and for that moment it has no `--type=`
  either. Measured immediately it looked like a second browser *every time*. **Persistence** is the
  test, not existence.

### Honest about what is not known

V.O.I.D cannot read that browser, so the reply says so: *"Opened YouTube in the Opera GX you already
had open. I cannot read that browser, so I have not confirmed the page itself."* A persistent second
browser is reported as a failure of reuse rather than dressed up as success.

### Two defects found on the way

- **Observing the world opened a browser.** `world_state` read the tab list for every goal; `tabs()`
  runs `_ensure()`, which attaches or **launches**. That launch was the second Opera window, and it
  happened before any routing decision. Tabs are now read only when a session already exists or CDP
  is listening - a browser V.O.I.D has not opened has no tabs to report anyway.
- **An explicit "in Edge" never reached the launch.** `_ensure` resolves the browser from
  `policy.preferred`, the *configured* choice, so the override reached the routing decision and
  stopped there: Opera GX started while the reply said only "Opened YouTube." `prefer()` now carries
  it, and refuses to switch a session that is already open rather than closing the owner's window.

**Measured**: `Open YouTube` 1.95 s, reused, no new browser (renderers 20->25). `Open Wikipedia about
artificial intelligence` 1.47 s, reused. `Open YouTube in Edge` launched 12 `msedge.exe` processes and
**zero** new Opera browsers.

