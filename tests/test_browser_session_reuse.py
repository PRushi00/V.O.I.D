"""Use the browser the owner already has open - or say plainly that you did not.

The complaint: Opera GX was running, "Open YouTube" worked, and it worked in a *second* V.O.I.D
browser with none of the owner's logins.

Three approaches were measured on the real machine before anything was written:

* **CDP attach.** Opera GX does speak it - started with ``--remote-debugging-port`` and its own
  ``--user-data-dir`` it reported ``Chrome/152.0.7977.120``, protocol 1.3. But a debugging port
  **cannot be retrofitted onto a running browser**: launching the same executable with the flag
  while Opera was open only signalled the existing instance, the browser process was unchanged and
  port 9222 stayed closed. It also has no authentication, so an open port hands any local process
  the owner's cookies and sessions.
* **Windows UI Automation.** Not viable: UIA finds the Opera window and reads **zero** elements,
  because Chromium does not publish its accessibility tree by default. No address bar, no tab strip.
* **The OS default handler** - what double-clicking a link does. Verified to reuse the session.

So the third was chosen, with CDP keeping precedence where it is available (it can verify the page)
and the controlled browser remaining the fallback after both.

Two measurement mistakes are pinned here as tests, because both produced a confidently wrong answer
and either would come back:

1. **"lowest pid" is not "the browser".** Windows reuses pids, so a freshly started helper took a
   lower number than the real browser and the check reported a brand-new session where there was
   none. The browser is the process with no ``--type=`` argument.
2. **A new browser process is not a second browser.** Windows starts a transient ``opera.exe`` to
   carry the URL, which signals the running instance and exits - and for that moment it has no
   ``--type=`` either. Measured immediately it looked like a second browser every single time.
   Persistence is the test, not existence.
"""
from __future__ import annotations

import time

import pytest

from void.browser.existing_session import (Reuse, _is_browser_root, cdp_available,
                                           is_default_handler, spoken_browser)


# =========================================================================== which browser the shell uses

@pytest.mark.parametrize("browser, progid, expected", [
    ("opera gx", "Opera GXStable", True),
    ("opera", "Opera GXStable", True),
    ("edge", "MSEdgeHTM", True),
    ("chrome", "ChromeHTML", True),
    ("firefox", "Opera GXStable", False),
    ("edge", "Opera GXStable", False),
    ("opera gx", "ChromeHTML", False),
])
def test_the_default_handler_is_matched_by_name_not_by_a_table(browser, progid, expected):
    """Compared with ``squash``, the project's one definition of "the same name spaced differently",
    rather than a second table mapping browsers to ProgIds that would drift from reality."""
    assert is_default_handler(browser, progid) is expected


@pytest.mark.parametrize("progid", ["", None])
def test_an_unreadable_handler_means_no_reuse(progid):
    """Without knowing where the shell would land, a hand-off could silently open the wrong
    browser - which is worse than not reusing the session at all."""
    assert is_default_handler("opera gx", progid or "") is False


def test_a_blank_browser_name_matches_nothing():
    assert is_default_handler("", "Opera GXStable") is False
    assert is_default_handler(None, "Opera GXStable") is False


# =========================================================================== browser vs its children

@pytest.mark.parametrize("cmdline, is_root", [
    ([r"C:\opera.exe"], True),
    ([r"C:\opera.exe", "--remote-debugging-port=9222"], True),
    ([r"C:\opera.exe", "--type=renderer"], False),
    ([r"C:\opera.exe", "--type=gpu-process"], False),
    ([r"C:\opera.exe", "--type=utility", "--utility-sub-type=audio"], False),
    ([], True),
    (None, True),
])
def test_the_browser_is_the_process_with_no_type_argument(cmdline, is_root):
    """Chromium gives every helper a ``--type=``; the browser process gets none. This is what tells
    "a new tab" from "a second browser", and counting processes cannot."""
    assert _is_browser_root(cmdline) is is_root


def test_the_lowest_pid_is_not_used_to_find_the_browser():
    """The first version of this used ``min(pids)`` as the browser and was simply wrong: Windows
    reuses pids, so a helper started later took a lower number and the check reported a brand-new
    session where there was none. Pinned so the shortcut does not come back."""
    import inspect

    import void.browser.existing_session as module

    source = inspect.getsource(module)
    assert "min(" not in source, "a pid-ordering shortcut reappeared"


# =========================================================================== transient vs persistent

class FakeReuse(Reuse):
    """A Reuse whose process view is scripted, so the settle logic can be tested without a browser."""

    def __init__(self, sessions, tabs, image="opera.exe"):
        super().__init__("opera gx")
        self.image = image
        self._script_sessions = list(sessions)
        self._script_tabs = list(tabs)

    def _next(self, script):
        return frozenset(script[0] if len(script) == 1 else script.pop(0))

    def before(self):
        self._sessions = self._next(self._script_sessions)
        self._tabs = self._next(self._script_tabs)


def _patch_views(monkeypatch, sessions, tabs):
    import void.browser.existing_session as module

    state = {"s": list(sessions), "t": list(tabs)}

    def session_pids(_image):
        return frozenset(state["s"][0] if len(state["s"]) == 1 else state["s"].pop(0))

    def tab_pids(_image):
        return frozenset(state["t"][0] if len(state["t"]) == 1 else state["t"].pop(0))

    monkeypatch.setattr(module, "session_pids", session_pids)
    monkeypatch.setattr(module, "tab_pids", tab_pids)


def test_a_transient_handoff_process_is_not_a_second_browser(monkeypatch):
    """Windows starts an ``opera.exe`` to carry the URL; it signals the running instance and exits.
    Measured immediately that looked like a second browser every time."""
    # before: {2768}; during: a transient extra; then it is gone and a renderer has appeared.
    _patch_views(monkeypatch, [{2768}, {2768, 9999}, {2768}], [{1}, {1}, {1, 2}])
    reuse = Reuse("opera gx")
    reuse.image = "opera.exe"
    reuse.before()
    reused, detail = reuse.after(settle_s=3.0, step_s=0.01)
    assert reused is True, detail
    assert "new tab process" in detail


def test_a_persistent_second_browser_is_reported_as_a_failure_of_reuse(monkeypatch):
    """The one outcome that must never be dressed up as reuse - it is the whole complaint."""
    _patch_views(monkeypatch, [{2768}, {2768, 5555}], [{1}, {1}])
    reuse = Reuse("opera gx")
    reuse.image = "opera.exe"
    reuse.before()
    started = time.monotonic()
    reused, detail = reuse.after(settle_s=0.4, step_s=0.01)
    assert reused is False
    assert "second browser" in detail
    assert (time.monotonic() - started) < 10.0, "the settle window must stay bounded"


def test_a_browser_that_disappeared_is_not_reuse(monkeypatch):
    _patch_views(monkeypatch, [{2768}, set()], [{1}, {1}])
    reuse = Reuse("opera gx")
    reuse.image = "opera.exe"
    reuse.before()
    reused, detail = reuse.after(settle_s=0.3, step_s=0.01)
    assert reused is False and "no longer running" in detail


def test_a_replaced_browser_is_not_reuse(monkeypatch):
    """The process that was open has gone and a different one is there: not the owner's session."""
    _patch_views(monkeypatch, [{2768}, {4242}], [{1}, {1}])
    reuse = Reuse("opera gx")
    reuse.image = "opera.exe"
    reuse.before()
    reused, detail = reuse.after(settle_s=0.3, step_s=0.01)
    assert reused is False


def test_reuse_without_a_new_tab_is_still_reuse(monkeypatch):
    """An existing tab may be reused, which is legitimate - and is not claimed as a new one."""
    _patch_views(monkeypatch, [{2768}], [{1}])
    reuse = Reuse("opera gx")
    reuse.image = "opera.exe"
    reuse.before()
    reused, detail = reuse.after(settle_s=0.2, step_s=0.01)
    assert reused is True and "may have been reused" in detail


# =========================================================================== when reuse applies

def test_reuse_needs_both_the_default_handler_and_a_running_browser(monkeypatch):
    import void.browser.existing_session as module

    monkeypatch.setattr(module, "default_https_progid", lambda: "Opera GXStable")
    monkeypatch.setattr(module, "session_pids", lambda image: frozenset({2768}))
    assert Reuse("opera gx").possible is True

    monkeypatch.setattr(module, "session_pids", lambda image: frozenset())
    assert Reuse("opera gx").possible is False, "nothing is running, so there is nothing to reuse"

    monkeypatch.setattr(module, "session_pids", lambda image: frozenset({2768}))
    monkeypatch.setattr(module, "default_https_progid", lambda: "MSEdgeHTM")
    assert Reuse("opera gx").possible is False, "the shell would land in a different browser"


def test_a_browser_the_project_cannot_drive_is_not_claimed(monkeypatch):
    import void.browser.existing_session as module

    monkeypatch.setattr(module, "default_https_progid", lambda: "FirefoxURL")
    assert Reuse("firefox").image == "", "firefox is not in the browser layer's table"
    assert Reuse("firefox").possible is False


@pytest.mark.parametrize("port, expected", [(0, False), (-1, False), ("x", False), (None, False)])
def test_a_disabled_or_invalid_cdp_port_is_not_available(port, expected):
    assert cdp_available(port) is expected


# =========================================================================== the route's precedence

def test_the_controlled_path_keeps_precedence_when_cdp_can_attach(monkeypatch):
    """A hand-off cannot be verified past the hand-off; CDP can read the page back. So when a
    debugging port is listening the verifiable path wins, and reuse declines."""
    from void.app import Assistant
    import void.app as app_module

    assistant = Assistant()
    assistant.clear_stop()
    monkeypatch.setattr(app_module, "cdp_available", lambda port: True)

    from void.orchestration.websites import parse_target
    state = assistant.world_state("Open YouTube")
    assert assistant._reuse_open_browser(parse_target("Open YouTube"), state) is None


def test_reuse_declines_when_no_browser_is_preferred(monkeypatch):
    from void.app import Assistant
    from void.orchestration.routes import WorldState
    from void.orchestration.websites import parse_target

    assistant = Assistant()
    assistant.clear_stop()
    blank = WorldState(capabilities=frozenset({"browser"}))
    assert assistant._reuse_open_browser(parse_target("Open YouTube"), blank) is None


# =========================================================================== an explicit browser

class FakeAdapter:
    """Just enough adapter to exercise ``prefer`` handling in the route."""

    def __init__(self, *, answer=True):
        self.answer = answer
        self.asked: list[str] = []
        self.connected = False

    def prefer(self, browser):
        self.asked.append(str(browser))
        return self.answer

    def available(self):
        return True

    class policy:
        cdp_port = 0


def test_an_explicit_browser_reaches_the_launch(monkeypatch):
    """"open YouTube in Edge" used to start Opera GX: the adapter resolved the browser from its
    CONFIGURED preference, so the override reached the routing decision and stopped there."""
    from void.browser import BrowserPolicy
    from void.browser.playwright_adapter import PlaywrightBrowser

    adapter = PlaywrightBrowser(BrowserPolicy(enabled=True, preferred="opera gx"))
    assert adapter.prefer("edge") is True
    assert adapter.policy.preferred == "edge"


def test_asking_for_the_browser_already_configured_is_a_no_op():
    from void.browser import BrowserPolicy
    from void.browser.playwright_adapter import PlaywrightBrowser

    adapter = PlaywrightBrowser(BrowserPolicy(enabled=True, preferred="opera gx"))
    assert adapter.prefer("opera gx") is True
    assert adapter.policy.preferred == "opera gx"


def test_a_running_session_cannot_be_switched_to_another_browser():
    """The running browser cannot become a different browser, and closing the owner's window to
    switch would be a worse surprise than saying so."""
    from void.browser import BrowserPolicy
    from void.browser.playwright_adapter import PlaywrightBrowser

    adapter = PlaywrightBrowser(BrowserPolicy(enabled=True, preferred="opera gx"))
    adapter._context = object()                                # a session is open
    assert adapter.prefer("edge") is False
    assert adapter.policy.preferred == "opera gx", "the live session's browser was changed"


def test_a_blank_request_changes_nothing():
    from void.browser import BrowserPolicy
    from void.browser.playwright_adapter import PlaywrightBrowser

    adapter = PlaywrightBrowser(BrowserPolicy(enabled=True, preferred="opera gx"))
    assert adapter.prefer("") is False
    assert adapter.policy.preferred == "opera gx"


# =========================================================================== observing costs nothing

def test_building_the_world_state_does_not_open_a_browser():
    """The second Opera window the owner saw was opened by *observation*: ``world_state`` read the
    tab list for every goal, ``tabs()`` runs ``_ensure()``, and ``_ensure()`` launches."""
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    if assistant.browser is None:
        pytest.skip("browser automation is off in this configuration")
    assert assistant.browser.connected is False
    assistant.world_state("Open YouTube")
    assert assistant.browser.connected is False, "observing the world started a browser"


def test_connected_reports_a_session_without_creating_one():
    from void.browser import BrowserPolicy
    from void.browser.playwright_adapter import PlaywrightBrowser

    adapter = PlaywrightBrowser(BrowserPolicy(enabled=True, preferred="opera gx"))
    assert adapter.connected is False
    adapter._context = object()
    assert adapter.connected is True


# =========================================================================== no fabricated success

def test_the_reuse_reply_does_not_claim_the_page_loaded(monkeypatch):
    """V.O.I.D cannot read the owner's browser - no CDP, no accessibility tree - so it must not say
    the page is up. It says what it did and what it could not confirm."""
    import void.app as app_module
    from void.app import Assistant
    from void.orchestration.websites import parse_target

    assistant = Assistant()
    assistant.clear_stop()
    monkeypatch.setattr(app_module, "cdp_available", lambda port: False)

    class AlwaysReuse(app_module.Reuse):
        possible = True

        def before(self):
            pass

        def after(self, *a, **k):
            return True, "1 new tab process(es) in the session already open"

    monkeypatch.setattr(app_module, "Reuse", lambda browser: AlwaysReuse(browser))
    state = assistant.world_state("Open YouTube")
    result = assistant._reuse_open_browser(parse_target("Open YouTube"), state)
    assert result is not None
    assert result.status == "completed"
    assert "have not confirmed" in result.result
    assert result.engine_authored is True


def test_a_second_browser_is_reported_not_hidden(monkeypatch):
    import void.app as app_module
    from void.app import Assistant
    from void.orchestration.websites import parse_target

    assistant = Assistant()
    assistant.clear_stop()
    monkeypatch.setattr(app_module, "cdp_available", lambda port: False)

    class SecondBrowser(app_module.Reuse):
        possible = True

        def before(self):
            pass

        def after(self, *a, **k):
            return False, "a second browser was started instead of reusing the open one"

    monkeypatch.setattr(app_module, "Reuse", lambda browser: SecondBrowser(browser))
    state = assistant.world_state("Open YouTube")
    result = assistant._reuse_open_browser(parse_target("Open YouTube"), state)
    assert result is not None
    assert result.status == "failed"
    assert "not in the" in result.result and "second browser" in (result.task.error or "")


# =========================================================================== small helpers

@pytest.mark.parametrize("name, said", [
    ("opera gx", "Opera GX"), ("edge", "Edge"), ("chrome", "Chrome"),
    ("opera", "Opera"), ("Opera GX", "Opera GX"), ("", ""),
])
def test_a_browser_is_named_the_way_it_is_written(name, said):
    assert spoken_browser(name) == said


def test_no_debugging_port_is_ever_opened_by_this_module():
    """An unauthenticated CDP port hands any local process the owner's cookies and sessions. This
    module only ever *asks whether* one is listening."""
    import ast
    import inspect

    import void.browser.existing_session as module

    tree = ast.parse(inspect.getsource(module))
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for forbidden in ("Popen", "startfile", "system", "remote_debugging", "bind", "listen"):
        assert forbidden not in names, f"the module references {forbidden!r}"
    assert "connect" in names, "the only socket use should be a connect probe"
