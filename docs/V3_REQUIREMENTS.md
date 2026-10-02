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
| 28 | TTS interruption | V2 voice runtime (pre-existing) | yes | yes | not re-validated this pass — see §3 | yes | **PARTIAL** |
| 29 | AG-UI / A2UI / A2A adapters | — | — | — | — | — | **DECLINED** — see §4 |

## 2. Technologies

**Built in V.O.I.D (not imported):** the perception model and observation ranking; the URL scheme policy;
the consequential-action vocabulary shared by browser and desktop; engine-minted handles for both DOM
elements and UIA controls; screen capture over raw GDI; artifact inspection (reopen and count, including a
structural PDF check because no PDF parser is installed); the research relevance scorer; reference parsing,
candidate scoring and the ambiguity margin; device standing composition; the reversible priority manager.

**External, integrated behind one adapter each:** Playwright 1.63 (only `void/browser/playwright_adapter.py`
imports it), uiautomation 2.0.29 (only `void/desktop/uia_adapter.py`), python-docx, python-pptx, openpyxl,
reportlab (only `void/artifacts/`), psutil (only `void/system/`), pywin32 (screen capture, already present).

## 2a. Dependencies added in this pass

Recorded here because `requirements.txt`, `requirements.lock` and the three `requirements-*.txt` files are
**absent from the working tree** (deleted there, still present in git HEAD) and recreating them was not
this pass's call to make. Whoever restores them should add:

| Package | Version | Used by | Why not an internal implementation |
| --- | --- | --- | --- |
| `playwright` | 1.63.0 | `void/browser/playwright_adapter.py` | Driving a real browser is not reimplementable; a custom engine is explicitly out of bounds. Drives the *installed* Edge/Chrome/Opera GX, so nothing is downloaded. |
| `uiautomation` | 2.0.29 | `void/desktop/uia_adapter.py` | A thin wrapper over the Windows UIA COM API. Writing the COM plumbing by hand would be more code and more risk for no gain. |
| `python-docx` | 1.2.0 | `void/artifacts/` | OOXML by hand is a large, well-solved problem. |
| `python-pptx` | 1.0.2 | `void/artifacts/` | As above. |
| `openpyxl` | 3.1.5 | `void/artifacts/` | As above. |
| `reportlab` | 5.0.1 | `void/artifacts/` | PDF generation by hand is not worth it. Note: no PDF *parser* is installed, so PDF inspection is structural (header, EOF marker, page count). |

Pulled in transitively: `Pillow`, `lxml`, `comtypes`, `greenlet`, `pyee`, `XlsxWriter`, `et-xmlfile`.
`psutil` (7.2.2) and `pywin32` were already present and no new dependency was added for screen capture or
for observability.

## 3. Honest gaps

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

**AG-UI / A2UI / A2A adapters (#29).** Not built. V3 has no second agent to talk to and no external UI
surface asking for a protocol; building an adapter boundary now would be speculative multi-agent
infrastructure, which the brief explicitly rules out. The orchestration layer's event log and task state
are the natural attachment point if a real consumer appears.

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

### Defects found and fixed

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
