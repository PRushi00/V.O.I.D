"""What V.O.I.D says when the owner drifts, and the one place a distraction gets a name.

Two things live here and nothing else, because both are taste and both must be editable without
reading the engine: the escalating pools of callout lines, and the map that turns a host into a word
a person would say.

**The privacy law, which is the whole reason this is a module and not a few f-strings.** A label - the
name of the site or application the owner drifted into - is derived here, handed to the engine for the
single tick that speaks it, and stored nowhere. Not in the session state, not in the ledger, not in
the report card, not in memory, not in a log line. :func:`callout` returns a finished sentence; the
caller never keeps the label it was built from. ``tests/test_focus.py`` proves this with a canary
label that appears in no canned text anywhere in the repository, which is the only honest way to test
it - a test that greps for "Instagram" is fooled by the example sentences in this file.

Four lines per tier, deliberately. Three loops audibly within one long drift, and a pool that repeats
is worse than silence: it tells the owner the assistant is a machine reading from a list, which is
exactly when they stop listening to it.
"""
from __future__ import annotations

import random
import re

#: Hosts whose spoken name is not their domain. Short, and only for places people name out loud; an
#: unmapped host is spoken as its bare domain, which reads fine ("example.com can wait").
SPOKEN_HOSTS: dict[str, str] = {
    "instagram.com": "Instagram", "youtube.com": "YouTube", "x.com": "X", "twitter.com": "X",
    "reddit.com": "Reddit", "tiktok.com": "TikTok", "netflix.com": "Netflix",
    "facebook.com": "Facebook", "linkedin.com": "LinkedIn", "news.ycombinator.com": "Hacker News",
    "twitch.tv": "Twitch", "mail.google.com": "Gmail", "web.whatsapp.com": "WhatsApp",
    "discord.com": "Discord", "steampowered.com": "Steam", "amazon.com": "Amazon",
    "ebay.com": "eBay", "pinterest.com": "Pinterest", "spotify.com": "Spotify",
}

#: Leading labels stripped from a host before it is spoken: "www.example.com" is "example.com".
_HOST_NOISE = re.compile(r"\A(?:www|m|mobile|old|new)\.", re.IGNORECASE)

#: Callout pools, by how long the owner has been away. Each line takes one ``{what}`` placeholder.
#: Tier 1 is the first callout of an excursion, 2 the second, 3 the third and after.
NAMED_LINES: dict[int, tuple[str, ...]] = {
    1: ("{what} can wait.",
        "That is {what}, not the work.",
        "{what}. We had an arrangement.",
        "I see {what}. The timer does not."),
    2: ("Twice into {what} now. It is starting to look deliberate.",
        "{what} again. I am keeping count, since you are not.",
        "Still {what}. The work has not moved.",
        "Second trip to {what}. The timer is unimpressed."),
    3: ("Third time in {what}. The detours are becoming the project.",
        "{what} is winning, and it is not close.",
        "At this point {what} is the session and the work is the distraction.",
        "{what}, again. I will stop pretending to be surprised."),
}

#: The same pools for when naming is switched off, or the surface could not be named.
NAMELESS_LINES: dict[int, tuple[str, ...]] = {
    1: ("That is not what we sat down for.",
        "Wrong window. The timer is still running.",
        "Back to it, please.",
        "This is not the work."),
    2: ("Twice now. The work has not moved.",
        "Second excursion. I am keeping count.",
        "Still away. Still counting.",
        "Again. The timer did not pause."),
    3: ("Third time away. The detours are becoming the project.",
        "The session is mostly elsewhere now.",
        "This is no longer a detour.",
        "I will stop pretending to be surprised."),
}

#: The first callout of a session says what the owner told it they were working on, when they said.
#: One line, because the point is to make the contrast audible, not to lecture.
FIRST_WITH_INTENT = "{what} does not look like {intent} to me."
FIRST_WITH_INTENT_NAMELESS = "This does not look like {intent} to me."

#: The highest tier there is. A fourth excursion reads from tier 3; escalating forever would mean
#: writing lines nobody hears.
MAX_TIER = 3


def spoken_host(host: object) -> str:
    """A word a person would say for a host. Never empty for a non-empty host."""
    text = str(host or "").strip().lower().rstrip(".")
    if not text:
        return ""
    bare = _HOST_NOISE.sub("", text)
    if bare in SPOKEN_HOSTS:
        return SPOKEN_HOSTS[bare]
    # A known host reached through a subdomain still gets its name ("music.youtube.com").
    for known, name in SPOKEN_HOSTS.items():
        if bare.endswith("." + known):
            return name
    return bare


def callout(tier: int, label: str = "", *, intent: str = "", first: bool = False,
            pick=random.choice) -> str:
    """One spoken line. ``label`` is used and discarded; nothing here retains it.

    ``pick`` is injectable so a test can assert the line rather than the pool.
    """
    tier = max(1, min(int(tier or 1), MAX_TIER))
    what = str(label or "").strip()
    if first and intent:
        template = FIRST_WITH_INTENT if what else FIRST_WITH_INTENT_NAMELESS
        return template.format(what=what, intent=str(intent).strip())
    pool = NAMED_LINES[tier] if what else NAMELESS_LINES[tier]
    return pick(pool).format(what=what)
