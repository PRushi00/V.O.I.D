"""Turning a spoken sentence into a website address - and refusing the ones that are not about one.

The REA pack's route rule, kept exactly: catch ONLY sentences that name a site. "Tear down the old
shed" and "how should I reverse engineer my pricing" must trigger nothing, because a teardown opens a
browser, runs a third-party analyser and writes a note, and doing all that because someone used the
word "teardown" in a sentence about a shed is the kind of eagerness that makes a feature untrustable.

Speech recognition writes addresses as words, so "igloo dot inc" has to become ``igloo.inc`` and
"stripe.com slash pricing" has to become ``stripe.com/pricing``. That substitution happens here and
nowhere else, and it is the only transformation applied: the result still goes through
``void.browser.safe_url`` before anything opens it, so one place decides what V.O.I.D will navigate to.
"""
from __future__ import annotations

import re

#: Spoken punctuation, longest first so "dot com" is not rewritten by the "dot" rule twice.
SPOKEN = (
    (r"\s+dot\s+", "."),
    (r"\s+slash\s+", "/"),
    (r"\s+colon\s+", ":"),
    (r"\s+dash\s+", "-"),
    (r"\s+hyphen\s+", "-"),
    (r"\s+underscore\s+", "_"),
)

#: Top-level domains a bare spoken host is accepted with. A closed list, because "take it apart" must
#: not read "apart" as a host, and an open rule on "any word with a dot" matches sentence punctuation.
#: Add the ones your own work touches.
KNOWN_TLDS = frozenset("""
com net org io inc ai co dev app sh me gg tv fm xyz art studio design so to us uk de fr nl es it
se no dk fi pl cz ch at be ie nz au ca jp cn in br mx ru za edu gov mil info biz tech cloud page
link live site online store shop blog wiki news media world space fun run build tools
""".split())

#: The sentences that ask for a teardown. Each must name something; a bare "tear this apart" is
#: caught only by the explicit this-page forms below.
_ASK = (
    re.compile(r"\A(?:reverse\s+engineer|tear\s+(?:down|apart)|take\s+apart|take\s+down|"
               r"deconstruct|dissect)\s+(?P<site>.+)\Z", re.IGNORECASE),
    re.compile(r"\Ahow\s+(?:does|did)\s+(?P<site>.+?)\s+(?:do\s+that|work|build\s+that|"
               r"pull\s+that\s+off|achieve\s+that)\s*\Z", re.IGNORECASE),
    re.compile(r"\Awhat\s+(?:is|was)\s+(?P<site>.+?)\s+(?:built|made)\s+with\s*\Z", re.IGNORECASE),
    re.compile(r"\Awhat\s+(?:is|does)\s+(?P<site>.+?)\s+(?:use|using|running\s+on)\s*\Z",
               re.IGNORECASE),
)

#: The forms that mean "the page I am looking at". Resolved by the caller, which is the only thing
#: that knows what the owner's front tab is - and which must never answer with V.O.I.D's own window.
THIS_PAGE = ("this site", "this page", "this website", "the site i'm on", "the page i'm on",
             "the site im on", "the page im on", "this one")

_LEAD = re.compile(r"\A(?:void|hey\s+void|ok(?:ay)?|alright|right|so|now|please|hey)[,\s]+",
                   re.IGNORECASE)

#: Stripped from the end of a named site: "for me", "would you", "please".
_TRAIL = re.compile(r"\s*(?:for\s+me|would\s+you|could\s+you|please|now|quickly)\s*\Z",
                    re.IGNORECASE)

_HOSTISH = re.compile(r"\A(?:https?://)?(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,})"
                      r"(?P<rest>[/:?][^\s]*)?\Z")
_LOCALHOST = re.compile(r"\A(?:https?://)?(?:localhost|127\.0\.0\.1)(?::\d{1,5})?(?:/[^\s]*)?\Z",
                        re.IGNORECASE)


def spoken_to_address(text: object) -> str:
    """Apply the spoken-punctuation substitutions. Does not validate."""
    out = " ".join(str(text or "").split())
    for pattern, replacement in SPOKEN:
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out.strip(" .,!?")


def _looks_like_a_site(candidate: str) -> bool:
    if _LOCALHOST.match(candidate):
        return True
    match = _HOSTISH.match(candidate)
    if match is None:
        return False
    tld = match.group("host").rsplit(".", 1)[-1].lower()
    return tld in KNOWN_TLDS


def parse(utterance: object) -> str | None:
    """The address a teardown request names, ``"this"`` for the current page, or None.

    None is the common case and the important one: a sentence that merely contains the words "tear
    down" is not a request to open a browser.
    """
    text = " ".join(str(utterance or "").split())
    while True:
        stripped = _LEAD.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    bare = text.strip(" .!,").rstrip("?")
    if not bare:
        return None
    for pattern in _ASK:
        match = pattern.match(bare)
        if match is None:
            continue
        said = _TRAIL.sub("", match.group("site")).strip(" .,!?")
        lowered = said.lower().removeprefix("the ").removeprefix("a ")
        if lowered in THIS_PAGE:
            return "this"
        candidate = spoken_to_address(said)
        # A possessive or a description is not an address: "Stripe's homepage" names a site but not
        # one we can open, so the brain is left to resolve it rather than guessing a host.
        if _looks_like_a_site(candidate):
            return candidate
        return None
    return None
