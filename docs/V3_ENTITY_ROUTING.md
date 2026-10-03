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
