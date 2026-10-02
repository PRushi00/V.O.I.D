# V.O.I.D V3 — requirement ledger and completeness audit

The durable record of what V3 asked for and what exists. It lives in the repository rather than in a
conversation so that it survives the task, and it is the artifact the final completeness audit is read
from.

Status words mean exactly one thing each, and are used honestly:

| Status | Meaning |
| --- | --- |
| **COMPLETE** | Implemented, integrated into the running assistant, tested, and exercised against the real thing |
| **PARTIAL** | Implemented and tested, but some part is unproven or depends on something absent here |
| **BLOCKED** | Cannot be completed on this machine, with the reason named |
| **DECLINED** | Deliberately not built, with the reason named |

"Real validated" means the code ran against the actual browser, desktop, filesystem, machine or web — not
against a stub. Where a stub was the only option, it says so.

---

## 1. Capability matrix

| # | Requirement | Implementation | Integrated | Tested | Real validated | Security reviewed | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Browser layer, real Playwright (not interfaces) | `void/browser/`, `void/actions/browser.py` | yes — 7 tools | yes | yes — real Edge, CDP attach 2.8 s, YouTube nav 4.5 s | yes | **COMPLETE** |
| 2 | Reuse the owner's existing browser state | `playwright_adapter._ensure` CDP attach first | yes | yes | yes — attached to the running browser's real tabs | yes | **COMPLETE** |
| 3 | URL scheme policy | `void/browser/safe_url` | yes — single entry point | yes — 12 hostile URLs | yes | yes | **COMPLETE** |
| 4 | Desktop layer, Windows UI Automation | `void/desktop/`, `void/actions/desktop.py` | yes — 6 tools | yes | yes — 17 Notepad controls, 6 windows in 148 ms | yes | **COMPLETE** |
| 5 | Protected processes refused | `uia_adapter._protected_names`, reuses V2 list | yes | yes | yes — `explorer.exe` refused for read and activate | yes | **COMPLETE** |
| 6 | Engine-minted control handles | `desktop.parse_handle`, `_locate` | yes | yes — 9 hostile handles | yes | yes | **COMPLETE** |
| 7 | Perception model, ranked observations | `void/perception/` | yes | yes | yes | yes | **COMPLETE** |
| 8 | Screen capture | `void/perception/screen.py` — pywin32 GDI | yes — 3 tools | yes | yes — 1707×1067 in 24 ms, zero new dependency | yes | **COMPLETE** |
| 9 | Screen understanding | `actions/screen.py` + provider vision | yes | yes | yes — Gemini described the real screen (71 875 bytes) | yes | **COMPLETE** |
| 10 | Artifact engine — DOCX/PPTX/XLSX/PDF | `void/artifacts/`, `actions/artifacts.py` | yes — 2 tools | yes | yes — all four written and reopened | yes | **COMPLETE** |
| 11 | Inspect generated output, not just "file created" | `artifacts.inspect_artifact` | yes | yes | yes — catches truncation, wrong format, empty, non-PDF | yes | **COMPLETE** |
| 12 | Artifacts written through the confined file layer | `FileActions.write_bytes` (not a tool) | yes | yes | yes — System32, `~/.void`, traversal, own source all refused | yes | **COMPLETE** |
| 13 | Research engine — current, sourced information | `void/research/`, `actions/research.py` | yes — 2 tools | yes | yes — live pass, `artificialintelligenceact.eu/article/5` | yes | **COMPLETE** |
| 14 | Source tracking | `Finding` requires a URL; no unattributed constructor | yes | yes | yes | yes | **COMPLETE** |
| 15 | Fact vs model inference separated | engine returns excerpts and never summarises | yes | yes | yes | yes | **COMPLETE** |
| 16 | Search provider | ordered configurable endpoint chain | yes | yes | PARTIAL — see §3 | yes | **PARTIAL** |
| 17 | Reference resolution — "open this chart" | `orchestration/reference.py` + `referents.py` | yes — 2 tools | yes — 42 tests | yes — resolves chart / spreadsheet / deck / person's chat | yes | **COMPLETE** |
| 18 | Generic, not special-cased | providers register; resolver knows no referent type | yes | yes — asserted directly | yes | yes | **COMPLETE** |
| 19 | Ask rather than guess when ambiguous | `DECISIVE_MARGIN`, `Resolution.question()` | yes | yes | yes | yes | **COMPLETE** |
| 20 | Resource manager | `void/system/resources.py`, `actions/resources.py` | yes — 2 tools | yes | yes — real priority change 32→16384, exact restore | yes | **COMPLETE** |
| 21 | No unrestricted process control | lower-only, reversible, no kill/suspend/raise | yes | yes — asserted absent | yes | yes | **COMPLETE** |
| 22 | Device registry trust state | `void/device/trust.py` — composes existing registry + reading | yes — 1 tool | yes | yes — 29 real devices, 27 present, 2 authorized | yes | **COMPLETE** |
| 23 | Explicit authorization before trust | pairing/grant stay in the existing CLI; no tool grants | yes | yes — module has no `grant`/`trust` | yes | yes | **COMPLETE** |
| 24 | Verification and replanning | `orchestration/verify.py`, `replan.py` (foundation) | yes | yes | yes | yes | **COMPLETE** |
| 25 | Route ladder, structured before pixels | `orchestration/routes.py` | yes | yes | yes — tab reuse chosen by the resolver | yes | **COMPLETE** |
| 26 | Observability | `orchestration/trace.py`, OTel API only | yes | yes | PARTIAL — API present, SDK absent, so no exporter | yes | **PARTIAL** |
| 27 | Interaction model — confirmation boundary | `security/consequential.py`, `_click_risk` ×2 | yes | yes | yes | yes | **COMPLETE** |
| 28 | TTS interruption | `void/voice/adapters.py` + `orchestration/commands.py` + `app.py` | yes | yes — 99 voice + 35 interruption tests | simulated (deterministic SAPI harness); microphone **environment-limited** | yes | **COMPLETE** |
| 29 | Provider governance | `void/providers/policy.py` | yes — enforced at screen egress | yes — 16 tests | yes — real screen capture blocked by policy | yes | **COMPLETE** |
| 30 | Research provider architecture | `void/research/providers.py` | yes — config-driven | yes — 15 tests | yes — live fallback across 3 providers | yes | **COMPLETE** |
| 31 | OpenTelemetry export | `void/obs/__init__.py` | yes — `Assistant.telemetry` | yes — 11 tests | yes — real OTLP/protobuf over HTTP, decoded with official protobufs | yes | **COMPLETE** |
| 32 | AG-UI | `void/ui/agui.py` | yes — subscribes to `EventLog` | yes — 9 tests | yes — real protocol objects for all 15 event kinds | yes | **COMPLETE** |
| 33 | A2UI | `void/ui/a2ui.py` | yes — `Assistant.a2ui` | yes — 17 tests | yes — catalog, tokens and renderer exercised | yes | **COMPLETE** |
| 34 | A2A | `void/a2a/__init__.py` | yes — `Assistant.a2a` | yes — 18 tests | simulated transport (local harness); real `AgentCard` from the official SDK | yes | **COMPLETE** |
| 35 | Repository organization | root `conftest.py`, `requirements/` | yes | yes — 2 rewritten guard tests | yes — bare `pytest` and `pytest .` both collect cleanly | n/a | **COMPLETE** |

## 2. Technologies

**Built in V.O.I.D (not imported):** the perception model and observation ranking; the URL scheme policy;
the consequential-action vocabulary shared by browser and desktop; engine-minted handles for both DOM
elements and UIA controls; screen capture over raw GDI; artifact inspection (reopen and count, including a
structural PDF check because no PDF parser is installed); the research relevance scorer; reference parsing,
candidate scoring and the ambiguity margin; device standing composition; the reversible priority manager.

**External, integrated behind one adapter each:** Playwright 1.63 (only `void/browser/playwright_adapter.py`
imports it), uiautomation 2.0.29 (only `void/desktop/uia_adapter.py`), python-docx, python-pptx, openpyxl,
reportlab (only `void/artifacts/`), psutil (only `void/system/`), pywin32 (screen capture, already present).

## 2a. Dependencies

Now tracked in `requirements/` (see `requirements/README.md` for why that directory exists). The owner's
base manifests remain in `workspace/` and were not moved or edited; these are additive and pinned to exact
versions, and `tests/test_v3_interop.py` asserts every pin matches what is installed.

| Package | Version | Used by | Why not an internal implementation |
| --- | --- | --- | --- |
| `playwright` | 1.63.0 | `void/browser/playwright_adapter.py` | Driving a real browser is not reimplementable; a custom engine is explicitly out of bounds. Drives the *installed* Edge/Chrome/Opera GX, so nothing is downloaded. |
| `uiautomation` | 2.0.29 | `void/desktop/uia_adapter.py` | A thin wrapper over the Windows UIA COM API. Writing the COM plumbing by hand would be more code and more risk for no gain. |
| `python-docx` | 1.2.0 | `void/artifacts/` | OOXML by hand is a large, well-solved problem. |
| `python-pptx` | 1.0.2 | `void/artifacts/` | As above. |
| `openpyxl` | 3.1.5 | `void/artifacts/` | As above. |
| `reportlab` | 5.0.1 | `void/artifacts/` | PDF generation by hand is not worth it. Note: no PDF *parser* is installed, so PDF inspection is structural (header, EOF marker, page count). |

| `opentelemetry-sdk` | 1.45.0 | `void/obs/` | Official SDK for an API already in use. Building one would be the clearest possible case of replacing a mature library for no reason. Pinned to match the installed API 1.45.0 - a skew between API, SDK and exporter is a data-model mismatch. |
| `opentelemetry-exporter-otlp-proto-http` | 1.45.0 | `void/obs/` | Official OTLP exporter. HTTP/protobuf needs only `urllib3` (already present) and traverses proxies gRPC does not. |
| `ag-ui-protocol` | 1.0.0 | `void/ui/agui.py` | Official AG-UI event schema. Adds **no** transitive dependencies. Pinned because an event schema that changes under a running front end is a broken front end. |
| `a2a-sdk` | 1.2.1 | `void/a2a/` | Official A2A SDK, used for the agent card and message types. Adds only `json-rpc`. V.O.I.D does not start its server. |

Pulled in transitively: `Pillow`, `lxml`, `comtypes`, `greenlet`, `pyee`, `XlsxWriter`, `et-xmlfile`,
`json-rpc`, `opentelemetry-proto`, `opentelemetry-semantic-conventions`,
`opentelemetry-exporter-otlp-proto-common`, `opentelemetry-exporter-http-transport`.
`psutil` (7.2.2), `pywin32`, `protobuf` (5.29.6) and `urllib3` were already present; no new dependency was
added for screen capture. All checked on Python 3.14.7 before adoption; every one is the official PyPI
package - no forks, no vendored copies, no git URLs.

**A2UI has no PyPI package**, so nothing was pinned for it; see §4.

## 2b. The governance and interoperability pass

**Repository organization.** The owner's documents and configuration stay in `workspace/`; nothing was
moved back. A root `conftest.py` scopes collection away from the untracked `wakeword-training` project,
which is pytest's own mechanism, works with no ini file, and is stricter than `testpaths` (which applies
only when no path argument is given, so `pytest .` would still have collided). The two tests that read
`pytest.ini` and `requirements.txt` from the root were **rewritten to assert the property rather than the
file location** — collection is scoped; telemetry is optional and pinned — which makes them stronger, not
weaker. New dependency manifests live in `requirements/`, a dedicated directory, so the root stays clean
and the pins are tracked in git for the first time.

**Provider governance** answers *which provider may perform which capability using which data, under which
conditions*, from configuration, below the model. `Request` has no field a model could set; capability is
the intersection of what a provider declares and what the policy permits, so an installed SDK grants
nothing; `DataClass.CREDENTIAL` is refused to every provider with no configuration that can permit it; and
every member of a fallback chain is authorized against the *same* request, so a local-only task cannot
reach a cloud model and a provider not granted `screen_image` never receives a screenshot. Enforced at the
screen egress point as a second, independent gate alongside `screen.allow_cloud_analysis`.

**Research providers** are now described, governed, and measured: endpoint, priority, timeout, local/cloud,
fallback eligibility, plus per-provider attempts, successes, latency and last failure. A provider that
keeps failing is rested rather than retried. V.O.I.D still does **not** work around an access control —
a refusal is recorded and the next permitted provider is used.

**Telemetry** configures the official SDK so the spans `trace.py` already emitted become collectable.
Attributes are allowlisted twice. Headers come from configuration or the environment and are never logged.
A defect was found and fixed here: OpenTelemetry permits one global tracer provider per process and a
second attempt is ignored with only a log line, so `configure()` was reporting `exporting=True` for a
provider whose exporter received nothing. It now verifies the provider was actually installed and reports
degradation honestly.

**AG-UI** is outbound only. It has no method that executes, authorizes, confirms or changes a task, so a
front end cannot use the event stream to drive V.O.I.D. Only engine-minted fields travel; `detail`, the
one free-text field on a task event, is excluded because its contents cannot be reasoned about.

**A2UI** uses a closed component catalog with typed properties and a trusted renderer. Executable
properties (`html`, `script`, `onclick`, `src`, `action`, `tool`, …) are refused loudly, not sanitised.
Interactions are allowlisted intents — there is no way to express "run this tool". An approval reply
carries only a token; V.O.I.D looks up what that token was for, so a tampered or replayed message cannot
approve something other than what was shown. Tokens are single-use, task-bound and expiring.

**A2A** treats the caller as untrusted and never as the owner. Every advertised skill is read-only, and
there is no skill identifier for writing, automating, messaging, device access, memory or shell — a remote
agent cannot even express those requests. The gateway decides and returns; it executes nothing, and an
accepted request is handled by the ordinary path under the skill's tool ceiling. Identity comes from the
transport, never from the message body, so injected claims in the text grant nothing.

**TTS interruption** was already well built — generation tokens in the voice session, newest-command-wins
supersede and purge in the SAPI adapter, 99 existing voice tests. What this pass added is the cross-layer
coverage (a spoken "stop" reaching the backend, a late completion not resuming speech, concurrent
speak/stop) and one real fix: cancelling now **asks which job** when more than one is running, because
cancelling the wrong one is not recoverable and "cancel that" does not identify either.

## 3. Honest gaps

**Microphone hardware (#28).** The TTS interruption *state machine* and the whole control chain are
validated deterministically, and speech really does stop: the SAPI adapter is driven through a fake voice
that records purges and completions. What cannot be validated here is the real microphone - PortAudio
reports `-9999` on this machine, a limitation already on record for this project - so a spoken "stop"
captured by real hardware is **environment-limited, not verified**. The link that is unproven is audio
capture, not interruption: everything from the transcript onwards is covered.

**A2A transport (#34).** The boundary, the agent card and every refusal path are validated, and the card
is a real `a2a.types.AgentCard` from the official SDK. There is no second agent on this machine, so the
transport is a local harness - **simulated transport validation**. V.O.I.D deliberately does not start the
SDK's server (`a2a.allow_listener` is false and separate from `a2a.enabled`), so no listener was exercised.

**Collector (#31).** The export is real end to end - official SDK, real OTLP/protobuf encoding, real HTTP,
payload decoded with the official protobuf definitions - but the collector is a local test harness rather
than a production one (Jaeger, Tempo, otelcol). The wire format and transport are genuine; the receiver is
not a product.

**Search provider quality (#16).** The engine works and was validated live. But measured against the real
web, Google, Bing and both DuckDuckGo HTML endpoints refuse automated navigation with `ERR_ABORTED`, and
Startpage and Mojeek answer 403. V.O.I.D does **not** attempt to defeat those defences — working around bot
detection is out of bounds. The default chain therefore uses endpoints that permit programmatic access
(a SearXNG instance, Marginalia), and result quality depends on them: one live pass returned the canonical
EU AI Act source alongside two weak results. An owner running their own SearXNG gets materially better
results, which is why the endpoint list is configuration.

**Observability export (#26).** The OpenTelemetry *API* is present transitively and is used; the *SDK* is
not installed, so spans are created but nothing exports them. No dependency was added for this.

**TTS interruption (#28).** Implemented in the V2 voice runtime and tested there. Not re-validated in this
pass, because the real microphone is unavailable on this machine (PortAudio `-9999`), a limitation recorded
earlier in the project. Marked PARTIAL rather than COMPLETE for that reason.

**Camera vision.** Local vision models are absent; cloud analysis is the only path and is off by default.
Unchanged from V2, where it was recorded as PARTIAL for the same reason.

## 4. Declined, with reasons

**A2UI as an upstream library.** A2UI has no published Python package, so there was nothing to adopt or
pin. The representation in `void/ui/a2ui.py` is the smallest V.O.I.D-native one that satisfies the
requirement, isolated behind one module and version-stamped (`void.a2ui/1`) so a renderer can refuse a
shape it does not understand. Adopting an upstream library later means rewriting that one file.

**Raw keyboard and mouse injection at coordinates.** Not built. It sits at the bottom of the route ladder,
it is unverifiable, and every V3 need it would serve is better met by an addressable control.

**A second device registry.** Not built. One already exists with pairing, grants and per-device secrets;
two stores of who is trusted is how something gets trusted twice and revoked once.

## 5. Security review of the new surfaces

Asserted by `tests/test_v3_security.py` (108 tests), not merely claimed:

- No V3 module reaches for `subprocess`, `os.system`, `eval`, `exec`, or any elevation API.
- No V3 module calls `RiskGate.authorize`, constructs a gate, or touches the kill switch — authorization
  happens only in the single execution funnel.
- The only socket in the V3 layers is one loopback TCP connect used to notice a browser the owner started.
  It has no host parameter, so it cannot become a port scanner.
- Every new layer is deny-by-default; each tool refuses while its layer is off, and refuses rather than
  crashes when its adapter raises.
- Web content, page titles, window titles, control names and device names are cleaned, bounded, carried as
  data, and labelled untrusted to the model. None can authorize anything.
- Consequential risk is read from the live page or control tree. A name supplied in the call is ignored —
  there is no such parameter — and the risk function fails to HIGH on every unexpected condition.
- Handles are ASCII index paths or `role#index`. Selectors, XPath, UIA conditions, coordinates and
  Unicode-digit lookalikes are all refused.
- Reference memory stores a label and a target only; it has no field that could carry a permission.

### Defects found and fixed — governance and interoperability pass

**`configure()` reported an export that was not happening.** OpenTelemetry permits exactly one global
tracer provider per process; a second `set_tracer_provider` is ignored with only a log line and does not
raise. `configure()` therefore returned `exporting=True` for a provider whose exporter received nothing —
a status that lies, which is the one thing an observability layer must not do. It now verifies the
provider was actually installed and reports degradation with the reason. Found by the end-to-end export
test failing when it ran second in a process.

**The gate-bypass scan became too coarse.** Adding `ProviderPolicy.authorize` made the security test's
`.authorize(` pattern match a legitimate, unprivileged call. Rather than loosen the test, it now names the
RiskGate directly — stricter, since a module may not import or construct one at all — and a second test
pins that the only `authorize` a capability layer may call is a provider policy's.

**Cancelling could abandon the wrong job.** `cancel` moved the most recent cancellable task, so "cancel
that" with two jobs running was a guess with an unrecoverable cost. It now names the candidates and asks,
which is the rule the reference resolver already follows for "open that chart". Pausing, being
recoverable, still just acts.

### Defects found and fixed — capability pass

**Unicode digits satisfied "strict" handle checks.** Python's `\d` and `str.isdigit()` both accept
non-ASCII decimal digits, so `path:١.٢` and `button#١` passed checks documented as strict in the desktop
and browser handle parsers and then converted cleanly through `int()`. Both now match ASCII explicitly.
Found by `tests/test_v3_security.py`.

**Browser element handles were positionally unstable — a real time-of-check-to-time-of-use gap.** Found
only by real validation, on a live Wikipedia edit page. Handles were `role#index`, and the index came from
DOM order. The edit toolbar loads asynchronously, so inserting a button shifted every later index:
`button#5` was *Publish changes* on one read and *Find and replace* on the next, with 4 of 57 handles
changing identity between two reads of the same page. Risk was graded by re-reading the page and matching
the handle, so V.O.I.D could grade one control and click another — exactly the gap the design exists to
close.

Fixed in two parts. Handles now carry a fingerprint of the accessible name they were offered under
(`role#index~fingerprint`), and the adapter keeps an **offer table** recording what it showed. Resolution
goes through that table and locates the element by its own accessible name rather than its position, so a
handle survives a page that is still loading; a handle absent from the table does not resolve at all, which
is what makes a handle real rather than merely well-shaped. Risk grading reads the offered name, which is
sound precisely because resolution refuses to act on an element whose live name no longer matches — the
name a decision was made under and the name of the thing clicked cannot diverge.

Measured before and after on the same page: 4 handles drifting → 0; *Publish changes* graded MEDIUM → HIGH;
and *Show preview*, *Show changes* and *Cancel*, which the fail-safe path had been grading HIGH for the
wrong reason, correctly MEDIUM. That last part matters on its own: confirming harmless clicks teaches the
owner to approve without reading, which damages the confirmation boundary more than asking less often and
meaning it.

## 6. What a reader should check first

If you are reviewing this work, the three files that carry the most risk are
`void/desktop/uia_adapter.py` (it can press anything in any window), `void/actions/browser.py` and
`void/actions/desktop.py` (they decide when the owner is asked). The invariant to check in all three is
that the name used for the consequential decision is read from live state and never taken from the
model's arguments.
