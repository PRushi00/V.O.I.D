"""Websites as routable entities: "Open YouTube".

A website is not an application, and treating it as one is why this failed. "Open YouTube" went to
the application matcher, which correctly reported that nothing called YouTube is installed, and then
to the model - which had to guess that the right move was ``navigate`` with a URL it invented. On the
owner's machine that cost 30-40 seconds of provider retries and succeeded only sometimes; once it
reported "I have opened the Wikipedia page" when the navigation had in fact been refused.

Meanwhile the browser layer works. Measured here: ``PlaywrightBrowser.navigate`` loaded
``https://www.youtube.com/`` in 7.4 s and reported the title "YouTube". Nothing was broken except
that no route led to it.

**Why a table and not a model call.** Mapping a spoken service name to a URL is identity work, not
reasoning, and the same vocabulary-rather-than-branches arrangement is already used twice in this
project - :data:`void.browser.playwright_adapter._BROWSERS` for browsers V.O.I.D can drive and
:data:`void.orchestration.messaging.MESSAGING_APPS` for messengers it can reach. Each site is a row:
a canonical name, the spoken forms that mean it, and where it lives. No row has code of its own, and
adding a site is a row.

**Two ways in, and deliberately no third.**

1. a row in :data:`WEBSITES`;
2. something the owner said that is already a hostname ("open reddit.com", "open en.wikipedia.org").

An unknown bare word yields **nothing**. Guessing ``https://<word>.com`` would let a mishearing send
the owner's browser to a domain nobody chose, which is a security question and not a convenience
one - so the model keeps that sentence and this module stays silent.

**Routes, not actions.** This proposes; the resolver chooses and the browser layer acts, through the
existing ``navigate`` tool and therefore through the kill switch and ``RiskGate`` unchanged. A route
requiring ``browser`` is dropped before selection when browser automation is off, which is how the
owner gets an honest refusal instead of a fabricated success.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from void.orchestration.routes import DirectCall, Route, RouteKind, WorldState
from void.perception import clean_text

#: Longest spoken target accepted. A service name, not a sentence.
MAX_TARGET = 60


@dataclass(frozen=True)
class Website:
    """One service V.O.I.D can reach. A row of data, with no behaviour of its own."""

    #: Canonical lower-case name; the key everything else uses.
    name: str
    #: How to refer to it when speaking to the owner.
    display: str
    #: Where it lives.
    url: str
    #: Spoken forms that mean this site.
    aliases: tuple[str, ...] = ()
    #: Where a search goes, when the owner asked about something. ``{q}`` is the quoted terms.
    search: str = ""

    @property
    def vocabulary(self) -> frozenset[str]:
        return frozenset({self.name, *self.aliases})


#: The sites this project can name. Ordered only for stable output; no row is privileged.
WEBSITES: tuple[Website, ...] = (
    Website("youtube", "YouTube", "https://www.youtube.com/", ("you tube",),
            "https://www.youtube.com/results?search_query={q}"),
    Website("gmail", "Gmail", "https://mail.google.com/", ("google mail", "g mail")),
    Website("wikipedia", "Wikipedia", "https://www.wikipedia.org/", ("wiki",),
            "https://en.wikipedia.org/wiki/Special:Search?search={q}"),
    Website("pinterest", "Pinterest", "https://www.pinterest.com/", (),
            "https://www.pinterest.com/search/pins/?q={q}"),
    Website("github", "GitHub", "https://github.com/", ("git hub",),
            "https://github.com/search?q={q}"),
    Website("google", "Google", "https://www.google.com/", (),
            "https://www.google.com/search?q={q}"),
    Website("google drive", "Google Drive", "https://drive.google.com/", ("drive", "gdrive")),
    Website("google maps", "Google Maps", "https://www.google.com/maps", ("maps",),
            "https://www.google.com/maps/search/{q}"),
    Website("google calendar", "Google Calendar", "https://calendar.google.com/", ("calendar",)),
    Website("whatsapp web", "WhatsApp Web", "https://web.whatsapp.com/", ("whatsapp web",)),
    Website("chatgpt", "ChatGPT", "https://chatgpt.com/", ("chat gpt",)),
    Website("stack overflow", "Stack Overflow", "https://stackoverflow.com/", ("stackoverflow",),
            "https://stackoverflow.com/search?q={q}"),
    Website("reddit", "Reddit", "https://www.reddit.com/", (),
            "https://www.reddit.com/search/?q={q}"),
    Website("linkedin", "LinkedIn", "https://www.linkedin.com/", ("linked in",)),
    Website("amazon", "Amazon", "https://www.amazon.in/", (),
            "https://www.amazon.in/s?k={q}"),
)

#: Verbs that mean "put this in front of me". Matched at the start of the utterance only.
_OPEN = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)[ ,]+)?(?:void[ ,]+)?(?:please[ ,]+)?"
    r"(?:open|launch|start|go to|goto|navigate to|take me to|show me|show|bring up|pull up|visit)"
    r"(?: up)?\s+(?P<rest>.+?)\s*$", re.IGNORECASE)

#: "about X" / "for X" / "search X" - the part of the utterance that is a query rather than a site.
_QUERY = re.compile(r"\b(?:about|for|searching for|search for|search)\s+(?P<q>.+?)\s*$",
                    re.IGNORECASE)

#: "in Edge" / "with Opera GX" / "using Chrome" at the END of the utterance. Which browser to use is
#: a separate decision, already made by :mod:`void.orchestration.overrides` for the ``browser`` role,
#: so the phrase has to come off before the site name is matched - otherwise "open YouTube in Opera
#: GX" looks like a request for a site called "youtube in opera gx" and resolves to nothing.
_IN_BROWSER = re.compile(r"\s+(?:in|with|using|via|through|on)\s+(?P<browser>[\w .+-]{2,30})$",
                         re.IGNORECASE)


#: Phrases that mean "the browser you would normally use" rather than a specific one.
_ANY_BROWSER = frozenset({
    "this browser", "the browser", "my browser", "the current browser", "current browser",
    "this window", "the same browser", "my current browser", "the open browser",
})


def _drop_browser_phrase(text: str) -> str:
    """Remove a trailing browser choice, but only when it really names a browser.

    Checked against the browser layer's own table rather than a word list here, so "open YouTube in
    Opera GX" loses the phrase while "open the invoice in Documents" keeps it and resolves to
    nothing - the same refusal :mod:`void.orchestration.overrides` makes for the same reason.
    """
    match = _IN_BROWSER.search(text)
    if match is None:
        return text
    # "in this browser" / "in the current browser" names no particular browser - it means "wherever
    # you would normally put it", so the phrase comes off and the stored preference decides, exactly
    # as it would have with no phrase at all.
    said = " ".join(match.group("browser").lower().split()).strip(" .,!?")
    if said in _ANY_BROWSER:
        return text[: match.start()].strip()
    try:
        from void.orchestration.overrides import _canonical, browser_vocabulary
        if _canonical(match.group("browser"), browser_vocabulary()) is None:
            return text
    except Exception:                                          # noqa: BLE001 - keep the text as-is
        return text
    return text[: match.start()].strip()


#: A hostname the owner actually said. Deliberately strict: no scheme, no path, no credentials, no
#: port - so this recognises "reddit.com" and not an arbitrary string.
_HOSTNAME = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?P<tld>[a-z]{2,24})$",
                       re.IGNORECASE)

#: Final labels that make a dotted word a real address rather than a filename.
#:
#: An ALLOWLIST, and the shape of the rule matters more than its contents. The first version accepted
#: any 2-24 letter final label, which made "open notepad.exe" look like a hostname and would have
#: sent the browser to ``https://notepad.exe`` - a filename turned into a destination by a parser.
#: A denylist of file extensions cannot be used instead, because the project's own
#: :data:`void.core.fast_path._EXTENSIONS` contains "com": it is both a DOS executable suffix and
#: the commonest TLD there is, so denying it would reject "reddit.com".
#:
#: Erring towards rejection is free here. A site that is not on this list is simply not claimed, the
#: model keeps the sentence, and nothing is lost; a wrong acceptance navigates somewhere nobody
#: chose, which is a security question.
_TLDS = frozenset({
    "com", "org", "net", "edu", "gov", "mil", "int", "info", "biz", "name", "pro",
    "io", "dev", "app", "ai", "co", "me", "tv", "fm", "gg", "xyz", "online", "site", "tech",
    "cloud", "page", "blog", "shop", "store", "news", "wiki", "social", "chat", "email",
    "in", "uk", "us", "ca", "au", "de", "fr", "nl", "es", "it", "se", "no", "fi", "dk",
    "pl", "ru", "br", "jp", "cn", "kr", "sg", "nz", "ie", "ch", "at", "be", "pt", "cz",
})

#: Determiners and trailing nouns stripped before matching, so "open the YouTube website" works.
_DETERMINERS = ("the ", "my ", "our ", "this ", "a ", "an ")
_TRAILING = (" website", " site", " web site", " page", " homepage", " home page", " web page",
             " dot com", " app")


def site_by_name(name: object) -> Website | None:
    """The row a spoken name refers to, or None. Longest match first."""
    probe = " ".join(clean_text(name, MAX_TARGET).lower().split())
    if not probe:
        return None
    for site in sorted(WEBSITES, key=lambda s: -len(s.name)):
        if probe == site.name or probe in site.aliases:
            return site
    return None


def website_vocabulary() -> frozenset[str]:
    """Every spoken form that names a site in the table."""
    names: set[str] = set()
    for site in WEBSITES:
        names |= site.vocabulary
    return frozenset(names)


@dataclass(frozen=True)
class WebTarget:
    """A resolved destination: which site, which URL, and what the owner asked about."""

    site: Website | None = None
    url: str = ""
    display: str = ""
    query: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.url)


def _strip(text: str) -> str:
    """Drop a leading determiner and a trailing "website"/"page" noun."""
    body = " ".join(text.lower().split())
    for determiner in _DETERMINERS:
        if body.startswith(determiner):
            body = body[len(determiner):]
            break
    for trailing in sorted(_TRAILING, key=len, reverse=True):
        if body.endswith(trailing):
            body = body[: -len(trailing)].strip()
            break
    return body.strip(" .,!?;:")


def parse_target(goal: object) -> WebTarget:
    """What website, if any, an utterance asks for. Deterministic; no model involved.

    Returns an unresolved target for everything that is not an "open <site>" request, which is the
    common case and the case in which routing behaves exactly as it did before this module existed.
    """
    text = clean_text(goal, 300)
    if not text:
        return WebTarget()
    match = _OPEN.match(text)
    if match is None:
        return WebTarget()
    rest = match.group("rest").strip()

    # "open wikipedia about AI" - the site, then what to look up in it.
    query = ""
    inner = _QUERY.search(rest)
    if inner is not None:
        query = clean_text(inner.group("q"), 120)
        rest = rest[: inner.start()].strip()

    body = _strip(_drop_browser_phrase(rest))
    if not body:
        return WebTarget()

    site = site_by_name(body)
    if site is not None:
        if query and site.search:
            from urllib.parse import quote_plus
            return WebTarget(site=site, url=site.search.format(q=quote_plus(query)),
                             display=site.display, query=query)
        return WebTarget(site=site, url=site.url, display=site.display, query=query)

    # A hostname the owner said themselves. Never a guess: the text has to already look like one.
    host = body.replace(" dot ", ".").replace(" ", "")
    match = _HOSTNAME.match(host)
    if match is not None and match.group("tld").lower() in _TLDS:
        return WebTarget(url="https://" + host.lower(), display=host.lower(), query=query)
    return WebTarget()


class WebsiteRoutes:
    """Websites, as routes the existing resolver can compare against everything else.

    A provider, not a second routing engine. A page already open in a browser tab is proposed by
    :class:`void.orchestration.routes.ExistingTabRoutes`, which scores as ``EXISTING_STATE`` and
    therefore wins against the ``BROWSER`` route proposed here - so reuse continues to beat opening
    something new, without this module knowing anything about tabs.
    """

    name = "websites"

    #: Tool the browser layer registers to load a URL. Named, not imported.
    NAVIGATE_TOOL = "navigate"

    def target(self, goal: str) -> WebTarget:
        return parse_target(goal)

    def propose(self, goal: str, state: WorldState):
        found = parse_target(goal)
        if not found.resolved:
            return ()
        said = found.query or found.display
        return (Route(
            kind=RouteKind.BROWSER,
            describes=f"open {said} in the browser",
            calls=(DirectCall(name=self.NAVIGATE_TOOL,
                              # The URL comes from the table or from a hostname the owner spoke, and
                              # is never assembled from a guess. See the module docstring.
                              arguments={"url": found.url},
                              reply=f"Opened {found.display}."),),
            requires=frozenset({"browser"}),
            # Structured navigation through the browser layer is reliable when it is available at
            # all; the cost is the page load, measured at 7.4 s for a cold controlled browser here.
            reliability=0.9,
            latency_s=7.0,
            why=(f"{found.display} is a website, so it opens in the browser"
                 if not found.query else
                 f"searching {found.display} for what you asked about"),
        ),)
