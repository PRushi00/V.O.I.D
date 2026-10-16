"""Reusing the browser the owner already has open.

The complaint: Opera GX is running, "Open YouTube" works - and it works in a *second*, V.O.I.D-
controlled browser, with none of the owner's logins. They want their own window used.

The browser layer already prefers reuse: :meth:`PlaywrightBrowser._ensure` attaches over CDP first
and only launches when it cannot. The reason it never attaches here is measured, not guessed:

* Opera GX **does** speak CDP - started with ``--remote-debugging-port`` and its own
  ``--user-data-dir`` it reported ``Chrome/152.0.7977.120``, protocol ``1.3``;
* but a debugging port **cannot be retrofitted onto a running browser**. Launching the same
  executable with the flag while Opera is open merely signals the existing instance and exits:
  the browser process (pid 2768) was unchanged and port 9222 stayed closed;
* and Windows UI Automation is no help either - it finds the Opera window and reads **zero**
  elements, because Chromium does not publish its accessibility tree by default. No address bar, no
  tab strip, nothing to drive or to read.

So reusing the *already open* session cannot go through CDP or through UIA. What does work is the
mechanism the owner's own desktop uses: hand the URL to Windows, which gives it to the default
handler, which opens a tab in the running browser. Measured - the browser process was unchanged and
only new renderer processes appeared, which is exactly what a new tab looks like.

**What this module is for.** Two questions the website route needs answered before it decides:
"would the shell land in the browser we actually mean?" and "did a tab really appear in the session
that was already running, rather than a second browser?" It answers both from the process table and
the owner's own file-association choice. It opens nothing, navigates nothing and holds no policy -
the route does that, through the existing ``open_path`` tool and therefore the existing risk gate.

**Why not simply always use the shell.** Because it is strictly weaker: a shell hand-off cannot be
verified past the hand-off. When CDP *is* available the controlled path can read the page back and
confirm the host, so that stays preferred. This is the fallback that keeps the owner's session, and
the controlled browser remains the fallback after it.
"""
from __future__ import annotations

import logging
import time

from void.actions.app_names import squash

_log = logging.getLogger(__name__)

#: Where Windows records the owner's own choice of https handler. Read-only, and only this value.
_URL_ASSOCIATION = r"SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"

#: Longest ProgId considered. A guard on a value read from the registry, not a meaningful limit.
_MAX_PROGID = 120

#: How long a new browser process is given to prove it is only the hand-off and not a real second
#: browser. Measured: the transient ``opera.exe`` that carries a URL to the running instance is gone
#: well inside a second; 6 s is slack for a loaded machine, and it only delays the *detail*.
SETTLE_S = 6.0


def default_https_progid() -> str:
    """The ProgId Windows will hand an ``https`` URL to, or "" when it cannot be read.

    This is the owner's setting, made in Windows' own UI. Nothing here changes it.
    """
    try:
        import winreg
    except Exception:                                          # noqa: BLE001 - not Windows
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _URL_ASSOCIATION) as key:
            value, _kind = winreg.QueryValueEx(key, "ProgId")
    except OSError:
        return ""
    return str(value or "")[:_MAX_PROGID]


def is_default_handler(browser: object, progid: str | None = None) -> bool:
    """Would an ``https`` URL handed to Windows open in ``browser``?

    Compared with :func:`void.actions.app_names.squash`, the project's one definition of "the same
    name spaced differently", rather than a second table of ProgIds. It is containment rather than
    equality because a ProgId carries a suffix the owner never says:

        "opera gx" -> "operagx"  in  "Opera GXStable" -> "operagxstable"      yes
        "edge"     -> "edge"     in  "MSEdgeHTM"      -> "msedgehtm"          yes
        "chrome"   -> "chrome"   in  "ChromeHTML"     -> "chromehtml"         yes
        "firefox"  -> "firefox"  in  "operagxstable"                          no

    An empty or unreadable ProgId answers False: without knowing where the shell would land, a
    hand-off could silently open the wrong browser, which is worse than not reusing the session.
    """
    wanted = squash(browser)
    if not wanted:
        return False
    found = squash(progid if progid is not None else default_https_progid())
    return bool(found) and wanted in found


def cdp_available(port: object) -> bool:
    """Is a browser listening for CDP on ``port`` right now?

    The same question :meth:`PlaywrightBrowser._ensure` asks before it decides to attach, asked here
    so the website route can tell which path is better *before* executing one. When a browser is
    listening, the controlled path wins: it can read the page back and confirm the host. Only when
    nothing is listening is handing the URL to the owner's own browser the better answer.
    """
    try:
        number = int(port)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    import socket

    probe = socket.socket()
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", number))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def spoken_browser(name: object) -> str:
    """A browser name as it should be said back: "opera gx" -> "Opera GX".

    The table keys are lower case because they are match keys, and saying "the opera gx you already
    had open" reads like a typo. Words of one or two letters are upper-cased, which is what makes
    "gx" right and does not disturb "Edge" or "Chrome"; anything already carrying capitals is left
    exactly as it is.
    """
    text = " ".join(str(name or "").split())
    if not text or text != text.lower():
        return text
    return " ".join(word.upper() if len(word) <= 2 else word[:1].upper() + word[1:]
                    for word in text.split())


def _processes():
    """Every process, with name and command line. Empty when psutil is unavailable."""
    try:
        import psutil
    except Exception:                                          # noqa: BLE001 - optional dependency
        _log.debug("PSUTIL_UNAVAILABLE", exc_info=True)
        return ()
    try:
        return tuple(psutil.process_iter(["name", "cmdline", "pid"]))
    except Exception:                                          # noqa: BLE001
        _log.debug("PROCESS_LIST_UNAVAILABLE", exc_info=True)
        return ()


def _is_browser_root(cmdline) -> bool:
    """A browser's own process, not one of its children.

    Chromium gives every helper a ``--type=`` argument - renderer, gpu-process, utility - and gives
    the browser process none. That distinction is the whole reason this function exists: counting
    processes cannot tell "a new tab" from "a second browser", and the first measurement of this
    used the lowest pid as the root, which is simply wrong. Windows reuses pids, so a freshly
    started helper took a lower number than the real browser and the check reported a brand-new
    session where there was none.
    """
    return not any(str(argument).startswith("--type=") for argument in (cmdline or ()))


def session_pids(image: object) -> frozenset[int]:
    """The pids of running *browser* processes for an executable name (e.g. "opera.exe").

    One entry per open browser session. Comparing this set across an action is what distinguishes
    "a tab opened in the session that was already there" from "a second browser started".
    """
    wanted = str(image or "").strip().lower()
    if not wanted:
        return frozenset()
    found = set()
    for process in _processes():
        try:
            info = process.info
            if str(info.get("name") or "").lower() != wanted:
                continue
            if _is_browser_root(info.get("cmdline")):
                found.add(int(info.get("pid")))
        except Exception:                                      # noqa: BLE001 - a process may vanish
            continue
    return frozenset(found)


def tab_pids(image: object) -> frozenset[int]:
    """The pids of a browser's renderer processes - one or more per tab.

    A tab is not a process and a process is not a tab, so this is evidence rather than proof: a
    renderer appearing inside an unchanged session is what a new tab looks like from outside, and it
    is the strongest signal available for a browser that exposes neither CDP nor an accessibility
    tree. The route says exactly that much and no more.
    """
    wanted = str(image or "").strip().lower()
    if not wanted:
        return frozenset()
    found = set()
    for process in _processes():
        try:
            info = process.info
            if str(info.get("name") or "").lower() != wanted:
                continue
            if any(str(a).startswith("--type=renderer") for a in (info.get("cmdline") or ())):
                found.add(int(info.get("pid")))
        except Exception:                                      # noqa: BLE001
            continue
    return frozenset(found)


def image_for(browser: object) -> str:
    """The executable name a browser runs as, from the browser layer's own table.

    Read from :data:`void.browser.playwright_adapter._BROWSERS` - the same rows used to launch one -
    so a browser this project cannot drive is also one this cannot claim to find running.
    """
    wanted = squash(browser)
    if not wanted:
        return ""
    try:
        from void.browser.playwright_adapter import _BROWSERS
    except Exception:                                          # noqa: BLE001
        return ""
    import os

    for row in _BROWSERS:
        name = squash(row[0])
        if not name or name not in wanted and wanted not in name:
            continue
        for raw in (row[2] if len(row) > 2 else ()) or ():
            tail = os.path.basename(os.path.expandvars(str(raw)))
            if tail:
                return tail.lower()
        channel = str(row[1]).strip().lower() if len(row) > 1 else ""
        if channel:
            return channel + ".exe"
    return ""


class Reuse:
    """Whether the owner's own browser can be used, and what changed when it was.

    A plain holder with two questions and no authority: it never opens anything. The route hands the
    URL to the existing ``open_path`` tool, so the kill switch and ``RiskGate`` apply exactly as
    they do to any other call.
    """

    def __init__(self, browser: object):
        self.browser = str(browser or "")
        self.image = image_for(self.browser)
        self._sessions: frozenset[int] = frozenset()
        self._tabs: frozenset[int] = frozenset()

    @property
    def possible(self) -> bool:
        """True when a hand-off would land in this browser AND it is already running.

        Both halves matter. Without the first the shell could open a different browser than the one
        asked for; without the second there is no session to reuse and the controlled path, which
        can actually verify itself, is the better answer.
        """
        if not self.image or not is_default_handler(self.browser):
            return False
        return bool(session_pids(self.image))

    def before(self) -> None:
        self._sessions = session_pids(self.image)
        self._tabs = tab_pids(self.image)

    def after(self, settle_s: float = SETTLE_S, step_s: float = 0.3) -> tuple[bool, str]:
        """``(reused, detail)`` - did a tab appear in the session that was already open?

        The failure this exists to make impossible is reporting reuse while having started a second
        browser. But "a new browser process exists" cannot be read the instant after the hand-off,
        and getting that wrong is what the first version of this did: Windows starts a **transient**
        ``opera.exe`` to carry the URL, that process signals the running instance and exits, and for
        a moment it carries no ``--type=`` argument - so it is indistinguishable from a real second
        browser. Measured immediately it looked like a second browser every time; measured after a
        pause there was none.

        So persistence is the test, not existence. This waits up to ``settle_s`` for any new browser
        process to go away. One that stays really is a second browser and is reported as a failure
        of reuse; one that disappears was the hand-off itself.
        """
        deadline = time.monotonic() + max(0.0, float(settle_s))
        detail = ""
        while True:
            sessions = session_pids(self.image)
            if not sessions:
                return False, "the browser is no longer running"
            if not (self._sessions & sessions):
                return False, "the browser that was open has gone"
            extra = sessions - self._sessions
            new_tabs = tab_pids(self.image) - self._tabs
            if not extra:
                if new_tabs:
                    return True, f"{len(new_tabs)} new tab process(es) in the session already open"
                detail = "no new tab process appeared; an open tab may have been reused"
                # Keep waiting a little: a renderer often lags the hand-off. The answer is already
                # "reused" either way, so this only sharpens the detail.
                if time.monotonic() >= deadline:
                    return True, detail
            elif time.monotonic() >= deadline:
                return False, (f"a second browser was started instead of reusing the open one "
                               f"({len(extra)} new process(es) still running)")
            time.sleep(max(0.05, float(step_s)))
