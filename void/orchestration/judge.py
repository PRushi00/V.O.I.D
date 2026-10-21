"""The last thing between a model's sentence and the owner's ears: did that actually happen?

The failure this layer exists to break is narrow and expensive. V.O.I.D's verifier
(:mod:`void.orchestration.verify`) asks whether an *intended world state* came about. This asks a
different question about a different artifact: the model has written a final answer, and that answer
claims V.O.I.D **just performed an action with an effect outside this machine** - sent, called,
booked, paid, posted, deleted. If no tool in this turn produced that effect, the sentence is a
fabrication, and it is the single most costly kind of wrong answer an assistant can give: the owner
stops watching for the email that never left.

Every prompt pack in ``docs/PROMPT_PACK_SOURCES.md`` carries this rule in some form - "receipts or it
didn't happen", "only claim an action happened when a tool result confirms it", "an AI that smooths
over failures is worse than no AI". The Jev pack implements it by asking a decision model for
P(the answer claims an action) and P(every claim is backed). That service is unreachable from here,
and a guessed probability would be a worse foundation than the thing V.O.I.D already has: its own
deterministic record of which tools ran and whether they succeeded.

So the design is:

**Receipts, not memory.** :class:`Receipt` records come from ``Agent._run_call`` - the one funnel every
tool call goes through - so the ledger cannot be influenced by what a model says it did.

**Five classes V.O.I.D cannot do at all, and one it can.** V.O.I.D has no tool that sends a message,
places a call, books anything, pays anyone or posts anywhere; a claim in any of those classes is
unbacked by construction and always blocked. Deletion is the one claim class with a real capability
behind it, so it is checked against a successful receipt from the tool that does it. That is the
whole table (:data:`TOOL_EFFECTS`), and ``tests/test_judge.py`` fails if a tool with one of these
effects is ever added without being put in it.

**Narrow on purpose.** Only an unambiguous first-person completed-action claim counts. An offer
("shall I send it?"), an intention ("I'll send it"), a draft ("I've drafted a reply"), a denial ("I
could not send it") and a question are all left alone, because replacing an honest sentence with a
correction is its own kind of lie, and a judge that cries wolf gets switched off.

**What it deliberately does not screen.** Only a final answer a MODEL wrote. The engine's own
completions do not pass through it, and must not: ``Agent._deterministic_completion`` returns a
successful tool's own summary line verbatim (and only for a first-step, single-call,
``terminal_on_success`` tool), so it is receipt-backed by construction; ``_finish_direct`` replies
with engine constants or a fast-path line. Screening those would mean a gate inspecting sentences the
engine already knows to be true, which buys nothing and adds a way to be wrong.

The ruling never deletes evidence. The model's original sentence stays in the task's message history;
only what the owner is *told* is replaced, and the replacement is engine-authored, so it can be
spoken aloud under the same rule as any other engine sentence (see ``AgentResult.engine_authored``).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


class Effect:
    """Classes of claim about the world outside this machine."""

    SEND = "send"
    CALL = "call"
    BOOK = "book"
    PAY = "pay"
    PUBLISH = "publish"
    DELETE = "delete"

    ALL = frozenset({SEND, CALL, BOOK, PAY, PUBLISH, DELETE})


#: The past-tense words that assert each class. Deliberately small: every entry has to be a word
#: whose first-person past form can only mean "I performed this action", because the cost of a false
#: block is an honest answer replaced by a correction.
CLAIM_VERBS: dict[str, frozenset[str]] = {
    Effect.SEND: frozenset({"sent", "emailed", "e-mailed", "mailed", "messaged", "texted",
                            "replied", "forwarded", "submitted", "dispatched"}),
    Effect.CALL: frozenset({"called", "phoned", "rang", "dialled", "dialed"}),
    Effect.BOOK: frozenset({"booked", "scheduled", "reserved", "rescheduled"}),
    Effect.PAY: frozenset({"paid", "purchased", "ordered", "subscribed"}),
    Effect.PUBLISH: frozenset({"posted", "published", "uploaded", "tweeted"}),
    Effect.DELETE: frozenset({"deleted", "erased", "trashed"}),
}

#: Which tools can actually produce each effect. Five of the six classes map to nothing, which is a
#: statement about V.O.I.D and not an oversight: there is no send, call, booking, payment or posting
#: capability in this project, and ``void/actions/messaging.py`` refuses a control that would act
#: rather than open one. A claim in an empty class can never be backed.
TOOL_EFFECTS: dict[str, frozenset[str]] = {
    Effect.SEND: frozenset(),
    Effect.CALL: frozenset(),
    Effect.BOOK: frozenset(),
    Effect.PAY: frozenset(),
    Effect.PUBLISH: frozenset(),
    Effect.DELETE: frozenset({"delete_file"}),
}

#: What the owner is told instead, per class. Engine-authored, present tense about the record rather
#: than about the model: "there is no receipt" is checkable, "the model hallucinated" is a guess.
HONEST_LINES: dict[str, str] = {
    Effect.SEND: "I nearly reported that as sent, and there is no record of it. Nothing was sent - "
                 "I have no way to send anything.",
    Effect.CALL: "I nearly reported that call as made, and there is no record of it. No call was "
                 "placed - I have no way to place one.",
    Effect.BOOK: "I nearly reported that as booked, and there is no record of it. Nothing was "
                 "booked - I have no way to book anything.",
    Effect.PAY: "I nearly reported that as paid, and there is no record of it. Nothing was paid "
                "for - I have no way to pay for anything.",
    Effect.PUBLISH: "I nearly reported that as posted, and there is no record of it. Nothing was "
                    "posted - I have no way to post anything.",
    Effect.DELETE: "I nearly reported that as deleted, and there is no record of it. Nothing was "
                   "deleted this turn.",
}

#: Closing sentence appended to every correction, so the turn ends with something the owner can act on.
ASK_LINE = "Tell me what you would like me to do."

#: Phrases anywhere in the same sentence that mean the verb is not a completed claim: a denial, an
#: offer, an intention, a condition, or a draft. Checked first, because a sentence can contain both
#: ("I have not sent it").
_NOT_A_CLAIM = re.compile(
    r"\b(?:did\s+not|didn'?t|do\s+not|don'?t|could\s+not|couldn'?t|can\s*not|can'?t|will\s+not|"
    r"won'?t|was\s+not|wasn'?t|were\s+not|weren'?t|have\s+not|haven'?t|has\s+not|hasn'?t|"
    r"never|nothing\s+was|no\s+\w+\s+was|unable|failed\s+to|"
    r"shall\s+i|should\s+i|may\s+i|would\s+you|do\s+you\s+want|want\s+me\s+to|"
    r"i\s+can|i\s+could|i\s+will|i'?ll|i'?d|ready\s+to|about\s+to|going\s+to|happy\s+to|"
    r"if\s+you|once\s+you|when\s+you|you\s+can|you\s+could|you\s+should|you'?ll|"
    r"draft|drafted|drafting|would\s+have|could\s+have|instead\s+of)\b",
    re.IGNORECASE)

#: A verb whose object is V.O.I.D's own machinery is not a claim about the world: "I called the
#: search tool", "I posted the query". Matched at the verb, allowing one determiner in between.
_OWN_MACHINERY = re.compile(
    r"\A\s*(?:the|a|an|it|this|that|my|your)?\s*"
    r"(?:tool|tools|function|functions|api|endpoint|endpoints|script|command|method|search|"
    r"query|memory|proposal|prompt|request)\b", re.IGNORECASE)

#: Something produced IN this answer is not something delivered to the world: "I posted the summary
#: below", "I've listed what I sent above". Checked across the whole sentence, because the locator
#: usually trails the object.
_IN_THIS_ANSWER = re.compile(r"\b(?:below|above|here|as\s+follows|in\s+this\s+(?:answer|reply|list))\b",
                             re.IGNORECASE)

#: "called" doubles as the naming verb, and naming something is not phoning anyone: "I called it
#: pricing", "I called this one the retainer note".
_NAMING = re.compile(r"\Acalled\s+(?:it|this|that|them|these|those)\b", re.IGNORECASE)

#: Words that attribute a sentence to a SOURCE rather than to V.O.I.D's own action. They defuse the
#: passive frame only: "your note says the kickoff has been booked" reports what a note claims, and
#: blocking that would be the judge inventing a problem. They must not defuse the active frames, or
#: "I sent it, as your notes asked" would walk straight through.
_ATTRIBUTED = re.compile(r"\b(?:your|their|his|her|the)\s+(?:notes?|note|email|emails|calendar|"
                         r"message|messages|diary|file|document)\b|\b(?:according\s+to|per\s+your|"
                         r"mentions?|says?|said|states?|reads?|suggests?|appears?|seems?)\b",
                         re.IGNORECASE)

#: The frames a completed claim takes, each with whether it is PASSIVE (and so subject to
#: :data:`_ATTRIBUTED`). Each captures the verb, so the class is read from the match rather than from
#: the shape of the sentence.
_FRAMES = (
    # "I sent it", "I've just sent it", "we have booked it"
    # The auxiliary carries its own whitespace: with a leading \s* instead, "I have forwarded it"
    # reads the auxiliary as the verb and the claim is missed.
    (re.compile(r"\b(?:i|we)(?:\s*'ve|\s+have|\s+had)?\s+(?:just\s+|already\s+|now\s+)?"
                r"(?P<verb>[a-z-]+)\b", re.IGNORECASE), False),
    # "the invoice has been sent", "everything has now been deleted"
    (re.compile(r"\b(?:the|your|it|that|this|they|everything|all)\s+(?:[a-z-]+\s+){0,3}"
                r"(?:has|have|'s|'ve)\s+(?:now\s+|already\s+)?been\s+(?P<verb>[a-z-]+)\b",
                re.IGNORECASE), True),
    # A bare declaration opening the answer: "Sent.", "Done. Booked for seven."
    (re.compile(r"\A\s*(?:all\s+)?(?:done|right)?[,.!\s]*(?P<verb>[a-z-]+)\b", re.IGNORECASE),
     False),
)

#: A further claim verb in a sentence that already carries one, after "and", "then" or a comma.
_CONTINUATION = re.compile(r"(?:\band\b|,)\s*(?:then\s+|also\s+)?(?:i\s+|we\s+|the\s+\w+\s+)?"
                           r"(?:have\s+|has\s+|just\s+|already\s+|been\s+)?(?P<verb>[a-z-]+)\b",
                           re.IGNORECASE)

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")
_VERB_CLASS = {verb: cls for cls, verbs in CLAIM_VERBS.items() for verb in verbs}


@dataclass(frozen=True)
class Receipt:
    """One tool call that really ran, as the engine recorded it. ``ok`` is the engine's verdict."""

    tool: str
    ok: bool


@dataclass(frozen=True)
class Ruling:
    """What the judge decided about one answer.

    ``blocked`` false is the overwhelmingly common case and means the answer goes through byte for
    byte: the judge is a gate, not a rewriter, and it has no opinion about style.
    """

    blocked: bool
    answer: str
    #: The claim classes found in the answer, whether or not they were backed.
    claimed: tuple[str, ...] = ()
    #: The claim classes with no successful receipt behind them. Non-empty exactly when blocked.
    unbacked: tuple[str, ...] = ()
    #: The sentence the owner hears instead. Empty unless blocked.
    replacement: str = ""
    original: str = ""


def _effect_at(sentence: str, match) -> str | None:
    """The effect class this match asserts, or None when the verb is not asserting one here."""
    verb = match.group("verb").lower()
    cls = _VERB_CLASS.get(verb)
    if cls is None:
        return None
    rest = sentence[match.start("verb"):]
    if _NAMING.match(rest) or _OWN_MACHINERY.match(sentence[match.end():]):
        return None
    return cls


def _claim_classes(answer: str) -> set[str]:
    """Which effects this answer asserts as already done, sentence by sentence."""
    found: set[str] = set()
    for raw in _SENTENCE.findall(answer or ""):
        sentence = raw.strip()
        if not sentence or sentence.endswith("?"):
            continue
        if _NOT_A_CLAIM.search(sentence) or _IN_THIS_ANSWER.search(sentence):
            continue
        attributed = _ATTRIBUTED.search(sentence) is not None
        here: set[str] = set()
        for frame, passive in _FRAMES:
            if passive and attributed:
                continue
            for match in frame.finditer(sentence):
                cls = _effect_at(sentence, match)
                if cls is not None:
                    here.add(cls)
        if here:
            # "I sent the email and booked the table": the subject carries across the conjunction, so
            # a second claim verb in the same sentence counts once the first one has established that
            # the sentence is about what V.O.I.D did.
            for match in _CONTINUATION.finditer(sentence):
                cls = _effect_at(sentence, match)
                if cls is not None:
                    here.add(cls)
        found |= here
    return found


def screen(answer: str, receipts) -> Ruling:
    """Rule on one final answer against this turn's receipts.

    Never raises and never calls anything: it compares a string with a list the engine wrote. A
    judge that can fail is a judge that can take an answer down with it.
    """
    text = str(answer or "")
    claimed = _claim_classes(text)
    if not claimed:
        return Ruling(False, text)

    done = {r.tool for r in (receipts or ()) if getattr(r, "ok", False)}
    unbacked = sorted(cls for cls in claimed if not (TOOL_EFFECTS.get(cls, frozenset()) & done))
    if not unbacked:
        return Ruling(False, text, claimed=tuple(sorted(claimed)))

    replacement = " ".join([HONEST_LINES[cls] for cls in unbacked] + [ASK_LINE])
    # Content-free by V.O.I.D's logging convention: the classes and the tools that did run, never the
    # sentence. The sentence is not lost - it stays in the task's message history.
    _log.warning("JUDGE_BLOCKED unbacked=%s claimed=%s receipts=%s",
                 ",".join(unbacked), ",".join(sorted(claimed)), ",".join(sorted(done)) or "none")
    return Ruling(True, replacement, claimed=tuple(sorted(claimed)),
                  unbacked=tuple(unbacked), replacement=replacement, original=text)
