# Response timing: the missing-application path

Date: 2026-09-27 · Base commit 1b1af96 (working tree, uncommitted)

## Root cause

**"Open Notepad++" took minutes because a fast-path miss entered the agent loop, which then made up to 12 model
calls looking for an application it had no way to find.**

Reproduced with the real `Assistant` and a stubbed model that behaves the way Gemini does on this request (reaches
for `find_app`, gets nothing, tries again):

```
Open Notepad++  ->  fast path: why='unknown'
                ->  agent loop: 12 model calls
                ->  "Reached max_steps (12) without finishing."
                ->  "The task failed. Please check the command line for more details."
```

At the owner's own measured Gemini latencies (successful calls p50 **6.88 s**, p90 **37 s**) those 12 calls are
**1.4 minutes at p50 and 7.4 minutes at p90** — which brackets the reported 3–5 minutes, and the error message is
exactly the one reported.

Every one of those calls was foregone work. The model's `find_app` tool reads the **same `AppCatalog`** the
resolver had just searched, so it cannot find an application the resolver could not. The delay was not Gemini
latency, retry policy, backoff, shell invocation or process launch — it was routing a question to something with
no additional ability to answer it.

Classification: **architecture** (routing), not provider, network, timeout or environment.

## What changed

| file | change |
|---|---|
| `void/core/fast_path.py` | `_not_found()` reply; `_is_about_owner_content()` guard; `answer_unknown` flag on `FastPath` |
| `void/app.py` | passes `fast_path.answer_unknown_apps`; distinguishes `not_found` from `clarify` in telemetry |
| `void/perf/schema.py` | `route.kind` gains `not_found` |
| `config/default_config.yaml` | `fast_path.answer_unknown_apps: true`, documented with the measurement |
| `tests/test_missing_app.py` | new, 27 tests |
| `tests/test_fast_path.py` | the two tests that pinned "unknown → model" split to match the new contract |

When a sentence is unambiguously `open/launch/start <name>` and **no tier of the resolver matches**, V.O.I.D says
so itself: *"I can't find Notepad++ on this machine. If it is installed under a different name, tell me that
name."* No model call, no agent loop.

### The guard that keeps this honest

An early version of this broke two existing memory tests, and they were right to break. `"Open my V.O.I.D
project"` and `"Open the project folder"` also parse as launch commands and also match no installed application —
but they are about the owner's **own content**, which the model genuinely can handle with `find_directory` and
`open_path`. Answering "I can't find that" would have removed a real capability.

So the local answer is withheld when the phrase belongs to somebody (`my`, `our`, `your`, `that`, …) or names a
container (`project`, `folder`, `file`, `notes`, `report`, …). Those still go to the model. Both word sets are
pinned by tests that isolate each one — emptying either is caught.

Also unchanged, deliberately: a failed **discovery** never claims an application is missing (if the catalog could
not be built, V.O.I.D does not know what is installed), and an **excluded** administration console still goes to
the model rather than being reported as absent.

## Before → after

| | before | after |
|---|---|---|
| `Open Notepad++` (not installed) | 12 model calls, **1.4–7.4 min**, `failed` | **0 model calls, 0.2–0.4 ms**, `completed` |
| agent steps for a missing application | 12 | **0** |
| Gemini calls for a local command | 12 (missing) / 0 (installed) | **0 / 0** |

## Measured matrix

Real `Assistant`, real catalog, model stubbed to answer instantly so the numbers are routing only:

**A — installed applications, all local, 0 model calls**

| command | ms | reply |
|---|---|---|
| Open VS Code | 22.6 | Opening VS Code. |
| Open WhatsApp | 10.3 | Opening WhatsApp. |
| Open File Explorer | 10.8 | Opening File Explorer. |
| Open Opera GX | 121.6 | Opening Opera GX Browser. |
| Open ChatGPT | 10.6 | Opening ChatGPT. |
| Open Windows Terminal | 10.0 | Opening Terminal. |
| **Open terminal** | **8.5** | Opening Terminal. |
| Open Notepad | 31.1 | Opening Notepad. |

**B — missing applications, all local, 0 model calls**

`Open Notepad++` 0.2 ms · `Open Photoshop` 0.4 ms · `Open Spotify` 0.2 ms · `Open Sublime Text` 0.2 ms

**C — still reaches the model, 1 call each**

`Explain ARP.` · `What is the TCP three-way handshake?` · `Summarise the notes in my workspace` ·
`remind me to buy milk`

Total across the whole matrix: **4 model calls — exactly the four that need one.**

## Windows Terminal and ChatGPT

Both already resolve, and this milestone confirms it rather than changing it. `Open terminal` → `Terminal`
(the Store app, by AppUserModelID) in **8.5 ms**; `Open Windows Terminal` → the same entry in 10.0 ms via the
vendor-qualifier tier. `Open ChatGPT` → `OpenAI.Codex_2p2nqsd0c76g0!App` in **10.6 ms**, no browser automation
and no shell. Neither is a hardcoded path: both come from the shell `AppsFolder` enumeration.

## Provider policy — unchanged

The Gemini→Ollama path was measured and fixed in the previous milestone
(`docs/PROVIDER_FALLBACK_2026-09-24.md`) and is untouched here. Its retry policy remains
evidence-supported: 10 of 37 first-attempt failures were rescued by retrying Gemini, and successful Gemini calls
have p90 37 s, so neither removing the retry nor shortening the 30 s deadline is justified.

**No caching was added.** A negative cache was considered and rejected as unnecessary: resolution is already
0.02 ms, and the rebuild-on-miss path is rate-limited to a 6 ms fingerprint check. There is nothing expensive left
to cache, and a negative cache would risk exactly the staleness the brief warns about.

## Security

No `shell=True`, `eval`, `exec` or `subprocess` in any changed file; no credentials (the only key-shaped match is
the telemetry schema's comment about *rejecting* key-shaped strings). The new path launches nothing at all — it
returns a sentence — so it cannot reach RiskGate, a shell, or a path. Speech never becomes an executable
argument. RiskGate, the kill switch and the protected-root controls are untouched and their tests pass.
Five mutations of the new logic were injected and all five were caught.

## Limitations

* **An application that is installed but not discoverable is now reported as missing.** If something lives outside
  the Start Menu, App Paths, the PATH aliases and the Store `AppsFolder`, V.O.I.D will say it cannot find it
  instead of asking the model to try. The model had no better discovery route — but it could previously have
  guessed a web URL, and it no longer gets the chance. `fast_path.answer_unknown_apps: false` restores the old
  behaviour.
* The possessive/container word lists are English and hand-written. They are small and deliberately biased toward
  sending things to the model, but a phrase like `"open alpha"` meaning a folder would be answered as missing.
* The 3–5 minute figure was reproduced with a **stubbed** model at measured latencies, not by spending live quota
  on a 12-call loop.

## Do not attempt again

Sending an unresolvable application name to the model in the hope it finds something. It uses the same catalog;
the outcome is fixed and the cost is minutes.
