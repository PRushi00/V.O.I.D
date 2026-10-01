# Response-timing audit

Date: 2026-09-28 · Base commit 1b1af96 (working tree, uncommitted)

The audit's central question — *do deterministic local commands reach a model?* — is answered **no**. Routing was
already correct and was not changed. What measurement found instead was avoidable work in catalog construction and
a redundant filesystem scan, plus one concurrency defect that produced a *wrong answer* rather than a slow one.

`scripts/bench/response_bench.py` reproduces everything below.

## 1. Baseline

Providers are counting fakes that answer instantly. That is deliberate: "does a local command reach a model" is a
routing question, and real provider latency is not something V.O.I.D controls (measured separately in
`docs/PROVIDER_FALLBACK_2026-09-24.md`: Gemini p50 6.88 s, Ollama warm ~2 s). Running this matrix against live
Gemini would spend the owner's quota to learn nothing new.

**First measurement — and it was wrong.** Running "Open VS Code" 24 times really started VS Code 24 times, so the
numbers were mostly Windows' process creation and a dozen editors opened on the machine. The benchmark now
**records** launches instead; real launch cost belongs to the OS and is reported separately via `--real-launch`.

| category | p50 | p90 | p95 | max | model | Ollama | failed |
|---|---|---|---|---|---|---|---|
| LOCAL actions (8 commands × 6) | 13.4 ms | 49.2 | 51.9 | 102.0 | **0** | **0** | 0 |
| MISSING application | 0.6 ms | 0.7 | 0.7 | 0.7 | **0** | **0** | 0 |
| INFORMATIONAL (3 × 6) | 26.8 ms | 29.4 | 70.4 | 70.4 | 18 ✓ | 0 | 0 |
| CONVERSATIONAL | 23.9 ms | 26.1 | 26.1 | 26.1 | 6 ✓ | 0 | 0 |
| REPEATED (one aliased command × 24) | 53.7 ms | 57.4 | 60.1 | 61.0 | 0 | 0 | 0 |

Cold/warm, fresh process:

| | |
|---|---|
| `import void.app` | 455 ms |
| `Assistant()` | 718–782 ms |
| **first catalog build** | **1578 ms** |
| first local command (cold) | 1632–2236 ms |
| same command (warm) | 49 ms |
| **`Assistant.run`, launch recorded** | **6.9–7.3 ms** |

So V.O.I.D's own per-command work is about **7 ms** for a local action and about **21 ms** of overhead on the
reasoning path. Everything else in the earlier numbers was the operating system starting programs.

## 2. Bottlenecks found, ranked by measured impact

| # | finding | measured | verdict |
|---|---|---|---|
| 1 | Concurrent callers each ran a **full** discovery | 4 threads → **4 discoveries** (~1.6 s each) | fixed |
| 2 | A rebuild was observable **half-done** | 1 wrong "not found" per ~346 rebuilds | fixed |
| 3 | `shutil.which` called **twice** per alias candidate | 19.6 ms per PATH scan → ~39 ms per aliased launch | fixed |
| 4 | Local commands reaching a provider | **0 of 48** | already correct |
| 5 | Per-command engine work | ~7 ms | nothing to gain |
| 6 | Cold start (~2.8 s) | prewarmed on a background thread at start-up | not per-command |

### Finding 2 is a correctness bug, not a latency one

`_build` published `self._entries` **before** clearing and repopulating the lookup indexes. A reader arriving in
that window saw a complete-looking catalog with an empty index. Since the previous milestone answers an
unresolvable name locally, that lookup does not merely take longer — it says *"I can't find WhatsApp on this
machine"* about an application that is installed. One occurrence in 346 forced rebuilds, and the variant observed
reported `ambiguous` (a duplicated half-built index), which would have said *"I found more than one match"*.

The window is the index-population loop, roughly 0.2 ms — which is why it is rare, and why it is pinned
**structurally** rather than by timing: a test that fails one run in 346 is a flake, not a guard.

## 3. Changes

| file | change |
|---|---|
| `void/actions/computer.py` | `_build` builds indexes into locals and publishes `_entries` **last**; an `RLock` serialises builds so a second caller waits for the one in flight; the TTL refresh double-checks under that lock; `invalidate` takes it |
| `void/actions/apps.py` | one `shutil.which` per alias candidate instead of two |
| `tests/test_response_timing.py` | new — 11 tests |
| `scripts/bench/response_bench.py` | new — the reproducible matrix above |
| `tests/guard_limits.json` | floor 2324 → 2335 |

Reads are deliberately **not** locked: the fast path is 0.02 ms and must stay that way. Publishing the index
atomically is what makes lock-free reads safe.

No routing change, no cache, no new dependency, no sleep, no provider or security change.

## 4. After

| category | p50 before | p50 after | change |
|---|---|---|---|
| LOCAL actions | 13.4 ms | **11.6 ms** | −13 % |
| **REPEATED (aliased command)** | **53.7 ms** | **33.5 ms** | **−38 %** |
| MISSING application | 0.6 ms | 0.6 ms | — |
| INFORMATIONAL | 26.8 ms | 31.5 ms | run-to-run noise; no code on that path changed |
| CONVERSATIONAL | 23.9 ms | 32.0 ms | same |
| concurrent discoveries | 4 | **1** | −75 % of startup work |
| wrong "not found" during rebuilds | 1 in ~346 | **0** | — |

Provider calls, unchanged and verified: **local 0 model / 0 Ollama** (48 executions), missing application
**0 / 0**, informational and conversational exactly one model call each.

The informational rows moving *up* is measurement noise on the fake-provider path — nothing there was touched, and
the difference is within the spread of repeated runs. It is reported rather than explained away.

## 5. What was deliberately not done

* **No change to routing.** The audit's premise was that local commands might be reaching the cloud. They are not.
* **No cache of PATH lookups.** Halving the scans is safe; caching them risks answering from a stale PATH after an
  install. 20 ms did not justify that.
* **No change to cold start.** The 1.6 s catalog build is already moved off the first command by the start-up
  prewarm; the remaining ~1.2 s is Python import plus `Assistant()`, paid once when the runtime starts.
* **No speculative work, no new worker, no endpointing or STT change** — all previously measured and rejected.

## 6. Security

RiskGate, the kill switch, protected roots, AUMID validation, the shell restrictions and application resolution are
untouched; 65 of their tests pass. Nothing was made faster by skipping a check: the lock only prevents *duplicate*
discovery, and the publication change only makes a complete index visible instead of a partial one. No `shell=True`,
no `eval`, no credentials in code, tests, benchmark or this document. The benchmark uses fake providers and
therefore no API key.

## 7. Limitations

* Provider latency in the matrix is **faked**. Real Gemini and Ollama timings come from the earlier fallback
  milestone and were not re-measured here, to avoid spending quota.
* Finding 2's window is ~0.2 ms and probabilistic; its guard is a source-order assertion plus a probabilistic
  test. A different refactor of `_build` could satisfy the assertion while reintroducing the hazard — the comment
  explains the invariant for that reason.
* `--real-launch` was not run as part of the reported matrix, so OS process-creation cost per application is not
  tabulated here (it was 8–120 ms in earlier milestones).
* All measurements are from one machine.

## 8. Remaining bottleneck

For a local voice command the engine's share is now **~7–12 ms**. The user-perceived wait is dominated by
endpointing (~0.45 s, evidence-bound across two milestones) and then by the operating system starting the
application. There is no further meaningful latency in V.O.I.D's own path.

## 9. Recommended next milestone

Stop optimising latency — it is measured out. The two leads worth picking up are both reliability rather than
speed:

1. **The speech-to-text tail.** 13 of 192 real decodes took 0.5–3.6 s, clustered in one period and independent of
   audio length, which points at GPU contention with Ollama (6.19 GB of an 8.15 GB card). Recording GPU
   utilisation in the telemetry would confirm it and turn a guess into a policy.
2. **Transcript accuracy on quiet speech**, still the largest source of *failed* commands: the phonetic resolver
   only rescues errors that are phonetically faithful, and Silero has been observed discarding whole utterances.
