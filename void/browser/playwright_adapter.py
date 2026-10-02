"""The Playwright adapter: the only module in V.O.I.D that imports playwright.

Everything above this file speaks the V.O.I.D browser vocabulary from :mod:`void.browser`. If Playwright
were replaced, this file would be rewritten and nothing else would change - which is the point.

Three implementation facts, all measured on this machine rather than assumed:

**Attach over CDP first (2.8 s), launch second (4.8 s).** `connect_over_cdp` attaches to a browser the
owner already has running, and only that gives V.O.I.D their real tabs, cookies and logged-in sessions.
Validated: attach → enumerate real tabs → find by url → `bring_to_front()` → `aria_snapshot()`.

**Drive the installed browser; download nothing.** Playwright's own Chromium is absent here
(`chromium-1243` does not exist on disk) and is not needed: Edge, Chrome and Opera GX are all installed,
and Playwright drives them by `channel=` or `executable_path=`. So no 130 MB download, and the owner's
preferred browser is the one used.

**Thread affinity is real.** Playwright's sync API is greenlet-based and its objects must be used from the
thread that created them. V.O.I.D runs the voice chain on a serial worker and the CLI on the main thread,
so a browser session created on one and used from the other would fail in a confusing way. :class:`_Driver`
therefore owns a single dedicated thread and marshals every call onto it - the same shape as
``void.voice.runtime._SerialVoiceWorker``, for the same reason.
"""
from __future__ import annotations

import hashlib
import re

import logging
import queue
import socket
import threading
from dataclasses import dataclass

from void.browser import (MAX_PAGE_TEXT, BrowserPolicy, BrowserUnavailable, PageState, TabInfo, UnsafeUrl,
                          is_consequential, safe_url)
from void.perception import Element, clean_text

_log = logging.getLogger(__name__)

#: Roles worth offering as actionable. A page has thousands of nodes; an agent needs the ones it can act
#: on, and a model cannot usefully choose from a thousand.
#: An engine-minted element index: ASCII digits only. See _locate for why this is not "\d".
_ASCII_INDEX = re.compile("[0-9]{1,6}")

#: The name-fingerprint suffix of an element handle: 6 lowercase hex characters.
_FINGERPRINT = re.compile("[0-9a-f]{6}")


#: How many offer records to keep. One page's worth per tab, several tabs over: bounded so a long session
#: cannot grow the table without limit.
_MAX_OFFERS = 1200


def handle_of_page(page) -> str:
    """A stable key for the page an offer belongs to, so a handle from one tab cannot resolve in another."""
    try:
        return str(getattr(page, "url", "") or "")[:300]
    except Exception:                                          # noqa: BLE001
        return ""


def element_handle_key(page_key: str, element_handle: str) -> str:
    return f"{page_key}|{element_handle}"


def handle_of_page_for(adapter, handle: str) -> str:
    """The page key for a tab handle, without touching the browser thread.

    Risk grading runs on the caller's thread and must not block on Playwright, so this reads the URL the
    adapter already recorded for that tab rather than asking the live page for it.
    """
    try:
        page = adapter._pages.get(handle)
    except Exception:                                          # noqa: BLE001
        return ""
    return handle_of_page(page) if page is not None else ""


def _fingerprint(name: object) -> str:
    """A short, stable digest of an element's accessible name.

    Not a security token - it is not secret and is not meant to be unforgeable. Its job is to detect that
    the element at a given index is no longer the one V.O.I.D offered, which a positional handle alone
    cannot do. Truncated to 6 hex characters because the comparison is against ONE element at ONE index,
    so collisions are irrelevant; the handle stays short enough to read in a log.
    """
    text = clean_text(name, 200).strip().lower()
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:6]


_INTERESTING_ROLES = ("button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
                      "menuitem", "tab", "option")

#: How many elements of each role are collected. Bounded so a huge page cannot stall the read.
_PER_ROLE = 25

#: Where to look for an installed Chromium-family browser, by preferred-name fragment. Checked in order;
#: the first existing path wins. ``channel`` is Playwright's own supported name where it has one.
_BROWSERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("opera gx", "", (r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe",)),
    ("opera", "", (r"%LOCALAPPDATA%\Programs\Opera\opera.exe",)),
    ("edge", "msedge", ()),
    ("chrome", "chrome", ()),
    ("chromium", "chromium", ()),
)


def _resolve_browser(preferred: str) -> tuple[str, str]:
    """``(channel, executable_path)`` for the owner's preferred browser; one of them is always "".

    Falls back through the table so a machine without the preferred browser still gets a working one rather
    than an error - but the preference is honoured when it can be.
    """
    import os
    want = clean_text(preferred, 60).lower()
    ordered = sorted(_BROWSERS, key=lambda row: 0 if want and row[0] in want else 1)
    for _name, channel, paths in ordered:
        for raw in paths:
            expanded = os.path.expandvars(raw)
            if os.path.exists(expanded):
                return "", expanded
        if channel:
            return channel, ""
    return "chromium", ""


class _Driver:
    """Owns the Playwright objects on one dedicated thread and runs callables there.

    Playwright's sync API must be used from its creating thread. Rather than hoping every caller is on the
    right one, this gives the session a thread of its own and marshals work onto it. Failures are
    re-raised to the caller, so from the outside it behaves like an ordinary method call.
    """

    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="void-browser", daemon=True)
        self._started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            function, reply = job
            try:
                reply.put((True, function()))
            except BaseException as exc:                        # noqa: BLE001 - relayed to the caller
                reply.put((False, exc))

    def call(self, function, timeout: float = 60.0):
        """Run ``function`` on the browser thread and return its result (or raise its exception)."""
        self.start()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((function, reply))
        try:
            ok, value = reply.get(timeout=timeout)
        except queue.Empty as exc:
            raise BrowserUnavailable("The browser did not respond in time.") from exc
        if ok:
            return value
        raise value

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self._jobs.put(None)
                self._started = False


#: The only address this module may probe. A literal, not a parameter: the probe exists solely to notice a
#: browser the owner started on this machine, and a host argument would make the same code a port scanner
#: against anything on the network. V.O.I.D's network model is observational, so the restriction is
#: structural rather than a convention about how to call it.
_LOOPBACK = "127.0.0.1"


def _port_open(port: int, timeout: float = 0.25) -> bool:
    """Is a browser listening on loopback? Decides whether to attach without trying to attach.

    One connect to one port on 127.0.0.1, with a short timeout. There is deliberately no way to point this
    anywhere else.
    """
    try:
        number = int(port)
    except (TypeError, ValueError):
        return False
    if not 0 < number < 65536:
        return False
    try:
        with socket.socket() as probe:
            probe.settimeout(timeout)
            return probe.connect_ex((_LOOPBACK, number)) == 0
    except Exception:                                          # noqa: BLE001
        return False


class PlaywrightBrowser:
    """V.O.I.D's browser, implemented with Playwright.

    Lazily started: constructing this does nothing, so the Assistant can hold one whether or not the owner
    ever uses a browser. The first operation attaches or launches.
    """

    name = "playwright"

    def __init__(self, policy: BrowserPolicy | None = None):
        self._policy = policy or BrowserPolicy()
        self._driver = _Driver()
        self._playwright = None
        self._browser = None
        self._context = None
        self._attached = False
        #: What V.O.I.D last offered, keyed by page URL and element handle: ``{role, name, rank}``.
        #:
        #: This is the offer table, and it carries two properties at once. It makes a handle resolvable by
        #: the element's own identity rather than its position, which is what lets a handle survive a page
        #: that is still loading. And because a handle must be IN this table to resolve at all, a caller
        #: cannot invent one - the table, not the string's shape, is what makes a handle real.
        self._offers: dict[str, dict] = {}
        #: handle -> Playwright Page. Handles are minted here; a caller can never invent one.
        self._pages: dict[str, object] = {}
        self._counter = 0

    @property
    def policy(self) -> BrowserPolicy:
        return self._policy

    @property
    def attached(self) -> bool:
        """True when driving a browser the OWNER started (so their sessions are available)."""
        return self._attached

    def available(self) -> bool:
        """Can a browser be driven at all? Does not start one."""
        if not self._policy.enabled:
            return False
        try:
            import playwright  # noqa: F401
        except Exception:                                      # noqa: BLE001
            return False
        return bool(self._policy.allow_launch or _port_open(self._policy.cdp_port))

    # -- session ------------------------------------------------------------------------------------
    def _ensure(self) -> None:
        """Attach to a running browser, or launch one. Runs on the browser thread."""
        if self._context is not None:
            return
        if not self._policy.enabled:
            raise BrowserUnavailable(
                "Browser automation is switched off in V.O.I.D's configuration.")
        from playwright.sync_api import sync_playwright
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        chromium = self._playwright.chromium
        port = self._policy.cdp_port
        if port and _port_open(port):
            try:
                self._browser = chromium.connect_over_cdp(f"http://127.0.0.1:{int(port)}")
                self._context = (self._browser.contexts[0] if self._browser.contexts
                                 else self._browser.new_context())
                self._attached = True
                _log.info("BROWSER_ATTACHED port=%s contexts=%d", port, len(self._browser.contexts))
                return
            except Exception as exc:                            # noqa: BLE001
                _log.info("BROWSER_ATTACH_FAILED kind=%s", type(exc).__name__)
        if not self._policy.allow_launch:
            raise BrowserUnavailable(
                "No browser is open for V.O.I.D to use, and launching one is switched off.")
        channel, executable = _resolve_browser(self._policy.preferred)
        kwargs: dict = {"headless": bool(self._policy.headless)}
        if executable:
            kwargs["executable_path"] = executable
        elif channel:
            kwargs["channel"] = channel
        try:
            self._browser = chromium.launch(**kwargs)
        except Exception as exc:                                # noqa: BLE001
            raise BrowserUnavailable(
                "I could not start a browser. Install one, or open the browser you want me to use."
            ) from exc
        self._context = self._browser.new_context()
        self._attached = False
        _log.info("BROWSER_LAUNCHED channel=%s executable=%s", channel or "-",
                  "yes" if executable else "no")

    def _handle_for(self, page) -> str:
        for handle, known in self._pages.items():
            if known is page:
                return handle
        self._counter += 1
        handle = f"tab-{self._counter}"
        self._pages[handle] = page
        return handle

    def _page(self, handle: str | None):
        """The page a handle names, or the active one. Raises for an unknown handle."""
        if handle:
            page = self._pages.get(handle)
            if page is None:
                raise BrowserUnavailable("That tab is no longer open. Ask me to list the tabs again.")
            if page.is_closed():
                self._pages.pop(handle, None)
                raise BrowserUnavailable("That tab has been closed.")
            return page
        pages = [page for page in (self._context.pages if self._context else []) if not page.is_closed()]
        if not pages:
            raise BrowserUnavailable("No browser tab is open.")
        return pages[-1]

    # -- the V.O.I.D browser operations --------------------------------------------------------------
    def tabs(self) -> tuple[TabInfo, ...]:
        def work():
            self._ensure()
            found = []
            browser_name = self._policy.preferred or ("attached browser" if self._attached
                                                      else "browser")
            for page in self._context.pages:
                if page.is_closed():
                    continue
                try:
                    url, title = page.url, page.title()
                except Exception:                              # noqa: BLE001 - a tab mid-navigation
                    url, title = getattr(page, "url", ""), ""
                found.append(TabInfo(url=url, title=title, handle=self._handle_for(page),
                                     browser=browser_name))
            return tuple(found)
        return self._driver.call(work, timeout=self._policy.timeout_s + 40)

    def activate(self, handle: str) -> TabInfo:
        """Bring an existing tab to the front. The blueprint's "reuse, don't relaunch"."""
        def work():
            self._ensure()
            page = self._page(handle)
            page.bring_to_front()
            return TabInfo(url=page.url, title=page.title(), handle=handle,
                           browser=self._policy.preferred, active=True)
        return self._driver.call(work, timeout=self._policy.timeout_s + 10)

    def find_tab(self, needle: str) -> TabInfo | None:
        """The first open tab matching ``needle``, or None. Matching happens over observed urls/titles."""
        for tab in self.tabs():
            if tab.matches(needle):
                return tab
        return None

    def navigate(self, url: str, *, handle: str | None = None) -> PageState:
        """Go to a URL, in an existing tab or a new one. The URL is validated before any browser sees it."""
        target = safe_url(url)                                  # raises UnsafeUrl

        def work():
            self._ensure()
            if handle:
                page = self._page(handle)
            else:
                page = self._context.new_page()
                self._handle_for(page)
            page.goto(target, wait_until="domcontentloaded",
                      timeout=int(self._policy.timeout_s * 1000))
            return self._state(page)
        return self._driver.call(work, timeout=self._policy.timeout_s + 40)

    def read(self, handle: str | None = None) -> PageState:
        """What the page says and what can be acted on. Structured only - no screenshot."""
        def work():
            self._ensure()
            return self._state(self._page(handle))
        return self._driver.call(work, timeout=self._policy.timeout_s + 20)

    def _state(self, page) -> PageState:
        """Build a PageState from a Playwright page. Runs on the browser thread."""
        try:
            outline = page.aria_snapshot()
        except Exception:                                      # noqa: BLE001
            outline = ""
        elements: list[Element] = []
        per_name: dict[tuple, int] = {}
        for role in _INTERESTING_ROLES:
            try:
                found = page.get_by_role(role).all()[:_PER_ROLE]
            except Exception:                                  # noqa: BLE001
                continue
            for index, locator in enumerate(found):
                # Via the shared helper, so the fingerprint minted here and the one verified in _locate
                # are computed from the same name by the same fallback order.
                name = self._element_name(locator)
                try:
                    enabled = locator.is_enabled(timeout=300)
                    visible = locator.is_visible(timeout=300)
                except Exception:                              # noqa: BLE001 - detached mid-read
                    continue
                handle = f"{role}#{index}~{_fingerprint(name)}"
                cleaned = clean_text(name, 200).strip()
                # Record what was offered, so acting on it later resolves by identity rather than by
                # position. ``rank`` is this element's place among same-named siblings of the same role.
                rank = per_name.get((role, cleaned), 0)
                per_name[(role, cleaned)] = rank + 1
                self._remember_offer(page, handle, role, cleaned, rank)
                elements.append(Element(role=role, name=name, handle=handle,
                                        enabled=enabled, visible=visible))
        try:
            url, title = page.url, page.title()
        except Exception:                                      # noqa: BLE001
            url, title = getattr(page, "url", ""), ""
        return PageState(url=url, title=title, outline=outline, elements=tuple(elements),
                         text=self._page_text(page), links=self._page_links(page))

    def _page_text(self, page) -> str:
        """The page's visible text, for reading content rather than finding controls.

        ``body.inner_text()`` is used rather than ``content()`` because it returns rendered, visible text -
        no markup, no script bodies, no hidden elements - which is both what research wants and far less
        untrusted material to carry around.
        """
        try:
            return page.locator("body").inner_text(timeout=2000)[:MAX_PAGE_TEXT]
        except Exception:                                      # noqa: BLE001
            return ""

    def _page_links(self, page) -> tuple[tuple[str, str], ...]:
        """Outbound links as absolute (url, label) pairs, extracted in the page rather than from markup.

        Done with one ``evaluate`` call instead of per-link locator reads because a results page has
        hundreds of links and a round trip each would take seconds. ``a.href`` is already absolute, and
        resolution is the browser's own, so there is no URL joining to get wrong here.

        The script only reads; it is V.O.I.D's own literal, not anything a model or a page supplied.
        """
        try:
            raw = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".slice(0, 300).map(a => [a.href, (a.innerText || '').slice(0, 120)])")
        except Exception:                                      # noqa: BLE001
            return ()
        pairs: list[tuple[str, str]] = []
        for entry in raw or ():
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            href, label = entry
            if isinstance(href, str) and href:
                pairs.append((href[:2000], clean_text(label, 120)))
        return tuple(pairs)

    def _locate(self, page, element_handle: str):
        """Resolve an engine-minted element handle back to a locator, or refuse.

        The handle format is ``role#index~fingerprint``, produced by :meth:`_state`. Parsed strictly: a
        handle that is not of that shape, or whose role is not one V.O.I.D collects, is refused. So a model
        cannot pass a CSS selector or an XPath here and have it evaluated - it can only choose something
        V.O.I.D already found and offered.

        **The fingerprint is the part that matters, and it was added because of a measured defect.** On a
        real Wikipedia edit page, ``button#5`` was "Publish changes" on one read and "Find and replace" on
        the next, because the edit toolbar loads asynchronously and inserting a button shifts every later
        index. A purely positional handle therefore let the risk be graded against one control and the
        click land on another - precisely the time-of-check-to-time-of-use gap the design exists to close.

        So the handle carries a fingerprint of the accessible name it had when it was offered, and this
        method re-checks it against the live element. A page that has shifted underneath V.O.I.D fails
        CLOSED: the call is refused and the caller has to look again. The same change fixes risk grading
        for free, because a handle whose element has changed no longer appears in a fresh read at all, and
        ``_click_risk`` already treats "not found" as HIGH.
        """
        text = clean_text(element_handle, 80)
        if "#" not in text:
            raise BrowserUnavailable("That is not an element I offered.")
        role, _, rest = text.partition("#")
        index_text, _, fingerprint = rest.partition("~")
        # ``str.isdigit()`` is Unicode-aware, so "role#١" (Arabic-Indic digits) would pass it and then
        # convert cleanly through int(). The handles V.O.I.D mints are ASCII; anything else is not one.
        if role not in _INTERESTING_ROLES or not _ASCII_INDEX.fullmatch(index_text):
            raise BrowserUnavailable("That is not an element I offered.")
        if not _FINGERPRINT.fullmatch(fingerprint or ""):
            raise BrowserUnavailable("That is not an element I offered.")
        offer = self._offers.get(element_handle_key(handle_of_page(page), text))
        if offer is None:
            raise BrowserUnavailable("That is not an element I offered.")

        name = offer["name"]
        if name:
            # Resolve by the element's own accessible name, not by where it sat in the list. This is what
            # makes a handle survive a page that is still loading: an inserted toolbar button shifts every
            # later index, but it does not rename the button V.O.I.D was asked about.
            try:
                matches = page.get_by_role(role, name=name, exact=True)
                count = matches.count()
            except Exception:                                  # noqa: BLE001
                count = 0
            if count == 1:
                return matches.first
            if count > 1:
                # Several controls share a name ("Edit" on every row). The offer records which one it was
                # among its namesakes, which is stable in a way a global index is not.
                rank = int(offer.get("rank", 0))
                if rank < count:
                    return matches.nth(rank)
                raise BrowserUnavailable("That element is no longer on the page.")
            raise BrowserUnavailable(
                "That element is no longer on the page - let me read it again before acting.")

        # Unnamed element (an icon button, a bare input). There is no identity to match on, so fall back to
        # position AND verify the fingerprint, which at least detects that the page shifted underneath us.
        found = page.get_by_role(role).all()
        index = int(index_text)
        if index >= len(found):
            raise BrowserUnavailable("That element is no longer on the page.")
        locator = found[index]
        if _fingerprint(self._element_name(locator)) != fingerprint:
            raise BrowserUnavailable(
                "The page has changed since I looked at it - let me read it again before acting.")
        return locator

    def offered_name(self, handle: str, element_handle: str) -> str | None:
        """The accessible name of an element V.O.I.D offered, or None if it never offered that handle.

        Used for grading risk. Sound despite not re-reading the page, because :meth:`_locate` will only
        ever act on an element whose LIVE accessible name equals this one: if the page has relabelled the
        control, the click is refused rather than landing on something graded under the old name. So the
        name used to decide, and the name of the thing acted on, cannot diverge.

        The alternative - searching a freshly read element list - graded "Show preview" and "Cancel" as
        consequential on a real page, purely because a still-loading toolbar had shifted their positions
        out of the handle they were offered under. Confirming harmless clicks is not free: it teaches the
        owner to approve without reading, which is worse than asking less often and meaning it.
        """
        key = element_handle_key(handle_of_page_for(self, handle), element_handle)
        offer = self._offers.get(key)
        if offer is None:
            return None
        return offer.get("name") or ""

    def _remember_offer(self, page, handle: str, role: str, name: str, rank: int) -> None:
        """Record an offered element so it can later be resolved by identity.

        Bounded: once the table is full the oldest half is dropped. Losing an old offer is harmless - the
        handle simply stops resolving, and the caller reads the page again, which is the same thing that
        happens when a page navigates away.
        """
        if len(self._offers) >= _MAX_OFFERS:
            for stale in list(self._offers)[: _MAX_OFFERS // 2]:
                self._offers.pop(stale, None)
        self._offers[element_handle_key(handle_of_page(page), handle)] = {
            "role": role, "name": name, "rank": rank}

    def _element_name(self, locator) -> str:
        """An element's accessible name, by the same route :meth:`_state` used to offer it.

        Shared so the fingerprint is computed identically in both places; a different fallback order here
        would make every handle look stale.
        """
        try:
            name = locator.inner_text(timeout=300) or ""
            if not name:
                name = (locator.get_attribute("aria-label", timeout=300)
                        or locator.get_attribute("placeholder", timeout=300) or "")
            return name
        except Exception:                                      # noqa: BLE001 - detached mid-read
            return ""

    def click(self, handle: str, element_handle: str) -> PageState:
        def work():
            self._ensure()
            page = self._page(handle)
            self._locate(page, element_handle).click(timeout=int(self._policy.timeout_s * 1000))
            page.wait_for_load_state("domcontentloaded",
                                     timeout=int(self._policy.timeout_s * 1000))
            return self._state(page)
        return self._driver.call(work, timeout=self._policy.timeout_s + 40)

    def fill(self, handle: str, element_handle: str, text: str) -> PageState:
        value = clean_text(text, 2000)

        def work():
            self._ensure()
            page = self._page(handle)
            self._locate(page, element_handle).fill(value,
                                                    timeout=int(self._policy.timeout_s * 1000))
            return self._state(page)
        return self._driver.call(work, timeout=self._policy.timeout_s + 20)

    def close(self) -> None:
        """Release the browser. An ATTACHED browser is detached, never closed - it is the owner's."""
        def work():
            try:
                if self._browser is not None and not self._attached:
                    self._browser.close()
            finally:
                if self._playwright is not None:
                    self._playwright.stop()
                self._playwright = self._browser = self._context = None
                self._pages.clear()
                self._attached = False
            return None
        try:
            self._driver.call(work, timeout=20)
        except Exception:                                      # noqa: BLE001 - shutdown must not raise
            pass
        self._driver.stop()
