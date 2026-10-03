# Reaching a person: "open Rushi's chat"

How V.O.I.D goes from a sentence naming a *person* to a conversation on screen — and why there is no
WhatsApp in any of it.

## 1. The shape of the problem

The request names a person and a kind of thing. It names no program:

```
"open Rushi's chat"  ->  which application?  ->  which conversation inside it?  ->  is it open already?
```

So three separate questions have to be answered before anything happens, and getting any of them
wrong is not a small error. Opening the wrong person's conversation puts someone's private
correspondence on screen.

## 2. What is not in the implementation

There is **no application-specific code anywhere**. A messaging application is a row in one table:

```python
MessagingApp("whatsapp", "WhatsApp", ("whats app", "whatsapp desktop"),
             ("whatsapp.exe", "whatsapp.root.exe"))
```

canonical name, how the owner might say it, and the process names the window layer reports. Six rows
ship (WhatsApp, Telegram, Signal, Slack, Discord, Microsoft Teams) and **not one of them has a
method, a branch or a selector of its own**. Teaching V.O.I.D another messenger is a row.

This is the same arrangement as `void/browser/playwright_adapter._BROWSERS`, which is how "Edge" is
recognised without a branch for Edge. A test asserts the property directly: it walks every `.py` file
under `void/` and fails if an application name ever decides control flow.

`whatsapp.root.exe` is in the table because that is what the desktop build actually reports on the
owner's machine — observed, not guessed.

## 3. Choosing the application is the existing precedence chain

No new policy was invented. Highest first:

| | signal | source |
| --- | --- | --- |
| 1 | the application named **in this utterance** | `void/orchestration/overrides.py` |
| 2 | **observed state** — the conversation is already open in exactly one app | the window layer |
| 3 | the owner's stored **`messaging` preference** | `state.sqlite` |
| 4 | the only installed messenger, when there is exactly one | the application catalog |
| 5 | otherwise **ask** | — |

`messaging` became an overridable role by *gaining a vocabulary*, not by gaining a code path — so
"open Rushi's chat in Discord" travels through the same extractor as "open Gmail in Edge".

Two utterance forms are understood, and both had to be, because they are not the same grammar:

| utterance | app | how |
| --- | --- | --- |
| `Open Rushi's chat in Discord` | discord | the existing "in X / with X / using X" extractor |
| `Open Rushi's WhatsApp chat` | whatsapp | attributive: a vocabulary word *immediately* before a conversation noun |
| `WhatsApp is slow, open Rushi's chat` | — | a mention is not a choice, exactly as for browsers |
| `Open Rushi's chat in Telegram` (not installed) | — | reported by name; **no substitution** |

Step 4 is the one that keeps V.O.I.D from being irritating: with a single messenger installed there
is no choice to make, so it does not manufacture a question. Step 5 is the one that keeps it safe.

### Ambiguity is settled before any route exists

`RouteResolver` deliberately treats two equally-scored routes of the *same kind* as not worth a
question — true for two ways of opening a web page, false for WhatsApp versus Discord. So the
messaging planner settles that choice itself and proposes **no route at all** when the answer is
"ask". The resolver was not modified.

## 4. The route, and why the contact is an argument

```
plan                 route kind        calls
------------------   ---------------   ----------------------------------
REUSE                EXISTING_STATE    focus_window(window=<handle>)
NAVIGATE / LAUNCH    DESKTOP_UI        open_conversation(contact=…, app=…)
```

A reuse route is `EXISTING_STATE`, so it beats opening something new for the same reason an already
open Gmail tab beats launching a browser.

`routes.py` says *"no caller text becomes an argument"*, and `contact` is the one exception in the
codebase. It has to be: there is no way to open Rushi's chat without the string "Rushi". It is
cleaned, bounded to 60 characters, and used **only as a match needle against accessible names the
application itself published** — never interpolated into a command, a path or a shell. That is
exactly what the reference resolver already does with a qualifier.

Both route kinds require the `desktop_ui` capability, which is new: `desktop` was already "can see
and activate windows", and `desktop_ui` is "can reach inside one". With `desktop.enabled: false`
(the shipped default) the route is dropped **before** selection, so the honest answer arrives
instead of a mid-way failure.

## 5. Opening is not sending

The guarantee is structural, not a promise:

- the only control ever clicked is one whose accessible name **names the contact**, read from the
  live control tree;
- that name is put through the shared confirmation boundary
  (`void/security/consequential.is_consequential`) and refused if it looks like it sends, submits,
  pays or deletes — so a row labelled "Message Rushi" is refused and said so;
- a **call**-shaped control is refused too. Clicking "Video call" makes a phone ring, which is not
  what "open the chat" asked for. This is an *additional* local refusal rather than a widening of
  the shared vocabulary, because adding "call" there would change confirmation behaviour for the
  browser and desktop layers as a side effect of adding messaging;
- **there is no typing.** No composer is filled, no Enter is pressed, and `type_into_control` is not
  reachable from this tool. The fake desktop used in tests raises if anything tries.

Every outcome carries `"sent": False`, including the successful ones.

Two more refusals worth naming. A **sign-in or QR-link screen** is detected and reported — the tool
never reads, stores or supplies a credential and never tries to get past authentication. **Two
contacts matching the same name** stops and asks rather than picking; `mentions()` is whole-word in
both directions, so "Ann" never matches "Joanna".

## 6. Verification, honestly

The window's own title is the only *independent* evidence that the right conversation opened. An
earlier version also accepted "the control I clicked was named like the contact", which is worthless
— that control was chosen *because* it names the contact, so the check was always true. Re-checking
what you already knew is precisely the `action succeeded == task succeeded` mistake
`void/orchestration/verify.py` exists to break.

Many messengers keep a constant window title whatever conversation is open, so **"could not
confirm" is the ordinary answer rather than a rare one**, and it is reported as such. Unverified is
not failed.

## 7. A wrong-answer bug this work uncovered

`"Open Rushi's chat"` used to resolve — with no ambiguity reported — to a PowerPoint V.O.I.D had
just produced. The arithmetic in `reference.py`: the kind mismatch cost `W_KIND/2` (−20), recency
paid +20 and "I produced it" +14, the total was positive, and **a lone positive candidate is
chosen**. A request for a person's conversation returned a revenue deck.

The flaw was treating recency as *identification*. `identifies()` now separates two things:

- **Pointing** — "this", "that", "the one I was just looking at" — indicates something by its
  presence. Recency and foreground are the right evidence, and anything recent is eligible.
- **Describing** — "Rushi's chat" — names properties. At least one must actually match: the kind,
  the qualifier, or the noun. Nothing matching means V.O.I.D has not found what was described and
  should say so.

Deixis still works; a description that matches nothing now returns nothing.

## 8. What was validated, and how

**Real machine** (WhatsApp, Discord and Microsoft Teams genuinely installed; WhatsApp running):
catalog resolution of installed messengers; UI Automation reading WhatsApp's live control tree;
`"Open Rushi's chat"` correctly asking which of the three; `"Open Rushi's WhatsApp chat"` resolving
to NAVIGATE because WhatsApp was already running; `"in Telegram"` failing accurately because
Telegram is genuinely absent; the tool finding the real window, **reusing it rather than launching**,
activating it, reading it, and reporting "could not find Rushi" without clicking anything.

**Deterministic harness**: the click-through, launch-then-navigate, ambiguity between two contacts,
the send and call refusals, sign-in detection, and the unverified-open path.

**Environment-limited**: WhatsApp exposes 116 named controls to UI Automation and **none of them is
a chat-list row** (not truncation — the read completed). So a real contact row could not be clicked
on this machine. Reaching one would mean typing into the application's search field, and typing is
deliberately outside this tool's reach. Recorded as a limitation, not worked around.
