"""Where the owner actually is, read fresh, compared, and then forgotten.

This is the only place in the focus layer that ever holds an identity - the name of the frontmost
application, or the host of the browser's front tab. Everything above it works in booleans, counters
and one opaque target handle, which is what makes the privacy property in ``void/focus/__init__.py``
structural rather than a promise about discipline.

Three rules the Astra pack earned the hard way and that are reproduced here deliberately:

**Read fresh, every tick.** The pack's own note is that a cached notification API freezes inside a
long-lived process and reports the first application forever. :meth:`SurfaceReader.read` therefore
asks the window layer every time and caches nothing.

**Only where the owner is may become the target.** Never a background window, never a second monitor,
never the best guess from a window list. :class:`Surface` describes the foreground and nothing else,
and when the foreground cannot be read it says ``unreadable`` instead of returning something
plausible - a wrong lock is worse than no lock.

**Home base is V.O.I.D itself.** Coming back to the assistant to talk to it is not a drift. V.O.I.D's
own windows are recognised by process, not by title, because a title is whatever a program chose to
display.

The tab lane is best effort and says so. Reading *which* tab is in front needs the browser layer
(deny-by-default, `browser.enabled`) and a browser that will answer; when it will not, the session
runs app-only, which is the pack's own fallback rather than a silent degradation.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

#: Process names that ARE V.O.I.D. A session started while looking at the orb must not lock the orb.
#: Matched on the process name, never the window title.
HOME_PROCESSES = frozenset({"python", "python3", "pythonw", "void", "void.exe", "pythonw.exe",
                            "python.exe", "python3.exe"})

#: Process names that are browsers, and so have a tab lane worth locking. A browser not in this list
#: is locked at application level, which is the honest granularity for it.
BROWSER_PROCESSES = frozenset({
    "chrome", "chrome.exe", "msedge", "msedge.exe", "brave", "brave.exe", "opera", "opera.exe",
    "opera_gx", "opera_gx.exe", "operagx", "operagx.exe", "vivaldi", "vivaldi.exe",
    "chromium", "chromium.exe", "firefox", "firefox.exe", "arc", "arc.exe",
})

#: Lanes a session can be locked in.
APP = "app"
TAB = "tab"

#: Why a surface could not be read, for the diagnostic. Statuses, never identities.
READ_OK = "ok"
READ_NO_WINDOW = "no_foreground_window"
READ_FAILED = "read_failed"
READ_NO_BROWSER_LAYER = "browser_layer_off"
READ_NO_TAB = "tab_not_readable"

_PROC_SUFFIX = re.compile(r"\.exe\Z", re.IGNORECASE)


def _handle(lane: str, identity: str) -> str:
    """An opaque handle for one surface, used only to compare two reads of it.

    A digest rather than the name, so that the one value the engine does keep is not a readable
    record of what the owner had open. Truncated because it is a comparison key and not a security
    boundary - the thing it protects against is the engine, the ledger or a log line accidentally
    carrying "instagram.com".
    """
    digest = hashlib.sha256(f"{lane}:{identity}".encode("utf-8")).hexdigest()
    return f"{lane}-{digest[:16]}"


@dataclass(frozen=True)
class Surface:
    """One foreground reading. Identity-bearing, and never stored above the reader.

    ``app_handle`` and ``tab_handle`` are what the engine compares. ``app_label`` and ``host`` exist
    for exactly one purpose - building the spoken line in the tick that speaks it - and the engine's
    contract is to use them and drop them.
    """

    status: str
    app_handle: str = ""
    tab_handle: str = ""
    #: What a person would call the application. Empty when it could not be read.
    app_label: str = ""
    #: Host of the browser's front tab. Empty when there is no tab lane for this surface.
    host: str = ""
    is_home: bool = False
    is_browser: bool = False
    #: Why the tab lane is empty, when it is.
    tab_status: str = ""

    @property
    def readable(self) -> bool:
        return self.status == READ_OK and bool(self.app_handle)


#: A reading that failed, with nothing invented to fill it in.
def unreadable(status: str = READ_FAILED) -> Surface:
    return Surface(status=status)


class SurfaceReader:
    """Reads the foreground surface through the capability layers that already exist.

    ``active_window`` and ``tabs`` are callables so that a focus session costs nothing until it runs,
    and so that the engine can be tested against a scripted reader with no desktop at all.
    """

    def __init__(self, *, active_window=None, front_tab=None,
                 home_processes=HOME_PROCESSES, browser_processes=BROWSER_PROCESSES):
        self._active_window = active_window
        self._front_tab = front_tab
        self._home = frozenset(home_processes)
        self._browsers = frozenset(browser_processes)
        #: Counts only, for the diagnostic: how many reads were asked for and how many answered.
        self.reads = 0
        self.failures = 0

    @classmethod
    def from_assistant(cls, assistant) -> "SurfaceReader":
        """Build a reader over an Assistant's existing capability layers.

        The window read goes through the registered ``get_active_window`` tool, so it passes the same
        funnel and the same protected-process rules as any other use of it. The tab read goes
        through the browser layer only if the owner has enabled it.
        """
        def active_window():
            out = assistant.tools.execute("get_active_window", {})
            return out.data if out.ok else None

        def front_tab():
            browser = getattr(assistant, "browser", None)
            if browser is None:
                return None, READ_NO_BROWSER_LAYER
            try:
                tabs = browser.tabs()
            except Exception as exc:                  # the browser is not running, or refused
                _log.info("FOCUS_TAB_READ_FAILED reason=%s", type(exc).__name__)
                return None, READ_NO_TAB
            front = next((t for t in tabs if getattr(t, "active", False)), None)
            if front is None and len(tabs) == 1:
                front = tabs[0]                        # one tab is unambiguously the front one
            if front is None:
                return None, READ_NO_TAB
            return getattr(front, "url", "") or "", READ_OK

        return cls(active_window=active_window, front_tab=front_tab)

    # --- reading -------------------------------------------------------

    def read(self) -> Surface:
        """One fresh reading of the foreground. Caches nothing, and never raises."""
        self.reads += 1
        if self._active_window is None:
            self.failures += 1
            return unreadable(READ_FAILED)
        try:
            window = self._active_window()
        except Exception as exc:
            _log.info("FOCUS_WINDOW_READ_FAILED reason=%s", type(exc).__name__)
            self.failures += 1
            return unreadable(READ_FAILED)
        if not window:
            self.failures += 1
            return unreadable(READ_NO_WINDOW)

        app = str((window or {}).get("app") or "").strip().lower()
        if not app:
            self.failures += 1
            return unreadable(READ_NO_WINDOW)
        bare = _PROC_SUFFIX.sub("", app)
        is_home = bare in {_PROC_SUFFIX.sub("", h) for h in self._home}
        is_browser = bare in {_PROC_SUFFIX.sub("", b) for b in self._browsers}

        host, tab_status, tab_handle = "", "", ""
        if is_browser:
            host, tab_status = self._read_host()
            if host:
                tab_handle = _handle(TAB, host)
        return Surface(status=READ_OK, app_handle=_handle(APP, bare),
                       tab_handle=tab_handle, app_label=bare, host=host,
                       is_home=is_home, is_browser=is_browser, tab_status=tab_status)

    def _read_host(self) -> tuple[str, str]:
        """The host of the browser's front tab, and why it is empty when it is.

        The HOST, never the full URL. A work tool is usually a single-page application whose path
        changes with every click, so locking the path would read as a drift the first time the owner
        opened a second record - site level is the honest granularity, and it is what the pack
        settled on for the same reason.
        """
        if self._front_tab is None:
            return "", READ_NO_BROWSER_LAYER
        try:
            url, status = self._front_tab()
        except Exception as exc:
            _log.info("FOCUS_TAB_READ_FAILED reason=%s", type(exc).__name__)
            return "", READ_NO_TAB
        if not url:
            return "", status or READ_NO_TAB
        try:
            host = (urlsplit(str(url)).hostname or "").lower()
        except ValueError:
            return "", READ_NO_TAB
        return (host, READ_OK) if host else ("", READ_NO_TAB)
