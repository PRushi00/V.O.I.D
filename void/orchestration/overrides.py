"""Reading an explicit application choice out of what the owner actually said.

A stored preference is a default, not an instruction. "Open Gmail" should use the preferred browser;
"Open Gmail in Edge" should use Edge, and the preference should lose without argument. That is the one
piece of precedence the route resolver could not express before: it knew the stored preference
(:meth:`WorldState.preferred`) but had no way to hear the owner contradict it.

This module extracts that contradiction, deterministically, with no model involved.

**Why it is not a pile of special cases.** The only domain knowledge here is a vocabulary of role
words, and each vocabulary is read from the layer that can actually act on it - browsers from
:data:`void.browser.playwright_adapter._BROWSERS`, the same table the browser layer uses to launch
one, and messengers from :data:`void.orchestration.messaging.MESSAGING_APPS`. So "Edge" is
recognised because V.O.I.D genuinely knows how to drive Edge, and "Discord" because it genuinely
knows how to reach a conversation inside it - not because a branch was written for either. Adding a
role means adding its vocabulary, not editing the resolver.

**Why it refuses more than it accepts.** An override changes which application runs, so a false
positive is worse than a miss: "open the invoice in Documents" must not be read as an application
choice. Three things keep it narrow:

* the phrase has to look like a choice ("in X", "with X", "using X", "use X for"), not merely contain a
  name - "Edge is slow, open Gmail" does not select Edge;
* the named thing has to be in a known role vocabulary;
* anything else yields nothing at all, and routing proceeds on the stored preference as before.

An override is still only a *signal*. It selects between applications V.O.I.D can already drive; it
cannot name something unavailable into existence, cannot widen a capability, and is dropped by the
resolver exactly like any other route whose machinery is missing.
"""
from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

#: Roles an utterance may override. Deliberately the same closed vocabulary the application registry
#: already understands (:data:`void.orchestration.apps.PREFERENCE_KEYS`), so an override and a stored
#: preference are expressed in the same terms and can be compared directly.
OVERRIDABLE_ROLES = frozenset({"browser", "editor", "terminal", "music", "mail", "messaging"})

#: Phrases that mark a deliberate choice of application, rather than a passing mention. The group is
#: the named application. Ordered longest-intent first so "use X for this" is not matched as "use X".
_CHOICE_PATTERNS = (
    re.compile(r"\buse\s+(?P<name>[\w .+-]{2,30}?)\s+(?:for|to|on)\b", re.IGNORECASE),
    re.compile(r"\b(?:in|with|using|via|through)\s+(?P<name>[\w .+-]{2,30}?)"
               r"(?=\s*(?:$|[,.;!?]|\bplease\b|\binstead\b|\bthis\b|\bthat\b|\bit\b))",
               re.IGNORECASE),
    re.compile(r"\bopen\s+(?:it|this|that)\s+(?:in|with)\s+(?P<name>[\w .+-]{2,30})", re.IGNORECASE),
    re.compile(r"\buse\s+(?P<name>[\w .+-]{2,30})\s*$", re.IGNORECASE),
)

#: Words that are never an application name even when they appear in a choice phrase. Without this,
#: "open it in the background" reads as an application called "the background".
_NOT_AN_APP = frozenset({
    "the", "a", "an", "it", "this", "that", "them", "there", "here", "background", "foreground",
    "fullscreen", "full screen", "private", "incognito", "secret", "new", "another", "same",
    "order", "place", "mind", "future", "general", "particular", "short", "detail", "full",
    "advance", "progress", "person", "time", "case", "fact", "addition", "mine", "yours",
})


def browser_vocabulary() -> frozenset[str]:
    """Browser names V.O.I.D can actually drive, read from the browser layer's own table.

    Reused rather than restated: if the browser layer learns to drive another browser, this recognises
    it the same day, and there is no second list to drift out of step.
    """
    names: set[str] = set()
    try:
        from void.browser.playwright_adapter import _BROWSERS
        for row in _BROWSERS:
            name = str(row[0]).strip().lower()
            if name:
                names.add(name)
                # "opera gx" should also be selectable as "opera gx"'s last word is not a browser on
                # its own, but "msedge" (the channel) is how Windows names Edge.
            channel = str(row[1]).strip().lower() if len(row) > 1 else ""
            if channel:
                names.add(channel)
    except Exception:                                          # noqa: BLE001 - vocabulary is optional
        _log.debug("BROWSER_VOCABULARY_UNAVAILABLE", exc_info=True)
    return frozenset(names)


#: Spoken forms that mean a vocabulary entry. These RESOLVE TO a canonical name rather than becoming
#: names themselves: the resolver and the browser layer both key on the table's own spelling, so
#: returning "microsoft edge" where "edge" is expected would route nowhere. An alias whose target is
#: not driveable on this machine is ignored, so this cannot invent a capability.
ALIASES = {
    "microsoft edge": "edge",
    "ms edge": "edge",
    "google chrome": "chrome",
    "opera gx browser": "opera gx",
    "operagx": "opera gx",
}


def messaging_vocabulary() -> frozenset[str]:
    """Messaging application names V.O.I.D can recognise and reach.

    Read from :data:`void.orchestration.messaging.MESSAGING_APPS` for the same reason the browser
    vocabulary is read from the browser layer's own table: one list, owned by the layer that can
    actually act, so the two cannot drift apart. Imported lazily so this module stays importable
    when the messaging layer is not.
    """
    try:
        from void.orchestration.messaging import messaging_vocabulary as surfaces
        return surfaces()
    except Exception:                                          # noqa: BLE001 - vocabulary is optional
        _log.debug("MESSAGING_VOCABULARY_UNAVAILABLE", exc_info=True)
        return frozenset()


def role_vocabularies() -> dict[str, frozenset[str]]:
    """Role -> the application names that count for it.

    A role appears here only when the repository holds real capability data for it: ``browser``
    because the browser layer can drive one, and ``messaging`` because
    :mod:`void.orchestration.messaging` can reach a conversation inside one. The remaining
    preference keys stay absent on purpose - recognising an override for a role V.O.I.D cannot act on
    would be a promise it could not keep.

    An empty vocabulary is dropped rather than offered, so a layer that fails to import costs the
    owner that role's overrides and nothing else.
    """
    claimed = {"browser": browser_vocabulary(), "messaging": messaging_vocabulary()}
    return {role: entries for role, entries in claimed.items() if entries}


def _canonical(name: str, vocabulary) -> str | None:
    """The vocabulary entry a spoken name refers to, or None.

    Longest match first, so "opera gx" wins over "opera" when both are present - picking the shorter
    one would silently route to a different browser than the owner named.
    """
    probe = " ".join((name or "").strip().lower().split())
    if not probe or probe in _NOT_AN_APP:
        return None
    # An alias resolves to the table's own spelling, and only if that spelling is actually driveable
    # here - so "microsoft edge" becomes "edge", and becomes nothing on a machine without Edge.
    for alias in sorted(ALIASES, key=len, reverse=True):
        if alias in probe:
            target = ALIASES[alias]
            return target if target in vocabulary else None
    for entry in sorted(vocabulary, key=len, reverse=True):
        if probe == entry or probe.startswith(entry + " ") or probe.endswith(" " + entry):
            return entry
        if entry in probe.split():
            return entry
    return None


def extract_overrides(goal: str, vocabularies=None) -> dict[str, str]:
    """``{role: application}`` the owner explicitly asked for in this utterance.

    Empty when they did not - which is the common case, and the case in which routing behaves exactly
    as it did before this module existed.
    """
    text = " ".join((goal or "").split())
    if not text:
        return {}
    vocab = vocabularies if vocabularies is not None else role_vocabularies()
    found: dict[str, str] = {}
    for pattern in _CHOICE_PATTERNS:
        for match in pattern.finditer(text):
            named = match.group("name")
            for role, entries in vocab.items():
                if role not in OVERRIDABLE_ROLES or not entries:
                    continue
                canonical = _canonical(named, entries)
                if canonical and role not in found:
                    found[role] = canonical
    if found:
        # The ROLE and the chosen application are both program-controlled vocabulary, so logging them
        # discloses nothing about what the owner is doing.
        _log.info("ROUTE_OVERRIDE_REQUESTED %s",
                  " ".join(f"{role}={app}" for role, app in sorted(found.items())))
    return found
