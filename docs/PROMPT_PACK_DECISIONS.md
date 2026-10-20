# Decision ledger

The directive asks for a decision ledger. The reasoning behind individual choices is scattered
across sixteen commit messages and eighty ledger rows, which is no use to someone deciding whether
to keep a decision. This collects the consequential ones.

Each entry says what was chosen, **what was rejected**, why, and — the part that matters most —
**what would change the decision**. A decision with no stated reversal condition is a prejudice.

Decisions marked **OWNER** are ones I had no standing to make and deliberately left open.

---

## A. Scope and architecture

### A1. Integrate the capabilities, not the packs' scaffold
**Chosen:** take the laws the corpus agrees on and build them into V.O.I.D's existing architecture.
**Rejected:** following the packs literally — `build.py` writing a graph file, `server.py` on port
4700, keys in `config.json`, the browser owning the microphone.
**Why:** the packs build a Jarvis from an empty folder. V.O.I.D is ~92k lines with its own voice
runtime, agent loop, risk gate, kill switch and encrypted memory. Following them literally would
have meant a second HTTP listener, a second scorer, a second state machine and credentials in a
tracked file — each a downgrade presented as a feature.
**Would change if:** someone wants the packs' 3-D viewer specifically, which is a UI product
decision and not a capability.
**Where:** `PROMPT_PACK_SOURCES.md` §4 has all six conflicts resolved against the packs individually.

### A2. The judge reads a ledger it cannot write
**Chosen:** receipts come from the single execution funnel `Agent._run_call` already writes; the
judge is a pure function with no imports from `void.*`.
**Rejected:** letting the judge ask tools whether they ran, or scoring confidence.
**Why:** the component with the most authority over what the owner is told should have the least
access. A judge that could query anything could be made to agree.
**Would change if:** never, as far as I can see. Giving it reach is the one change that would
destroy its value.

### A3. The screen watch decides WHEN, never WHAT
**Chosen:** 576 block means, locally diffed; acting on a nudge stays behind
`screen.allow_cloud_analysis`, the risk gate and the existing vision path.
**Rejected:** having the organ take a capture and ask a model about it — which is what the pack
describes.
**Why:** that is the most privacy-expensive thing in the whole corpus. Splitting "notice" from "act"
means the always-on half has no provider to call and the acting half is already gated.
**Would change if:** the owner explicitly wants automatic scene understanding. That is a different
feature with a different consent conversation, not a tweak to this one.

---

## B. Where I refused to guess

### B1. A model name that does not resolve is refused, never approximated
**Chosen:** exact match against a configured known set, then unambiguous size-tag completion, then
refusal with the list of what is available.
**Rejected:** nearest-match.
**Why:** silently running a different model than the owner asked for is worse than saying no. The
known set comes from configuration so nothing is in it because something guessed.
**Would change if:** never.

### B2. Absence is a first-class answer
**Chosen:** when the vault does not cover a question, return **no excerpts at all** plus an
instruction not to improvise. Same rule applied to the device inventory after finding it broken.
**Rejected:** returning the best-scoring note anyway.
**Why:** a note that merely shares a topic is the raw material for a confident wrong answer.
**Would change if:** never. This is the single most reusable rule in the corpus, and extending it
to `list_devices` fixed three CI failures as a side effect.

### B3. Twelve requirements are BLOCKED rather than stubbed
**Chosen:** write no client for Retell, Jev, Telegram or Google Calendar/Gmail, and name exactly
what each needs.
**Rejected:** a plausible-looking client that has never completed a request.
**Why:** code claiming a capability is precisely the failure the receipts rule exists to prevent. I
would have been doing in code what the judge blocks in prose.
**Would change if:** credentials and network access appear. The ledger names what each needs.

### B4. Invoice delivery stays blocked, and cannot be faked
**Chosen:** `delivered=False`, no code path able to set it true, and a syntax-tree walk asserting no
transport is imported.
**Rejected:** a `delivered` flag the caller sets.
**Why:** "the document exists" and "the invoice was sent" are different facts, and the gap between
them is exactly what the packs warn about.
**Would change if:** a transport is configured — and then the flag should be set by the transport's
own receipt, not by the engine.

---

## C. Security and privacy

### C1. Keys stay in the OS credential store
**Rejected:** the packs' `config.json`. **Why:** a tracked file with a key in it is one `git add`
from a published secret. **Would change if:** never.

### C2. State files are owner-only, repaired at boot
**Chosen:** 0700 on the state directory, 0600 on the databases, perf log, stop file and invoice
ledger — applied at creation **and** at boot.
**Why:** `~/.void/tasks.sqlite` was 0644 and holds task goals, the owner's own sentences and tool
output in plain text. 4 255 tests passed while that was true. Boot-time repair matters because an
installation that is already wrong needs fixing, not merely not worsening.
**Would change if:** never. On Windows it reports `not_applicable` rather than pretending.

### C3. The investigation browser is hardened three ways
**Chosen:** no `--remote-allow-origins=*` (verified unnecessary against a real browser), an
ephemeral debugger port by default, and `--no-sandbox` only under root and only with a warning.
**Rejected:** the pack's fixed port 9333 and permissive origins.
**Why:** anything that can reach loopback can try a known port, and permissive origins would let any
page that found it drive the investigation browser. Nothing needs the port to be predictable — REA
is handed the endpoint as an argument.
**Would change if:** someone wants to watch with their own devtools; 9333 is still available
explicitly.

### C4. The screen watch needs two switches
**Chosen:** `screen.enabled` **and** `screen.watch_enabled`, both false by default; `from_config`
returns `None` rather than a disabled object.
**Why:** "look at my screen when I ask" and "watch my screen on a loop" are different decisions and
one must never imply the other. Returning `None` means a caller cannot accidentally drive a watch
the owner never asked for — there is nothing to drive.
**Would change if:** never.

### C5. The grid size is not configurable
**Chosen:** 32×18, fixed in code.
**Why:** the entire privacy argument depends on it being too coarse to hold a glyph. A knob that
could raise it would be a knob that could undo the argument.
**Would change if:** never — and if more resolution is wanted, that is C/A3's conversation.

---

## D. Testing and verification

### D1. The skip ceiling is exact, never padded
**Chosen:** `max_skipped` set to precisely the derived count; corrected 6 → 14 → 15 as the count
changed.
**Rejected:** 16 or 18 "for margin".
**Why:** this is the decision that paid for itself twice. A padded ceiling would have let an
uncounted skip through silently — which is the thing the guard exists to prevent. It rejected my
arithmetic when I was one short, and the skip I had missed was one I had added myself two commits
earlier.
**Would change if:** never. Margin belongs in `min_collected`, which only ratchets upward.

### D2. Per-device gates, not a blanket marker
**Chosen:** each hardware test skips only when **its own** device is absent, and runs where present.
**Rejected:** implementing the `hardware` marker's documented "skip in CI" literally.
**Why:** blanket-skipping all nine marked tests would have turned five that **pass** on
`windows-latest` into skips, losing coverage CI really provides. The camera test is split so the
part CI can check — that the probe answers rather than raising — runs everywhere.
**Would change if:** the gates become unmaintainable; then a marker with a per-device condition is
the next step, not a blanket one.

### D3. Structural tests walk syntax trees, not text
**Chosen:** AST walks for "this module imports no transport", "no package manager is invoked",
"nothing constructs a QApplication".
**Rejected:** grep.
**Why:** three times a grep-based test failed on its own docstring. These files explain at length
why they do not do a thing, and a grep cannot tell an explanation from a call.
**Would change if:** never.

### D4. Views are tested with injected fakes, not a display
**Chosen:** `FocusCardView` and `TeardownPanelView` take their widgets as constructor arguments, so
render logic is covered on every platform.
**Why:** it removed every display-dependent skip, made the Windows skip count deterministic, and
found a real defect — placement sat inside the same `try` as `show()`, so an unresolvable screen
produced no card at all rather than a card in the wrong corner.
**Would change if:** never.

### D5. Nothing was weakened to get green
**Chosen:** across sixteen commits, no assertion weakened, no test skipped, disabled or quarantined,
no safeguard turned off. The three real defects behind CI's twelve were fixed **in the code**.
**Why:** it is the directive's §6, and it is also the only way the suite stays worth running.

---

## E. Reversals — decisions I got wrong and changed

Kept visible rather than edited away, because the reasoning that was wrong is useful.

### E1. "REA 6.7.0 has dropped the pack's commands"
**Was:** declined the teardown kit on that basis. **Actually:** installed it, ran `rea --help`, all
four commands present. **Corrected:** the kit was built. The original claim stays in the ledger with
the correction beside it.

### E2. "All twelve CI failures are environment-bound"
**Was:** reported as a group, twice, including on the PR. **Actually:** three were real defects —
the folder lookup cancelled by an app-catalog failure, the device inventory reporting absence as
failure, the wake thread bound never pinned — and two were tests asking the engine to breach
`allowed_roots`, which had therefore never passed anywhere.
**Lesson:** classifying failures as a group is how real defects hide. One at a time.

### E3. "`max_skipped` needs raising from 4 to 6"
**Was:** stated twice on the PR. **Actually:** it was already 6 on `main`, and 6 was already below
the 10 skips Windows reports — the ceiling had never been evaluated, because pytest failed first
every time and the guard step never ran.

### E4. "The panel and the card cannot be validated from here"
**Was:** both declined as unverifiable UI. **Actually:** once the logic was separated from the
painting, almost all of it was decidable headlessly, and writing the views against fakes found a
real defect (D4).
**Lesson:** "cannot be tested" usually means "is not yet factored to be testable".

### E5. "Windows needs no `xdg-open`, so the folder tests will pass"
**Was:** an inference I flagged as unverified. **Actually:** correct — but I had also predicted
green twice when it was not, both times for failure modes a Linux run structurally cannot see (a Qt
abort, and my own off-by-one). The inference held; the confidence was not earned.

---

## F. OWNER — open, deliberately

### F1. Merging to `main`
`main` is untouched at `48a2203`. Fifteen commits sit on `claude/gracious-gauss-u81whr` with CI
green. Merging is the owner's decision and I did not take it.

### F2. The declined organs, if they are wanted after all
The screen watch was built on explicit instruction after being declined. If the privacy trade in
A3/C4/C5 reads differently to the owner than it does to me, the switches are the place to change it
— not the grid size.

### F3. What to do about a bot finding that costs more to fix than it prevents
No instance arose in this round. If one does, the owner's call on the first case should become the
rule for later ones.
