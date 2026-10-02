"""V.O.I.D's browser abstraction. Playwright lives behind it, nowhere else.

The architecture the blueprint asks for:

    V.O.I.D Browser  (this module - concepts, types, policy)
          ↓
    Browser adapter  (void/browser/playwright_adapter.py - the ONLY file that imports playwright)
          ↓
    the owner's actual browser

Three decisions shape this, each with a concrete reason.

**Attach before launch.** The headline V3 behaviour is "Open Gmail" reusing the tab the owner already has
open in the browser they prefer. That is only possible by attaching to a *running* browser over the Chrome
DevTools Protocol - a freshly launched Playwright browser has none of the owner's sessions, cookies or
tabs, so it would have to log in again and would answer a different question. So the adapter attaches when
it can and launches only as a fallback.

**Drive the owner's installed browser, not a downloaded one.** Edge, Chrome and Opera GX are all present on
this machine. Playwright can drive any of them by channel or executable path, so V.O.I.D does not download
its own ~130 MB Chromium, and the owner's preferred browser is honoured rather than substituted.

**Structured locators, never coordinates.** Elements are found by role, accessible name and text - the same
information a screen reader uses. `aria_snapshot()` gives a page's structure as text. Clicking at an (x, y)
is brittle, unverifiable and the first thing to break when a page changes; it is not implemented here at
all.

Security properties, all enforced in this module so no adapter can forget them:

* **URL schemes are allowlisted** to http/https. `file://` would turn a model-supplied string into local
  file reading, `javascript:` into code execution, `data:` into a smuggled document. See :func:`safe_url`.
* **Page content is untrusted data.** Titles, text and element labels are written by the page. They are
  cleaned at the boundary (`void.perception.clean_text`) and never become instructions or authorization.
* **Consequential controls are recognised deterministically.** A button whose accessible name means send,
  submit, pay, delete or publish raises the risk of clicking it, so the owner is asked. That decision is
  made from the element's own name by fixed vocabulary - never by asking a model whether a click is safe.
* **Uploads are confined.** A file sent to a page must come from a path the owner's file policy already
  allows; otherwise a page could ask V.O.I.D to upload anything on the disk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse, urlunparse

from void.perception import Element, Observation, clean_text
from void.security.consequential import CONSEQUENTIAL_WORDS as _CONSEQUENTIAL_WORDS
from void.security.consequential import is_consequential

#: The only URL schemes V.O.I.D will navigate to. Everything else is refused before it reaches a browser.
#:
#: This is a short list on purpose. ``file://`` would let a model-supplied string read local files through
#: the browser, sidestepping the filesystem confinement entirely. ``javascript:`` is code execution.
#: ``data:`` can carry a whole document, including one crafted to be read back as an instruction. ``about:``
#: and ``chrome://`` reach browser internals and settings.
#: How much visible page text is carried. Enough for a research excerpt, bounded so a huge page
#: cannot blow up memory or a log line.
MAX_PAGE_TEXT = 20_000

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hosts that are never navigated to, because they are not web pages: the browser's own internals.
_BLOCKED_HOSTS = frozenset({"localhost.localdomain"})

# The consequential-control vocabulary lives in void/security/consequential.py, shared with the desktop
# layer: a button labelled "Send" means the same thing in Gmail and in WhatsApp, and a security vocabulary
# kept in two copies will eventually be two different copies. Re-exported here for callers that already
# import it from the browser layer.
CONSEQUENTIAL_WORDS = _CONSEQUENTIAL_WORDS

#: Any scheme-shaped prefix: "name:" at the start. Used to tell "example.com/path" (a bare host) from
#: "javascript:alert(1)" (a scheme V.O.I.D must refuse rather than rewrite).
_SCHEME_PREFIX = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


class BrowserUnavailable(RuntimeError):
    """No browser could be reached or driven. The message is safe to say to the owner."""


class UnsafeUrl(ValueError):
    """A URL was refused before any browser saw it."""


def safe_url(raw: object) -> str:
    """A URL V.O.I.D is willing to navigate to, or raise :class:`UnsafeUrl`.

    Normalises a bare host ("example.com") to https. Refuses anything whose scheme is not http/https,
    anything with credentials embedded (``user:pass@host`` is a phishing shape and a credential leak), and
    anything with control characters or newlines (header/argument smuggling).

    Called by the adapter before every navigation, so there is one place this is decided.
    """
    if not isinstance(raw, str):
        raise UnsafeUrl("That is not a web address.")
    text = clean_text(raw, 2000)
    if not text or any(character in text for character in "\r\n\t"):
        raise UnsafeUrl("That is not a usable web address.")
    # A scheme must be recognised BEFORE deciding this is a bare host. Testing for "://" is not enough:
    # javascript:, data:, about: and mailto: carry a single colon and no slashes, so they fell into the
    # bare-host branch and had "https://" prepended - silently rewriting "javascript:alert(1)" into an
    # https URL rather than refusing it. Any scheme-shaped prefix is now checked against the allowlist.
    scheme_match = _SCHEME_PREFIX.match(text)
    if scheme_match:
        found = scheme_match.group(1).lower()
        if found not in ALLOWED_SCHEMES:
            raise UnsafeUrl(f"I will only open http and https addresses, and that one is '{found}'.")
    elif text.startswith("//"):
        text = "https:" + text
    else:
        # A bare host or host/path. Default to https rather than http: downgrading silently would be worse.
        text = "https://" + text.lstrip("/")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrl(
            f"I will only open http and https addresses, and that one is '{scheme or 'unknown'}'.")
    if parsed.username or parsed.password or "@" in (parsed.netloc or "").split("]")[-1]:
        raise UnsafeUrl("I will not open an address with a username or password embedded in it.")
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        raise UnsafeUrl("That address has no usable host.")
    return urlunparse((scheme, parsed.netloc, parsed.path or "/", parsed.params,
                       parsed.query, parsed.fragment))


@dataclass(frozen=True)
class TabInfo:
    """One open tab, as V.O.I.D sees it.

    ``handle`` is the adapter's reference to this exact tab. A caller selects a tab by handle; it never
    constructs one. ``title`` and ``url`` are UNTRUSTED - the page chose them.
    """

    url: str
    title: str = ""
    handle: str = ""
    browser: str = ""
    active: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", clean_text(self.title, 200))
        object.__setattr__(self, "browser", clean_text(self.browser, 60))

    def matches(self, needle: str) -> bool:
        """Does this tab look like what the owner asked for? Substring over url and title."""
        probe = clean_text(needle, 200).lower()
        if not probe:
            return False
        return probe in self.url.lower() or probe in self.title.lower()

    def as_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "handle": self.handle,
                "browser": self.browser, "active": self.active}


@dataclass(frozen=True)
class PageState:
    """What a page looks like right now: where it is, what it says, what can be acted on."""

    url: str
    title: str = ""
    #: A structured outline of the page (Playwright's aria snapshot). UNTRUSTED, bounded.
    outline: str = ""
    elements: tuple[Element, ...] = ()
    #: The page's visible text. UNTRUSTED and bounded. Carried separately from ``outline`` because reading
    #: a page for its CONTENT (research) and reading it for its CONTROLS (acting) are different jobs, and
    #: an aria snapshot is good at the second and poor at the first.
    text: str = ""
    #: Outbound links as (url, label) pairs, already absolute. Provided structurally so that nothing
    #: downstream has to parse HTML to find out where a page can lead - the research engine in particular
    #: never sees markup. Still UNTRUSTED: a page chooses its own link text.
    links: tuple[tuple[str, str], ...] = ()
    #: True when the page finished loading rather than being captured mid-navigation.
    settled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", clean_text(self.title, 200))
        object.__setattr__(self, "outline", clean_text(self.outline, 4000))
        object.__setattr__(self, "text", clean_text(self.text, MAX_PAGE_TEXT))

    def as_dict(self) -> dict:
        """The model-facing form. ``links`` is omitted deliberately: a model that needs to go somewhere
        navigates by URL or clicks an offered element, so handing it every link on the page would be a lot
        of untrusted text for no capability."""
        return {"url": self.url, "title": self.title, "outline": self.outline,
                "elements": [element.as_dict() for element in self.elements],
                "text": self.text[:2000], "link_count": len(self.links),
                "settled": self.settled}


@dataclass(frozen=True)
class BrowserPolicy:
    """What the owner's configuration permits for browser automation."""

    enabled: bool = False
    #: Preferred browser, by name. Resolved to a channel or an executable by the adapter.
    preferred: str = ""
    #: Attach to a running browser over CDP on this port when one is listening. 0 disables attaching,
    #: which means losing session reuse - the thing the browser layer mostly exists for.
    cdp_port: int = 9222
    #: Launch a browser when none can be attached to. False means V.O.I.D only ever drives a browser the
    #: owner already started, which is the most conservative setting.
    allow_launch: bool = True
    #: Run a launched browser without a visible window. False by default: a browser doing things on the
    #: owner's behalf should be visible to them.
    headless: bool = False
    #: Seconds for any single browser operation.
    timeout_s: float = 20.0

    @classmethod
    def from_config(cls, config) -> "BrowserPolicy":
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default

        def number(key, default, low, high):
            try:
                return min(max(type(default)(get(key, default)), low), high)
            except (TypeError, ValueError):
                return default
        preferred = get("preferences.browser", "") or get("browser.preferred", "")
        return cls(enabled=bool(get("browser.enabled", False)),
                   preferred=clean_text(preferred, 60),
                   cdp_port=int(number("browser.cdp_port", 9222, 0, 65535)),
                   allow_launch=bool(get("browser.allow_launch", True)),
                   headless=bool(get("browser.headless", False)),
                   timeout_s=float(number("browser.timeout_s", 20.0, 1.0, 120.0)))


class BrowserAdapter(Protocol):
    """What V.O.I.D needs from a browser. Playwright is one implementation; nothing depends on it.

    Every method raises :class:`BrowserUnavailable` when the browser cannot serve the request, so callers
    have one failure type to handle. Implementations are responsible for applying :func:`safe_url` before
    navigating and for cleaning page-supplied text on the way out.
    """

    name: str

    def available(self) -> bool: ...
    def tabs(self) -> tuple[TabInfo, ...]: ...
    def activate(self, handle: str) -> TabInfo: ...
    def navigate(self, url: str, *, handle: str | None = None) -> PageState: ...
    def read(self, handle: str | None = None) -> PageState: ...
    def click(self, handle: str, element_handle: str) -> PageState: ...
    def fill(self, handle: str, element_handle: str, text: str) -> PageState: ...
    def close(self) -> None: ...


def describe_tabs(tabs) -> str:
    """A short, speakable summary of open tabs."""
    tabs = tuple(tabs)
    if not tabs:
        return "No browser tabs are open."
    shown = ", ".join(f"{tab.title or tab.url}" for tab in tabs[:4])
    more = f" and {len(tabs) - 4} more" if len(tabs) > 4 else ""
    return f"{len(tabs)} tab(s) open: {shown}{more}."
