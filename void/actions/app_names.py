"""Deterministic normalisation and name matching for installed applications.

Pure functions only: no Windows API, no filesystem, no model, no I/O of any kind. The application catalog
(``void/actions/computer.py``) builds its indexes with these, and ``AppCatalog.resolve_name`` walks the matching
hierarchy over them. Keeping the string logic here means it can be reasoned about and tested on its own, and that
the matching rules cannot quietly acquire a dependency on anything the OS or a model says.

The design constraint is that a WRONG match launches a program the owner did not ask for, so every rule here is
exact-by-construction: names are made comparable (case, punctuation, spacing, vendor decoration), never *approximate*.
There is no edit distance, no substring containment and no scoring - a query either normalises to the same thing as
an installed name, is a whole-word prefix of it, or does not match. Ambiguity is a first-class outcome, never a
tie-break.
"""
from __future__ import annotations

import re

# Characters that merely SEPARATE words in an application name: a display name differs from what someone says only
# by these. "Opera-GX", "Opera_GX", "Opera (GX)" and "Opera GX" are the same name.
_SEPARATORS = "-_/\\|,;:.()[]{}<>!?\"·–—‐‑‒―~®™©*"
_SEP_MAP = {ord(c): " " for c in _SEPARATORS}
# Characters KEPT inside a word. '+' and '#' carry meaning ("Notepad++", "C#") and dropping them would make
# different programs compare equal, so they are never stripped.
_KEEP = re.compile(r"[^a-z0-9 +#&']")
_WS = re.compile(r"\s+")

# Vendor words an installed name carries but nobody says out loud ("Microsoft Teams" -> "teams"). Deliberately short,
# and deliberately WITHOUT "windows": "Windows Security" and a hypothetical "Security" are not the same program.
PUBLISHER_PREFIXES = frozenset({
    "microsoft", "google", "intel", "nvidia", "amd", "asus", "oracle", "adobe", "apple", "realtek", "logitech",
    "corsair", "razer", "dell", "hp", "lenovo", "blackmagic", "mozilla", "jetbrains"})
# A word only belongs above when it is PURELY a vendor. "Opera" is excluded deliberately: it is also the product,
# so stripping it would let "gx browser" resolve to "Opera GX Browser" - a name the owner never said.

# The whole alias table (section 8: only genuinely useful, genuinely safe naming differences - NOT an application
# list). Everything else resolves from the installed display name itself. Keys and values are already normalised.
ALIASES: dict[str, str] = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "vs studio code": "visual studio code",
}


def normalise(name: object) -> str:
    """Case-, punctuation- and spacing-insensitive form of an application name. Never raises."""
    if not isinstance(name, str):
        return ""
    s = name.casefold().translate(_SEP_MAP)
    s = _KEEP.sub(" ", s)
    return _WS.sub(" ", s).strip()


def _merge_letter_runs(words: tuple[str, ...]) -> tuple[str, ...]:
    """Join runs of two or more single-character words: speech-to-text spells acronyms out ("opera g x").

    An installed display name practically never contains two adjacent one-character words, so this only ever
    reassembles what a transcript took apart - it cannot join meaningful words together.
    """
    out: list[str] = []
    run: list[str] = []
    for w in words:
        if len(w) == 1:
            run.append(w)
            continue
        if len(run) >= 2:
            out.append("".join(run))
        else:
            out.extend(run)
        run = []
        out.append(w)
    if len(run) >= 2:
        out.append("".join(run))
    else:
        out.extend(run)
    return tuple(out)


def tokens(name: object) -> tuple[str, ...]:
    """Whole words of the normalised name, with spelled-out letter runs reassembled."""
    n = normalise(name)
    return _merge_letter_runs(tuple(n.split(" "))) if n else ()


def squash(name: object) -> str:
    """The name with every separator removed: "Whats App", "whatsapp" and "Whats-App" all become "whatsapp".

    This is an IDENTITY comparison, never a prefix one: it settles how a name is spaced, not which name it is.
    """
    return "".join(tokens(name))


def is_prefix(query_words: tuple[str, ...], name_words: tuple[str, ...]) -> bool:
    """True when the query is the leading WHOLE words of the name, in order.

    ``("opera", "gx")`` is a prefix of ``("opera", "gx", "browser")``; "gx", "oper" and ("gx", "opera") are not.
    """
    return (bool(query_words) and len(name_words) >= len(query_words)
            and name_words[:len(query_words)] == query_words)


# Words the OWNER adds that an installed name may omit. "windows" belongs here but NOT in PUBLISHER_PREFIXES:
# dropping it from a query ("open windows terminal" -> the installed "Terminal") is safe because the exact tiers
# run first, while dropping it from an installed NAME would make "Windows Security" answer to "security".
QUERY_QUALIFIERS = PUBLISHER_PREFIXES | {"windows"}


def without_publisher(name_words: tuple[str, ...]) -> tuple[str, ...] | None:
    """The name with a leading vendor word removed ("microsoft teams" -> "teams"), or None if there is none.

    Only ever a *fallback* tier: a program genuinely called "Teams" still wins, because the exact tiers run first.
    """
    if len(name_words) >= 2 and name_words[0] in PUBLISHER_PREFIXES:
        return name_words[1:]
    return None


def without_qualifier(query_words: tuple[str, ...]) -> tuple[str, ...] | None:
    """The query with a leading vendor/platform word removed ("windows terminal" -> "terminal"), or None.

    The mirror image of ``without_publisher``: people say the vendor an installed name leaves out just as often as
    they leave out one it carries. Never strips a query down to nothing.
    """
    if len(query_words) >= 2 and query_words[0] in QUERY_QUALIFIERS:
        return query_words[1:]
    return None


# --- how a name SOUNDS ----------------------------------------------------------------------------
#
# Consonant classes English speech-to-text genuinely confuses, applied to the squashed phrase so spacing and
# punctuation are already gone. This is a canonicalisation, exactly like ``squash``: it decides how a name is
# SPELLED, not which name it is. There is no distance and no threshold - a query either sounds exactly like an
# installed application or it does not.
_SOUND_CLASSES = {
    **dict.fromkeys("pb", "P"), **dict.fromkeys("fv", "F"), **dict.fromkeys("td", "T"),
    **dict.fromkeys("kgqc", "K"), **dict.fromkeys("sz", "S"), **dict.fromkeys("mn", "N"),
    **dict.fromkeys("lr", "L"), **dict.fromkeys("jx", "J"),
}
_SILENT = frozenset("hwy'")
_VOWELS = frozenset("aeiou")
_DIGRAPHS = (("ch", "J"), ("sh", "J"), ("ph", "F"), ("gh", "K"), ("ck", "K"))

#: Keys shorter than this carry too little information to identify a program. Measured: at 4 the tier recovers
#: 10 of 11 real speech-to-text failures with ZERO false positives over 69 ordinary phrases and common words;
#: at 3 it starts matching "could" to Claude, "would" to Word and "other" to Weather.
MIN_SOUND_KEY = 4


def sound_key(name: object) -> str:
    """How a name sounds, as a short canonical key. "" when there is nothing to key on."""
    s = "".join(tokens(name))
    if not s:
        return ""
    for pair, rep in _DIGRAPHS:
        s = s.replace(pair, rep)
    out = []
    for ch in s:
        if ch in _SILENT:
            continue
        out.append("A" if ch in _VOWELS else _SOUND_CLASSES.get(ch, ch.upper()))
    collapsed: list[str] = []
    for ch in out:
        if not collapsed or collapsed[-1] != ch:
            collapsed.append(ch)
    # Vowels survive only in the leading position: which vowel was heard is the least reliable part of a
    # mishearing ("not pad" / "notepad"), while the opening sound is the most reliable.
    return "".join(collapsed[:1] + [c for c in collapsed[1:] if c != "A"])


def canonical_query(query: object) -> tuple[str, ...]:
    """The query as whole words, after applying the alias table. Empty when there is nothing to match."""
    words = tokens(query)
    if not words:
        return ()
    alias = ALIASES.get(" ".join(words))
    return tokens(alias) if alias else words
