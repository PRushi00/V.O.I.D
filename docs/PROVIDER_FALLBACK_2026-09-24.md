# Gemini → Ollama fallback: why it was slow

Date: 2026-09-24 · Base commit 1b1af96 (working tree, uncommitted)

## Root cause

**Neither the retry policy nor the model was the problem. It was a hostname.**

`llm.local.base_url` was `http://localhost:11434`. On a default Windows install `localhost` resolves to the IPv6
`::1` before IPv4, and Ollama listens only on IPv4. Every request therefore opened a connection to `::1`, waited
for it to fail, and only then retried on IPv4 — succeeding with status 200, roughly two seconds later.

Measured, same machine, same Ollama, only the hostname changed:

| | `localhost` | `127.0.0.1` |
|---|---|---|
| `available()` readiness probe | **2110 ms** | **16 ms** |
| `generate()` | **4.32 s** | **2.17 s** |

A second, compounding fault: `ProviderRegistry.available_order()` probed **every** provider before **every** run,
so a request Gemini answered perfectly well still paid the 2 s Ollama probe. The fallback path paid it twice —
once in selection, once in generation.

## Changes

1. **`_prefer_ipv4()` in `local_provider.py`** — rewrites the exact hostname `localhost` to `127.0.0.1`,
   preserving scheme, port, path and any URL credentials. `127.0.0.1`, an explicit `[::1]`, a LAN host and a
   remote URL are all left untouched, so this cannot redirect traffic anywhere the owner did not choose. The
   shipped default is now `http://127.0.0.1:11434`; the rewrite exists because `localhost` is what every Ollama
   document tells you to paste.
2. **`available_order()` stops probing at the first usable provider.** Fallbacks are returned unprobed. This is
   only safe because an unavailable provider raises `ProviderUnavailable`, which the existing classifier already
   turns into "move on" — and a test pins exactly that.

Nothing else changed: no new dependency, no new thread or process, no change to the retry policy, the failure
classifier, RiskGate, the application fast path, or voice.

## Before → after

Same script, same prompt, real Ollama, Gemini 503 injected:

| | before | after |
|---|---|---|
| `registry.available_order()` (every request) | 2034.6 ms | **0.4 ms** |
| `local.available()` | 2021 ms | **15 ms** |
| Ollama `generate()` | 4.32 s | **2.17 s** |
| **Gemini 503 → Ollama answer** | **7.92 – 9.51 s** | **3.93 – 4.12 s** |

The 2 s selection tax was also being paid by every **successful** Gemini request; that is gone too.

## Scenario matrix

`scripts/bench/fallback_bench.py`, median of 3, distinct prompts per repetition (an identical prompt lets Ollama
reuse its KV cache and reports several times faster than reality):

| scenario | total | Gemini attempts | outcome |
|---|---|---|---|
| 503 → warm Ollama | 1.68 s | 2 | completed |
| timeout → warm Ollama | 1.99 s | 2 | completed |
| 429 → warm Ollama | 0.79 s | **1** | completed |
| auth failure → warm Ollama | 0.98 s | **1** | completed |
| quota exhausted → warm Ollama | 0.88 s | **1** | completed |
| invalid request | 0.01 s | 1 | **failed, never forwarded** |
| 503, no fallback configured | 3.01 s | 3 | failed |
| 503 → Ollama unreachable | 3.06 s | 2 | failed cleanly |
| 503 → model not installed | 1.02 s | 2 | failed cleanly |
| **503 → COLD Ollama** | **4.81 s** | 2 | completed |
| 503 → warm again | 1.56 s | 2 | completed |

Classification costs 2–21 µs — it was never a factor.

## Gemini policy — unchanged, and here is the evidence for that

The obvious move was to cut retries. **The telemetry says don't.** Of 37 interactions where the first Gemini
attempt failed and a retry followed, **10 were rescued by retrying the same provider**. Retrying is worth its
one second: the alternative is demoting a quarter of transient failures to the weaker local model.

The second obvious move was to shorten Gemini's 30 s deadline so failures surface faster. **The telemetry says
don't do that either.** Successful Gemini calls have p50 6.88 s, **p90 37 s**, p95 61 s — 18 of 38 took longer
than 8 s. An 8–12 s deadline would abort successful work to make failures look faster. That is a bad trade and it
was rejected.

So the policy stands: transient (5xx / timeout / network) gets one bounded retry with a 1 s backoff, then fails
over; auth, quota, rate-limit and "provider says unavailable" fail over immediately; an invalid request is never
forwarded. On the last provider in the chain the full retry budget is restored, because there is nowhere to go.

## Ollama policy — warm-keeping evaluated and rejected

| | |
|---|---|
| cold generate (model not resident) | 3.03 s |
| warm generate | 1.94 – 2.35 s |
| **cold-start penalty** | **~1.1 s** |
| VRAM while resident | **6.19 GB of 8.15 GB** |
| free VRAM with the model resident | ~600 MB |
| Whisper STT needs | ~806 MB on the same card |

**No permanent warm-up was added.** Holding the model resident costs 76 % of the GPU and leaves less headroom
than the speech-to-text model already uses — it would contend with the voice pipeline and with anything else the
owner runs — to save about a second. The existing `keep_alive: 10m` already keeps it warm after any use, so
consecutive fallbacks are warm anyway; only the first fallback after ten idle minutes pays the ~1.1 s.

## Security

API keys, tokens and credentials appear nowhere in the changed files, and nothing new is logged. `base_url` is
used only to build request URLs — it is never logged and never placed in an error message (the failure path
reports the exception *class name* only), so a URL-embedded password cannot leak through it. No `shell=True`, no
`eval`/`exec`, no new thread, process or daemon. RiskGate, the kill switch and the application fast path are
untouched and their tests pass. No recordings or secrets were added to the repository.

## What is left, honestly

* **The dominant remaining term is Gemini's own failure latency**, not anything V.O.I.D controls: a failing call
  takes p50 2.77 s and p90 11.69 s before it reports failure. With one retry that is most of a real-world
  fallback. Bounding it would cost successful calls (see above); it is an external limitation and is recorded as
  one rather than worked around.
* **The 1 s retry backoff** could in principle be shorter, but there is no evidence about whether the 10 observed
  rescues needed the wait. Changing it would be guessing.
* The measurements come from **one machine**; the IPv6 behaviour in particular is Windows-specific, though the
  fix is harmless everywhere.
