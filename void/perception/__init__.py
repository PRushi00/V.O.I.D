"""V.O.I.D's perception layer: reading the computer, from the cheapest reliable source first.

The blueprint asks for one normalised representation across several very different sources - DOM,
accessibility tree, OS/application state, OCR, screenshots, vision. This package provides the smallest
abstraction that actually serves V3's needs rather than a universal schema nothing fits comfortably:

    Observation      what was read, from where, how confident, and what it contains
    ObservationKind  the source, ranked - structured sources before visual ones
    Element          one addressable thing (a control, a link, a field) with a semantic handle

**Structured before visual, always.** `ObservationKind.rank` encodes the preference the resolver and the
verifier both rely on: an accessibility tree answers "is there a Send button?" exactly, cheaply, and without
capturing anything else on the owner's screen. A screenshot answers it approximately, expensively, and
captures everything. Vision is a perception mechanism of last resort, not a default.

**Perception is untrusted data.** This is the single most important property in the package. A page title, a
control's label, OCR output and a vision model's description are all written by someone other than the
owner, and a screen can contain text crafted to be read by an agent. So:

  * nothing here authorizes anything - an Observation has no risk level, no permission and no decision;
  * text is length-bounded and stripped of control characters at the source, so nothing downstream has to
    remember to;
  * `Observation.trusted` is always False, and there is deliberately no way to set it True.

**Nothing is persisted.** No screenshot is written to disk, no observation is cached to a file. An
observation exists for the length of one answer. Screen content is the most sensitive thing V.O.I.D can
read, and the safest place for it is nowhere.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field

#: Text from any perception source is truncated to this. Generous enough for a page's worth of labels,
#: bounded so a hostile page cannot flood a model's context or a log.
MAX_TEXT = 4000

#: One element's name/label.
MAX_LABEL = 200

#: How many elements one observation carries. A real page has thousands of nodes; an agent needs the
#: interactive ones, and a model cannot use a thousand of them usefully.
MAX_ELEMENTS = 120

#: Control characters and the bidirectional overrides that make text display as something other than what
#: it is. Stripped from every label and text block as it enters V.O.I.D.
_UNSAFE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


class ObservationKind:
    """Where an observation came from, ranked by preference. Lower rank is better.

    The ordering is the blueprint's perception preference as data: structured application state is exact,
    an accessibility tree is exact and cheap, a DOM is exact, OCR is a guess about pixels, and a vision
    model is an interpretation. Prefer the top of this list and you get answers that are both cheaper and
    more reliable - the two usually coincide here.
    """

    APP_STATE = "app_state"            # the application's own structured state
    ACCESSIBILITY = "accessibility"    # the UIA / AX tree - exact, cheap, no pixels
    DOM = "dom"                        # a page's document model
    WINDOW_STATE = "window_state"      # OS window list: titles, bounds, foreground
    OCR = "ocr"                        # text recognised from pixels
    SCREENSHOT = "screenshot"          # raw pixels, no interpretation
    VISION = "vision"                  # a model's description of pixels

    _ORDER = (APP_STATE, ACCESSIBILITY, DOM, WINDOW_STATE, OCR, SCREENSHOT, VISION)
    ALL = frozenset(_ORDER)
    #: Sources that read pixels. These cost more, see more than they were asked about, and may require
    #: egress - so a caller that can avoid them should.
    VISUAL = frozenset({OCR, SCREENSHOT, VISION})

    @classmethod
    def rank(cls, kind: str) -> int:
        try:
            return cls._ORDER.index(kind)
        except ValueError:
            return len(cls._ORDER)


def clean_text(value: object, limit: int = MAX_TEXT) -> str:
    """Text safe to show the owner, log, or put in a model's context.

    Strips control and direction-override characters, normalises to NFC, collapses whitespace runs and
    truncates. Applied at the point text enters V.O.I.D from a page, a control or a model - once, at the
    source, rather than hopefully at every use.
    """
    if value is None:
        return ""
    try:
        text = unicodedata.normalize("NFC", str(value))
    except Exception:                                          # noqa: BLE001
        return ""
    text = _UNSAFE.sub("", text)
    # Collapse runs of whitespace but keep single newlines: a page's structure is information.
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text[:limit] if limit and len(text) > limit else text


@dataclass(frozen=True)
class Element:
    """One addressable thing an action could target.

    ``handle`` is how a capability refers back to this element later - a CSS selector, a UIA runtime id, a
    semantic locator. It is produced by the adapter that found the element, never by a model, and it is the
    only part of an Element that an action is allowed to act on. A model may *choose* an element from a
    list; it cannot invent a handle, because a handle it made up will not match anything the adapter knows.
    """

    #: What kind of thing: "button", "link", "textbox", "menuitem", ... The adapter's own role name,
    #: lower-cased. Deliberately not a closed set - UIA and the DOM have different vocabularies and
    #: flattening them would lose information an action needs.
    role: str
    #: The accessible name / label / visible text. UNTRUSTED: chosen by the page or application.
    name: str = ""
    #: The adapter's reference to this element. Opaque to everything above the adapter.
    handle: str = ""
    #: Can it be interacted with right now?
    enabled: bool = True
    #: Is it on screen / in the viewport?
    visible: bool = True
    #: (x, y, width, height) where the adapter knows it. For verification and for the rare case where a
    #: coordinate fallback is genuinely the only route.
    bounds: tuple[int, int, int, int] | None = None
    #: Current value, for a field. UNTRUSTED and bounded.
    value: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", clean_text(self.role, 40).lower())
        object.__setattr__(self, "name", clean_text(self.name, MAX_LABEL))
        object.__setattr__(self, "value", clean_text(self.value, MAX_LABEL))

    @property
    def actionable(self) -> bool:
        return bool(self.enabled and self.visible and self.handle)

    def as_dict(self) -> dict:
        out = {"role": self.role, "name": self.name, "handle": self.handle}
        if not self.enabled:
            out["enabled"] = False
        if not self.visible:
            out["visible"] = False
        if self.value:
            out["value"] = self.value
        return out

    def describe(self) -> str:
        """One short line for a model or a spoken answer."""
        label = self.name or "(unlabelled)"
        tail = "" if self.actionable else " [not actionable]"
        return f"{self.role}: {label}{tail}"


@dataclass(frozen=True)
class Observation:
    """What one perception source read, at one moment.

    Frozen because an observation is a record of a moment. The computer changes; re-read rather than edit.
    ``at`` matters for exactly that reason - an observation is evidence about the past, and a caller
    deciding whether to trust it should be able to see how old it is.
    """

    kind: str
    #: What was observed: a window title, a url, an application name. UNTRUSTED and bounded.
    subject: str = ""
    #: Readable text content, where the source produces any. UNTRUSTED and bounded.
    text: str = ""
    elements: tuple[Element, ...] = ()
    at: float = field(default_factory=time.time)
    #: 0..1. How much the SOURCE is to be relied on, not how plausible the content is. An accessibility
    #: tree is high; a vision model's description of a blurry screenshot is low.
    confidence: float = 1.0
    #: Where this came from, in words: "uia", "playwright-dom", "gdi+gemini". Program-controlled.
    provenance: str = ""
    #: Why the source could not answer, when it could not. Mirrors void.system.Reading's honesty rule:
    #: "I could not read this" is a different answer from "there is nothing there".
    unavailable: str = ""

    def __post_init__(self) -> None:
        if self.kind not in ObservationKind.ALL:
            raise ValueError(f"unknown observation kind: {self.kind!r}")
        object.__setattr__(self, "subject", clean_text(self.subject, MAX_LABEL))
        object.__setattr__(self, "text", clean_text(self.text))
        object.__setattr__(self, "provenance", clean_text(self.provenance, 60))
        object.__setattr__(self, "unavailable", clean_text(self.unavailable, 200))
        object.__setattr__(self, "confidence", min(1.0, max(0.0, float(self.confidence))))
        if len(self.elements) > MAX_ELEMENTS:
            object.__setattr__(self, "elements", tuple(self.elements[:MAX_ELEMENTS]))

    @property
    def trusted(self) -> bool:
        """Always False. There is deliberately no way to make an observation trusted.

        Everything in an Observation was written by someone other than the owner - a page author, an
        application, a model interpreting pixels. It is evidence about the world, never an instruction and
        never an authorization. Code that wants to *act* on what it read goes through the capability layer,
        where the action is authorized on its own merits.
        """
        return False

    @property
    def ok(self) -> bool:
        return not self.unavailable and bool(self.text or self.elements or self.subject)

    @property
    def visual(self) -> bool:
        return self.kind in ObservationKind.VISUAL

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.at)

    def find(self, name: str, role: str | None = None) -> tuple[Element, ...]:
        """Elements whose name contains ``name``, optionally of one role. Best-first by exactness.

        Matching happens here, over names already read and cleaned. A caller's string never reaches the
        adapter as a query - it is compared to what was observed.
        """
        needle = clean_text(name, MAX_LABEL).lower()
        if not needle:
            return ()
        wanted_role = clean_text(role, 40).lower() if role else None
        exact, partial = [], []
        for element in self.elements:
            if wanted_role and element.role != wanted_role:
                continue
            label = element.name.lower()
            if label == needle:
                exact.append(element)
            elif needle in label:
                partial.append(element)
        return tuple(exact + partial)

    def actionable(self) -> tuple[Element, ...]:
        return tuple(element for element in self.elements if element.actionable)

    def as_dict(self, *, include_text: bool = True) -> dict:
        out: dict = {"kind": self.kind, "subject": self.subject,
                     "confidence": round(self.confidence, 2),
                     "provenance": self.provenance, "age_s": round(self.age_s, 1),
                     "elements": [element.as_dict() for element in self.elements]}
        if include_text and self.text:
            out["text"] = self.text
        if self.unavailable:
            out["unavailable"] = self.unavailable
        return out

    def summarise(self, limit: int = 400) -> str:
        """A short description for a spoken answer, honest about what could not be read."""
        if self.unavailable:
            return f"I could not read that: {self.unavailable}"
        parts = []
        if self.subject:
            parts.append(self.subject)
        if self.elements:
            parts.append(f"{len(self.elements)} interactive element(s)")
        if self.text:
            parts.append(self.text[:limit])
        return " - ".join(parts) if parts else "nothing readable"


def unavailable(kind: str, reason: str, *, provenance: str = "") -> Observation:
    """An honest empty observation. Used instead of returning None or an empty one that looks successful."""
    return Observation(kind=kind, unavailable=reason, provenance=provenance, confidence=0.0)


def best(observations) -> Observation | None:
    """The most reliable usable observation from several sources.

    Ranked by source preference first, then confidence - so an accessibility tree beats a vision
    description even if the model sounded certain. This is the function that keeps "prefer structured
    information" from being a thing everyone has to remember.
    """
    usable = [observation for observation in observations if observation is not None and observation.ok]
    if not usable:
        return None
    usable.sort(key=lambda observation: (ObservationKind.rank(observation.kind),
                                         -observation.confidence))
    return usable[0]
