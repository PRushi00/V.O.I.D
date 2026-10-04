"""Resolving what "this", "that" and "the one I was looking at" refer to.

The blueprint's example is "open this chart", and the requirement attached to it is explicit: it must not be
a special-cased command. So nothing in this module knows what a chart is. What it provides is the general
machinery - **a referring expression is matched against candidates offered by providers** - and "chart"
works only because something in the system can offer a chart-shaped candidate. "Open the document I was
looking at", "open Rushi's chat" and "go back to that spreadsheet" all travel the same path, and a new kind
of referent becomes referenceable by registering a provider, never by editing the resolver.

How a phrase is resolved:

1. **Parse** the expression into a :class:`Reference`: a deictic word ("this", "that", "the one I was just
   looking at"), a noun naming a kind of thing ("chart", "file", "chat", "tab"), and any proper qualifier
   ("Rushi", "Q3 revenue").
2. **Gather** candidates from every registered provider. A provider reports things it can currently see:
   open tabs, open windows, documents V.O.I.D recently created, files recently touched.
3. **Score** each candidate against the reference - kind agreement, name overlap, recency, and whether the
   thing is in the foreground. Scoring is deterministic and inspectable; there is no model call here.
4. **Decide**: one clear winner is returned; a tie or an empty field returns the candidates and lets the
   caller ask. This is the part that matters most - *V.O.I.D asks instead of guessing.* Opening the wrong
   person's chat is not a small error, and the blueprint's interaction model calls for a question when
   reference is genuinely ambiguous.

Two security properties hold throughout:

* **Every label is untrusted.** Candidate names come from window titles, page titles and file names, all
  chosen by something other than the owner. They are cleaned, length-bounded, and used only for matching and
  display - never executed, never interpolated into a command.
* **Resolution is not authorization.** Resolving "this chart" yields a *target*, not an action. Opening it
  still goes through the normal tool funnel, the RiskGate and, where relevant, the consequential check. A
  resolver that could act would be a way to launder an action past the gate; this one cannot act at all.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from void.actions.app_names import squash
from void.core.fast_path import strip_address_prefix
from void.perception import clean_text

_log = logging.getLogger(__name__)

#: Words that mean "the one in play right now". Their presence is what turns a vague noun into a resolvable
#: reference: "open a chart" is a request to make one, "open this chart" is a reference to an existing one.
#:
#: "open" is deliberately NOT here. It is a verb that appears in both readings, and treating it as deictic
#: made "open notepad" look like a reference to an existing thing - which would hijack a plain launch
#: request and send it through resolution instead.
DEICTIC = frozenset({"this", "that", "these", "those", "it", "same", "again",
                     "current", "currently", "already", "just"})

#: Phrases meaning "the thing I was recently attending to". Matched as substrings, so word order variants
#: ("I was just looking at", "I had open") still land.
RECENCY_PHRASES = (
    "i was looking at", "i was just looking at", "was looking at", "i had open", "i was reading",
    "i was working on", "last one", "previous one", "the one before", "earlier", "just now",
    "a moment ago", "before that",
)

#: Nouns naming kinds of referents, mapped to the candidate kind they select. This is a vocabulary, not a
#: command table: a noun here does not trigger behaviour, it only narrows which candidates are eligible.
#: "chart" and "spreadsheet" both narrow to documents because that is what they are - no code path knows
#: anything more about a chart than that.
KIND_WORDS = {
    "chart": "document", "graph": "document", "diagram": "document", "figure": "document",
    "document": "document", "doc": "document", "file": "document", "report": "document",
    "deck": "document", "presentation": "document", "slides": "document", "slide": "document",
    "spreadsheet": "document", "sheet": "document", "workbook": "document", "pdf": "document",
    "tab": "tab", "page": "tab", "site": "tab", "website": "tab", "link": "tab",
    "window": "window", "app": "window", "application": "window", "program": "window",
    "chat": "conversation", "conversation": "conversation", "thread": "conversation",
    "message": "conversation", "dm": "conversation",
    "folder": "folder", "directory": "folder",
}

#: Words stripped before treating what is left as a proper qualifier.
_FILLER = frozenset({
    "open", "show", "bring", "up", "me", "the", "my", "a", "an", "to", "back", "again", "please",
    "go", "switch", "jump", "take", "pull", "that", "this", "those", "these", "it", "one", "same",
    "was", "i", "had", "been", "looking", "at", "reading", "working", "on", "just", "now", "and",
    "then", "with", "in", "of", "for", "from", "view", "see", "get", "find", "last", "previous",
    "earlier", "before", "current", "currently", "already", "moment", "ago", "display",
    # Possessive PRONOUNS are never the name of a referent. "my" was already here; the others were
    # not, so "open their chat" parsed "their" as the person to look for and then failed to find
    # them - where the right answer is to ask whose conversation is meant.
    "their", "theirs", "his", "her", "hers", "its", "your", "our", "ours",
    # Words from "...the folder you just found". They are how the owner refers to V.O.I.D's own
    # recent action, never part of a name - and left in, they became a QUALIFIER ("you found") that
    # no candidate could match, so the reference resolved to nothing once a named thing was
    # required to match by name.
    "you", "found", "opened", "showed", "mentioned", "we",
})

#: Verbs that mean "make a new one". Their presence cancels a reference, because "create a chart" names a
#: thing to produce, not one to find - and resolving it would open last week's chart instead of building the
#: one asked for. A deictic word overrides the cancellation ("make a chart like this one" genuinely refers
#: to something), which is why this is checked against deixis rather than on its own.
CREATION_VERBS = frozenset({"create", "make", "generate", "build", "write", "draft", "produce",
                            "compose", "new", "add", "start", "prepare", "export", "save"})

#: File extensions and the nouns they answer to. This is how "open that spreadsheet" finds a ``.xlsx``
#: whose name never contains the word "spreadsheet". A vocabulary mapping, not behaviour: nothing here
#: opens anything or knows what a spreadsheet does.
EXTENSION_NOUNS = {
    "xlsx": ("spreadsheet", "sheet", "workbook", "excel"),
    "xls": ("spreadsheet", "sheet", "workbook", "excel"),
    "csv": ("spreadsheet", "sheet", "data"),
    "pptx": ("deck", "presentation", "slides", "slide", "powerpoint", "chart"),
    "ppt": ("deck", "presentation", "slides", "powerpoint"),
    "docx": ("document", "doc", "report", "word"),
    "doc": ("document", "doc", "report", "word"),
    "pdf": ("pdf", "document", "report"),
    "png": ("chart", "graph", "image", "picture", "figure", "diagram"),
    "jpg": ("image", "picture", "photo"),
    "jpeg": ("image", "picture", "photo"),
    "svg": ("chart", "graph", "diagram", "figure", "image"),
    "txt": ("document", "notes", "note"),
    "md": ("document", "notes", "note"),
}

#: Possessive forms are a strong qualifier signal: "Rushi's chat" names a participant, not a kind.
_POSSESSIVE = re.compile(r"([\w][\w .-]{0,40}?)'s\b", re.IGNORECASE)

_WORD = re.compile(r"[a-z0-9][a-z0-9'.-]*")

#: Apostrophes a speech-to-text engine actually produces. Whisper emits U+2019, not the ASCII
#: apostrophe :data:`_POSSESSIVE` was written for, so "Rushi’s chat" matched no possessive at all and
#: the owner's name was split into "rushi" and "s".
_APOSTROPHES = {"‘": "'", "’": "'", "ʼ": "'", "＇": "'", "´": "'"}

#: Punctuation trimmed from the EDGES of a token before it is looked up.
#:
#: This is what made every spoken reference fail. ``_WORD`` has "." inside its character class so
#: that "report.docx" stays one token - and that also swallowed the full stop at the end of a
#: sentence, so "open this chart." tokenised to "chart." and matched nothing in KIND_WORDS. Real STT
#: always punctuates, so reference resolution was systematically broken for voice while working
#: perfectly for typed text. Trimming only the edges keeps "report.docx" intact.
_EDGE_PUNCTUATION = ".,;:!?\"'()[]"


def bare_word(word: str) -> str:
    """A token with sentence punctuation trimmed from its edges.

    Public because :mod:`void.orchestration.messaging` needs exactly the same rule and a second
    copy would drift: both parsers see the same punctuated transcripts.
    """
    return word.strip(_EDGE_PUNCTUATION)


#: How long a candidate stays plausible as "the one I was looking at". Beyond this, recency stops counting
#: for anything - a tab from yesterday is not what "this" means.
RECENCY_WINDOW_S = 1800.0

#: Scoring weights. Deterministic and ordered so that the reasons are explainable to the owner.
W_KIND = 40.0            # it is the kind of thing that was asked for
W_NAME_FULL = 60.0       # its name contains the whole qualifier
W_NAME_PART = 18.0       # its name shares words with the qualifier
W_FOREGROUND = 22.0      # it is what the owner is actually looking at
W_RECENCY = 20.0         # decays across RECENCY_WINDOW_S
W_PRODUCED = 14.0        # V.O.I.D made it this session, so "this chart" plausibly means it
#: The owner's own noun appears in its name ("chart" in "Q3 revenue chart.pptx") or is implied by its
#: extension ("spreadsheet" for a .xlsx). Set above ``W_PRODUCED + DECISIVE_MARGIN`` deliberately: when the
#: owner says "that spreadsheet" and exactly one candidate is a spreadsheet, that is not ambiguity, and the
#: fact that V.O.I.D happened to create the other one must not be enough to turn it into a question.
W_NOUN_HINT = 30.0

#: How much clear air the winner needs over the runner-up to be acted on without asking. Below this, the
#: caller is told it is ambiguous. Set from the weights: a difference smaller than a kind match is not a
#: decision, it is a coin toss.
DECISIVE_MARGIN = 15.0


@dataclass(frozen=True)
class Reference:
    """A parsed referring expression."""

    phrase: str
    kind: str = ""
    qualifier: str = ""
    deictic: bool = False
    recency: bool = False
    #: The literal noun the owner used ("chart", "deck", "spreadsheet"), kept alongside the kind it mapped
    #: to. The kind is coarse by design - a chart, a deck and a workbook are all documents - so the exact
    #: word is retained as a naming hint: it is what distinguishes "this chart" from "this spreadsheet"
    #: when both are documents sitting in the recent list.
    noun: str = ""
    #: True when the phrase asks for something to be made. Suppresses resolution unless the phrase also
    #: refers to something ("make a copy of this one").
    creating: bool = False

    @property
    def resolvable(self) -> bool:
        """True when this is actually a reference to an existing thing.

        Requires a deictic word, a recency phrase, or a noun naming a kind of referent. A bare name is
        **not** enough: "open notepad" names an application to launch, and treating it as a reference would
        divert an ordinary launch request into resolution. Likewise "what is the weather" leaves a residue
        that looks like a qualifier but refers to nothing.

        A creation request is not a reference either: "create a chart of Q3 revenue" names a chart to
        build, and resolving it would find last week's chart and open that instead. Deixis overrides, so
        "make a chart like this one" still resolves.
        """
        if self.creating and not (self.deictic or self.recency):
            return False
        return bool(self.deictic or self.recency or self.kind)

    def as_dict(self) -> dict:
        return {"phrase": self.phrase, "kind": self.kind, "qualifier": self.qualifier,
                "noun": self.noun, "deictic": self.deictic, "recency": self.recency,
                "creating": self.creating}


@dataclass(frozen=True)
class Candidate:
    """Something that could be what the owner meant.

    ``target`` is how to act on it later - a file path, a tab handle, a window handle. It is opaque here:
    the resolver's job ends at identifying the thing, and the caller's tools know what to do with it.

    ``label`` is UNTRUSTED (a window or page names itself) and is used only for matching and for telling
    the owner what was chosen.
    """

    kind: str
    label: str
    target: str
    source: str = ""
    at: float = 0.0
    foreground: bool = False
    produced: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", clean_text(self.label, 200))
        object.__setattr__(self, "detail", clean_text(self.detail, 200))

    def as_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "target": self.target,
                "source": self.source, "foreground": self.foreground,
                "produced": self.produced, "detail": self.detail}


@dataclass
class Resolution:
    """The outcome: one target, or a question to ask."""

    reference: Reference
    choice: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.choice is not None

    @property
    def ambiguous(self) -> bool:
        """Several things fit and none clearly wins. The caller must ask, not pick."""
        return self.choice is None and len(self.alternatives) > 1

    @property
    def empty(self) -> bool:
        return self.choice is None and not self.alternatives

    def question(self) -> str:
        """The question to put to the owner when reference is ambiguous."""
        if not self.alternatives:
            return ""
        names = [candidate.label or candidate.target for candidate in self.alternatives[:4]]
        listed = ", ".join(f"'{name}'" for name in names)
        return f"Which one do you mean - {listed}?"

    def as_dict(self) -> dict:
        return {"reference": self.reference.as_dict(),
                "choice": self.choice.as_dict() if self.choice else None,
                "alternatives": [candidate.as_dict() for candidate in self.alternatives],
                "reasons": list(self.reasons),
                "resolved": self.resolved, "ambiguous": self.ambiguous}


def parse_reference(phrase: str) -> Reference:
    """Parse a referring expression. Deterministic; no model involved.

    Kept lenient. The cost of parsing a non-reference as a reference is one resolution attempt that finds
    nothing and falls through to normal handling; the cost of missing a real reference is the owner being
    told V.O.I.D does not understand a sentence any person would.
    """
    # "Hey V.O.I.D., open this chart." - the address is not part of what is being referred to. Left
    # in, those words became the qualifier ("hey v.o.i.d"), and once a named thing had to match by
    # name that qualifier matched nothing, so spoken references stopped resolving entirely.
    text = clean_text(strip_address_prefix(phrase), 300).lower()
    for fancy, plain in _APOSTROPHES.items():
        text = text.replace(fancy, plain)
    if not text:
        return Reference(phrase="")
    recency = any(marker in text for marker in RECENCY_PHRASES)
    words = [bare_word(word) for word in _WORD.findall(text)]
    words = [word for word in words if word]
    word_set = set(words)
    deictic = bool(word_set & DEICTIC) or recency

    kind, noun = "", ""
    for word in words:
        mapped = KIND_WORDS.get(word.rstrip("s")) or KIND_WORDS.get(word)
        if mapped:
            kind, noun = mapped, word.rstrip("s")
            break

    def strip_filler(candidate_words) -> str:
        kept = [bare for bare in (bare_word(word) for word in candidate_words)
                if bare and bare not in _FILLER and bare not in DEICTIC
                and bare.rstrip("s") not in KIND_WORDS and bare not in KIND_WORDS]
        return " ".join(kept)

    possessive = _POSSESSIVE.search(text)
    if possessive:
        # "Rushi's chat" -> the possessor is the strongest name signal available. Filler is stripped from
        # the captured span because the pattern can start earlier in the sentence than the name does
        # ("open Rushi's chat" captures "open rushi").
        qualifier = strip_filler(_WORD.findall(possessive.group(1)))
    else:
        qualifier = strip_filler(words)
    return Reference(phrase=clean_text(phrase, 300), kind=kind, noun=noun,
                     qualifier=clean_text(qualifier, 120), deictic=deictic, recency=recency,
                     creating=bool(word_set & CREATION_VERBS))


def _extension_nouns(haystack: str) -> frozenset[str]:
    """The nouns implied by any file extension appearing in a candidate's name or detail."""
    found: set[str] = set()
    for match in re.finditer(r"\.([a-z0-9]{2,5})\b", haystack):
        found.update(EXTENSION_NOUNS.get(match.group(1), ()))
    return frozenset(found)


def _name_score(candidate: Candidate, qualifier: str) -> tuple[float, str]:
    """How well a candidate's name matches the qualifier.

    Includes the "same name spaced differently" tier, because the owner's spacing and the
    filesystem's rarely agree: the folder is called "VibeCoding" and they say "vibe coding", which
    matched neither as a substring nor by shared words, so the reference fell through to the model -
    16 to 26 seconds for something already in hand. The rule comes from
    :func:`void.actions.app_names.squash`, the same definition the application catalog and the
    folder catalog use, rather than a third copy of it.
    """
    if not qualifier:
        return 0.0, ""
    label = candidate.label.lower()
    detail = candidate.detail.lower()
    needle = qualifier.lower()
    if needle and (needle in label or needle in detail):
        return W_NAME_FULL, f"its name contains '{qualifier}'"
    tight = squash(qualifier)
    if tight and (tight in squash(candidate.label) or tight in squash(candidate.detail)):
        return W_NAME_FULL, f"its name is '{qualifier}' spaced differently"
    wanted = set(_WORD.findall(needle))
    if not wanted:
        return 0.0, ""
    have = set(_WORD.findall(label)) | set(_WORD.findall(detail))
    shared = wanted & have
    if not shared:
        return 0.0, ""
    return W_NAME_PART * len(shared), f"its name mentions {', '.join(sorted(shared))}"


def identifies(candidate: "Candidate", reference: "Reference") -> bool:
    """Is there positive evidence that this candidate is the thing the owner described?

    The distinction this draws is between **pointing** and **describing**, and it exists because of a
    real wrong-answer bug. "Open Rushi's chat" used to resolve, with full confidence, to a PowerPoint
    V.O.I.D had just produced: the kind mismatch cost it W_KIND/2, recency and "I made it" paid for
    more than that, the total came out positive, and a lone positive candidate is chosen. So a
    request for a person's conversation returned a revenue deck.

    The flaw was treating recency as identification. Being recent is *corroboration* - it helps
    choose between things that already fit - and it cannot establish that a document is a
    conversation with Rushi.

    So:

    * **Pointing** - "this", "that", "the one I was just looking at" - means the owner is indicating
      something by its presence rather than by its properties. Recency and foreground are exactly the
      right evidence, and anything recent is eligible.
    * **Describing** - "Rushi's chat", "the budget spreadsheet" - means the owner named properties.
      At least one of them has to actually match: the kind, the qualifier, or the noun. Nothing
      matching means V.O.I.D has not found what was described, and should say so rather than offer
      the nearest recent thing.

    Deliberately permissive within "pointing", and deliberately not a hard kind filter: ``score``
    keeps treating a kind mismatch as evidence against rather than disqualifying, because "open this
    file" can reasonably mean a document shown in a tab.
    """
    if not (reference.kind or reference.qualifier or reference.noun):
        return True
    # A NAMED thing must be matched by name. Kind agreement alone is not identification when the
    # owner said which one they meant: "open my Flurbleglorp folder" offered "VibeCoding" and
    # "Studies" - two folders that matched the *kind* and nothing else, scored on recency, and tied
    # into a question about the wrong things. A name that matches nothing has found nothing.
    #
    # Checked before deixis deliberately: "open that Flurbleglorp folder" names one just as
    # definitely as the version without "that".
    if reference.qualifier:
        return _name_score(candidate, reference.qualifier)[0] > 0
    if reference.deictic or reference.recency:
        return True
    if reference.kind and candidate.kind == reference.kind:
        return True
    if reference.noun:
        haystack = f"{candidate.label} {candidate.detail}".lower()
        if reference.noun in haystack or reference.noun in _extension_nouns(haystack):
            return True
    return False


def score(candidate: Candidate, reference: Reference, now: float | None = None) -> tuple[float, list[str]]:
    """Score one candidate against a reference, with the reasons.

    The reasons are returned alongside the number because the owner may need to hear *why* V.O.I.D decided
    "this chart" meant a particular file, and a bare score cannot answer that.
    """
    moment = time.time() if now is None else now
    total = 0.0
    reasons: list[str] = []

    if reference.kind:
        if candidate.kind == reference.kind:
            total += W_KIND
            reasons.append(f"it is a {reference.kind}")
        else:
            # Not disqualifying: "open this file" can reasonably mean a document shown in a tab. But an
            # explicit kind that does not match is strong evidence against.
            total -= W_KIND / 2

    gained, why = _name_score(candidate, reference.qualifier)
    if gained:
        total += gained
        reasons.append(why)

    if reference.noun:
        # The literal noun, used as a naming hint. "chart", "deck" and "spreadsheet" all map to the single
        # coarse kind "document", so the kind match alone cannot separate a chart from a budget workbook;
        # the word the owner actually said can. Generic - nothing here knows what a chart is, only that the
        # owner's noun appears in this thing's name and not in the other's.
        haystack = f"{candidate.label} {candidate.detail}".lower()
        if reference.noun in haystack:
            total += W_NOUN_HINT
            reasons.append(f"its name mentions '{reference.noun}'")
        elif reference.noun in _extension_nouns(haystack):
            # "that spreadsheet" matching a .xlsx whose name never says "spreadsheet".
            total += W_NOUN_HINT
            reasons.append(f"it is a {reference.noun}")

    if candidate.foreground:
        total += W_FOREGROUND
        reasons.append("it is what you are looking at")

    if candidate.at:
        age = max(0.0, moment - candidate.at)
        if age < RECENCY_WINDOW_S:
            decayed = W_RECENCY * (1.0 - age / RECENCY_WINDOW_S)
            total += decayed
            if decayed > W_RECENCY / 2:
                reasons.append("you were using it just now")

    if candidate.produced:
        total += W_PRODUCED
        reasons.append("I created it for you")

    return total, reasons


class ReferenceResolver:
    """Resolves referring expressions against candidates from registered providers.

    A provider is any zero-argument callable returning candidates. They are registered rather than
    hard-coded so that what V.O.I.D can refer to grows with its capabilities: the browser layer offers
    tabs, the desktop layer offers windows, the artifact layer offers documents it made. The resolver
    itself stays unaware of all of them.
    """

    def __init__(self, providers=None):
        self._providers = list(providers or [])

    def add_provider(self, provider) -> None:
        self._providers.append(provider)

    def candidates(self) -> list[Candidate]:
        """Everything currently referenceable. A provider that fails is skipped, not fatal.

        One unavailable layer must not make reference resolution impossible - if the browser is closed,
        "open that window" should still work.
        """
        found: list[Candidate] = []
        for provider in self._providers:
            try:
                offered = provider() or ()
            except Exception as exc:                            # noqa: BLE001
                _log.info("REFERENCE_PROVIDER_FAILED kind=%s", type(exc).__name__)
                continue
            for candidate in offered:
                if isinstance(candidate, Candidate) and candidate.target:
                    found.append(candidate)
        return found

    def resolve(self, phrase: str, now: float | None = None) -> Resolution:
        """Resolve a phrase to one target, or report ambiguity.

        Returns a :class:`Resolution` rather than raising, because "I am not sure which one you mean" is a
        normal, useful outcome - not an error.
        """
        reference = parse_reference(phrase)
        resolution = Resolution(reference=reference)
        if not reference.resolvable:
            return resolution

        ranked = []
        for candidate in self.candidates():
            # A positive score is not enough: it can be accumulated entirely from recency and
            # "I produced it", neither of which says the candidate IS what was described. See
            # :func:`identifies`.
            if not identifies(candidate, reference):
                continue
            value, reasons = score(candidate, reference, now=now)
            if value > 0:
                ranked.append((value, candidate, reasons))
        if not ranked:
            return resolution

        ranked.sort(key=lambda row: -row[0])
        resolution.alternatives = [row[1] for row in ranked[:5]]
        resolution.scores = {row[1].target: round(row[0], 1) for row in ranked[:5]}

        best, runner_up = ranked[0], ranked[1] if len(ranked) > 1 else None
        if runner_up is None or (best[0] - runner_up[0]) >= DECISIVE_MARGIN:
            resolution.choice = best[1]
            resolution.reasons = best[2]
            resolution.alternatives = [row[1] for row in ranked[1:5]]
            _log.info("REFERENCE_RESOLVED kind=%s source=%s score=%.1f",
                      best[1].kind, best[1].source, best[0])
        else:
            # Deliberately undecided. Two plausible referents and no clear winner is exactly the case where
            # guessing is worse than asking.
            _log.info("REFERENCE_AMBIGUOUS count=%d top=%.1f second=%.1f",
                      len(ranked), best[0], runner_up[0])
        return resolution
