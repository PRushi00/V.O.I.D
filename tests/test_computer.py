"""Phase 8A: Windows application & window control.

Deterministic logic tests over a FAKE backend (no pywin32/psutil, no GUI, no
Gemini). The fake backend imitates ONLY V.O.I.D's higher-level interface - it is
never treated as proof of real Windows API behavior (that is the smoke test's
job, verify/computer_smoke.py).
"""
import pytest

from void.actions.apps import AppActions
from void.actions.base import ToolResult
from void.actions.computer import (
    AppCatalog, ComputerActions, ComputerBackendError, WindowsBackend,
)
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import Status, TaskStore
from void.providers.base import LLMResponse
from void.security.risk import RiskGate, RiskLevel

from tests.helpers import FakeProvider, tool_call


class FakeBackend(WindowsBackend):
    """In-memory backend for logic tests - NOT the real Windows API."""

    def __init__(self, apps=None, windows=None, procs=None, current_pid=999,
                 activate_result=True, close_result=True, raise_on=None):
        self._apps = apps or []
        self._windows = [dict(w) for w in (windows or [])]
        self._procs = dict(procs or {})
        self._cur = current_pid
        self._activate_result = activate_result
        self._close_result = close_result
        self._raise_on = set(raise_on or ())
        self._alive = {w["hwnd"] for w in self._windows}
        self.launched, self.activated, self.closed = [], [], []

    def _maybe(self, m):
        if m in self._raise_on:
            raise ComputerBackendError("fake backend unavailable")

    def discover_apps(self):
        self._maybe("discover_apps")
        return [dict(a) for a in self._apps]

    def launch(self, kind, target):
        self._maybe("launch")
        self.launched.append((kind, target))

    def list_windows(self):
        self._maybe("list_windows")
        return [dict(w) for w in self._windows]

    def window_exists(self, hwnd):
        return hwnd in self._alive

    def window_pid(self, hwnd):
        for w in self._windows:
            if w["hwnd"] == hwnd:
                return w["pid"]
        return None

    def window_title(self, hwnd):
        for w in self._windows:
            if w["hwnd"] == hwnd:
                return w["title"]
        return None

    def process_name(self, pid):
        return self._procs.get(pid)

    def current_pid(self):
        return self._cur

    def activate(self, hwnd):
        self.activated.append(hwnd)
        return self._activate_result

    def close(self, hwnd):
        self.closed.append(hwnd)
        return self._close_result

    # test mutators (simulate volatility / TOCTOU)
    def kill_window(self, hwnd):
        self._alive.discard(hwnd)

    def repoint_window(self, hwnd, pid):
        for w in self._windows:
            if w["hwnd"] == hwnd:
                w["pid"] = pid


def _exe(tmp_path, name):
    p = tmp_path / name
    p.write_text("stub")   # a real, existing target so revalidate() passes
    return str(p)


# --- find_app: 0 / 1 / N -----------------------------------------------

def test_find_app_zero(tmp_path):
    be = FakeBackend(apps=[{"name": "Notepad", "kind": "exe",
                            "target": _exe(tmp_path, "n.exe")}])
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.find_app("Nope")
    assert r.ok and r.data == [] and "No applications matched" in r.summary


def test_find_app_one(tmp_path):
    t = _exe(tmp_path, "n.exe")
    be = FakeBackend(apps=[{"name": "Notepad", "kind": "exe", "target": t}])
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.find_app("notepad")
    assert r.ok and len(r.data) == 1
    assert r.data[0]["name"] == "Notepad" and r.data[0]["app_id"].startswith("app-")
    assert "target" not in r.data[0] and t not in r.summary   # path not disclosed


def test_find_app_ambiguous(tmp_path):
    be = FakeBackend(apps=[
        {"name": "Editor", "kind": "exe", "target": _exe(tmp_path, "a.exe")},
        {"name": "editor", "kind": "lnk", "target": _exe(tmp_path, "b.lnk")},
    ])
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.find_app("Editor")
    assert r.ok and len(r.data) == 2 and "ambiguous" in r.summary.lower()


def test_find_app_no_substring(tmp_path):
    be = FakeBackend(apps=[{"name": "Notepad++", "kind": "exe",
                            "target": _exe(tmp_path, "n.exe")}])
    ca = ComputerActions(be, AppCatalog(be))
    assert ca.find_app("Notepad").data == []      # exact only, not substring


# --- launch_app hardening ----------------------------------------------

def test_launch_by_app_id(tmp_path):
    t = _exe(tmp_path, "n.exe")
    be = FakeBackend(apps=[{"name": "Notepad", "kind": "exe", "target": t}])
    cat = AppCatalog(be)
    launched = []
    aa = AppActions(FileActions(allowed_roots=[tmp_path]), catalog=cat,
                    launcher=lambda kind, target: launched.append((kind, target)))
    app_id = ca_app_id(cat, "Notepad")
    r = aa.launch_app(app_id)
    assert r.ok and launched == [("exe", t)]


def test_launch_stale_app_id(tmp_path):
    t = _exe(tmp_path, "n.exe")
    be = FakeBackend(apps=[{"name": "Notepad", "kind": "exe", "target": t}])
    cat = AppCatalog(be)
    app_id = ca_app_id(cat, "Notepad")
    import os
    os.remove(t)                                   # target vanished
    aa = AppActions(FileActions(allowed_roots=[tmp_path]), catalog=cat,
                    launcher=lambda *a: None)
    r = aa.launch_app(app_id)
    assert not r.ok and "no longer available" in r.summary


def test_launch_arbitrary_input_refused_no_shell(tmp_path, monkeypatch):
    calls = []
    import void.actions.apps as appsmod
    monkeypatch.setattr(appsmod.subprocess, "Popen",
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(appsmod.shutil, "which", lambda *_: None)
    aa = AppActions(FileActions(allowed_roots=[tmp_path]))
    r = aa.launch_app('mystery.exe & del C:\\stuff')
    assert not r.ok and "Unknown application" in r.summary
    assert calls == []                             # nothing launched, no shell


def test_launch_alias_uses_no_shell(tmp_path, monkeypatch):
    calls = []
    import void.actions.apps as appsmod
    monkeypatch.setattr(appsmod.shutil, "which",
                        lambda c: r"C:\Windows\System32\notepad.exe")
    monkeypatch.setattr(appsmod.subprocess, "Popen",
                        lambda argv, *a, **k: calls.append((argv, a, k)))
    aa = AppActions(FileActions(allowed_roots=[tmp_path]))
    r = aa.launch_app("notepad")
    assert r.ok
    argv = calls[0][0]
    assert isinstance(argv, list) and argv == [r"C:\Windows\System32\notepad.exe"]
    assert "cmd" not in " ".join(argv).lower() and calls[0][2].get("shell") in (None, False)


# --- list_running_apps / list_windows ----------------------------------

def _win_backend(tmp_path):
    return FakeBackend(
        windows=[{"hwnd": 10, "pid": 100, "title": "Untitled - Notepad"},
                 {"hwnd": 11, "pid": 101, "title": "readme - Code"}],
        procs={100: "notepad.exe", 101: "Code.exe", 999: "python.exe"})


def test_list_running_apps(tmp_path):
    be = _win_backend(tmp_path)
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.list_running_apps()
    names = {d["name"] for d in r.data}
    assert names == {"notepad.exe", "Code.exe"}


def test_list_windows_makes_tokens(tmp_path):
    be = _win_backend(tmp_path)
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.list_windows()
    assert len(r.data) == 2
    assert all(d["window_token"].startswith("win-") for d in r.data)
    # title appears in summary (untrusted data the agent will frame)
    assert "Untitled - Notepad" in r.summary


# --- window revalidation (TOCTOU) --------------------------------------

def _one_window(activate_result=True, close_result=True, cur=999):
    return FakeBackend(windows=[{"hwnd": 10, "pid": 100, "title": "Doc - Notepad"}],
                       procs={100: "notepad.exe", 999: "python.exe"},
                       activate_result=activate_result, close_result=close_result,
                       current_pid=cur)


def _token(ca):
    return ca.list_windows().data[0]["window_token"]


def test_activate_valid(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.activate_window(_token(ca))
    assert r.ok and be.activated == [10]


def test_activate_unknown_token(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.activate_window("win-does-not-exist")
    assert not r.ok and "expired" in r.summary and be.activated == []


def test_activate_window_gone(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    tok = _token(ca)
    be.kill_window(10)
    r = ca.activate_window(tok)
    assert not r.ok and "no longer exists" in r.summary and be.activated == []


def test_activate_pid_reused(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    tok = _token(ca)
    be.repoint_window(10, 500)   # same hwnd, different pid
    r = ca.activate_window(tok)
    assert not r.ok and "reused by another process" in r.summary


def test_activate_identity_changed(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    tok = _token(ca)
    be._procs[100] = "evil.exe"   # same pid, different image name
    r = ca.activate_window(tok)
    assert not r.ok and "identity changed" in r.summary


def test_activate_foreground_restricted(tmp_path):
    be = _one_window(activate_result=False)
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.activate_window(_token(ca))
    assert not r.ok and "foreground" in r.summary   # graceful failure


# --- protected process policy ------------------------------------------

def test_close_normal_window(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.close_app(_token(ca))
    assert r.ok and be.closed == [10] and "graceful close" in r.summary


def test_close_protected_shell_denied(tmp_path):
    be = FakeBackend(windows=[{"hwnd": 20, "pid": 4, "title": "Desktop"}],
                     procs={4: "explorer.exe", 999: "python.exe"})
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.close_app(ca.list_windows().data[0]["window_token"])
    assert not r.ok and "protected" in r.summary and be.closed == []


def test_close_self_process_denied(tmp_path):
    be = FakeBackend(windows=[{"hwnd": 21, "pid": 999, "title": "V.O.I.D"}],
                     procs={999: "python.exe"}, current_pid=999)
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.close_app(ca.list_windows().data[0]["window_token"])
    assert not r.ok and "protected" in r.summary and be.closed == []


def test_close_configured_protected_denied(tmp_path):
    be = FakeBackend(windows=[{"hwnd": 22, "pid": 200, "title": "VM"}],
                     procs={200: "vmware.exe", 999: "python.exe"})
    ca = ComputerActions(be, AppCatalog(be), protected_processes=["vmware.exe"])
    r = ca.close_app(ca.list_windows().data[0]["window_token"])
    assert not r.ok and "protected" in r.summary


def test_close_refused_by_app(tmp_path):
    be = _one_window(close_result=False)
    ca = ComputerActions(be, AppCatalog(be))
    r = ca.close_app(_token(ca))
    assert not r.ok and "did not accept" in r.summary and "escalat" in r.summary.lower()


# --- risk classification -----------------------------------------------

def test_risk_levels(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    risk = {t.name: t.risk for t in ca.tools()}
    assert risk["find_app"] is RiskLevel.LOW
    assert risk["list_running_apps"] is RiskLevel.LOW
    assert risk["list_windows"] is RiskLevel.LOW
    assert risk["activate_window"] is RiskLevel.LOW
    assert risk["close_app"] is RiskLevel.HIGH


# --- non-Windows / backend unavailable fails cleanly -------------------

def test_backend_unavailable_is_clean(tmp_path):
    be = FakeBackend(raise_on={"discover_apps", "list_windows"})
    ca = ComputerActions(be, AppCatalog(be))
    assert not ca.find_app("x").ok
    assert not ca.list_windows().ok
    assert not ca.list_running_apps().ok


# --- agent integration: risk gate / ledger / kill switch ---------------

def _agent(tmp_path, script, ca, *, ks=None, confirm_fn=None, defer=False):
    tools = ToolRegistry()
    tools.register_all(ca.tools())
    ks = ks or KillSwitch()
    gate = RiskGate(confirm_at_or_above="high", confirm_fn=confirm_fn)
    return Agent(FakeProvider(script), tools, gate, ks,
                 TaskStore(tmp_path / "t.sqlite"), max_retries=0,
                 defer_confirmation=defer), ks


def test_agent_records_computer_steps_in_ledger(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    tok = ca.list_windows().data[0]["window_token"]
    agent, _ = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("list_running_apps")]),
        LLMResponse(tool_calls=[tool_call("activate_window", window_token=tok)]),
        LLMResponse(text="done"),
    ], ca)
    result = agent.run("go")
    task = agent.store.load(result.task.id)
    assert [e["calls"][0]["tool"] for e in task.plan] == \
        ["list_running_apps", "activate_window"]
    assert all(e["status"] == "succeeded" for e in task.plan)


def test_close_app_defers_to_confirmation(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    tok = ca.list_windows().data[0]["window_token"]
    agent, _ = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("close_app", window_token=tok)]),
        LLMResponse(text="done"),
    ], ca, defer=True)
    result = agent.run("close it")
    assert result.status == Status.AWAITING_CONFIRMATION
    assert be.closed == []                          # nothing closed without approval


def test_killswitch_blocks_computer_action(tmp_path):
    be = _one_window()
    ca = ComputerActions(be, AppCatalog(be))
    tok = ca.list_windows().data[0]["window_token"]
    ks = KillSwitch()
    ks.engage(reason="stop")
    agent, _ = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("activate_window", window_token=tok)]),
    ], ca, ks=ks)
    result = agent.run("go")
    assert result.status == Status.PAUSED
    assert be.activated == []                        # kill switch beat the action


# helper: resolve an app_id by name for launch tests
def ca_app_id(catalog, name):
    matches = catalog.find(name)
    assert len(matches) == 1
    return matches[0].app_id
