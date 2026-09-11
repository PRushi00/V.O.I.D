"""Deterministic tests for the Gemini credential-management CLI.

A FAKE in-memory keyring is injected via monkeypatch; the real Windows
Credential Manager is never touched, and only obvious fake sentinel values are
used (never anything resembling a real Gemini API key).
"""
import getpass as _getpass_mod
import json

import pytest

from void import cli
from void.security import credentials, secrets

PRIMARY = secrets.GEMINI_API_KEY        # "gemini_api_key"
MANIFEST = credentials.MANIFEST_KEY     # "gemini_api_key_names"

FAKE_PRIMARY = "FAKE_GEMINI_KEY_PRIMARY"
FAKE_02 = "FAKE_GEMINI_KEY_02"
FAKE_03 = "FAKE_GEMINI_KEY_03"


@pytest.fixture
def store(monkeypatch):
    """In-memory fake keyring backing secrets.{get,set,delete}_secret."""
    data = {}
    monkeypatch.setattr(secrets, "set_secret", lambda k, v: data.__setitem__(k, v))
    monkeypatch.setattr(secrets, "get_secret", lambda k: data.get(k))

    def _delete(k):
        existed = k in data
        data.pop(k, None)
        return existed

    monkeypatch.setattr(secrets, "delete_secret", _delete)
    return data


@pytest.fixture
def enter_key(monkeypatch):
    """Queue hidden inputs; record getpass use and forbid plain input()."""
    calls = {"getpass": 0}
    queue: list[str] = []

    def fake_getpass(prompt=""):
        calls["getpass"] += 1
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(_getpass_mod, "getpass", fake_getpass)

    def _boom(*a, **k):
        raise AssertionError("input() must never be used to read a secret")

    monkeypatch.setattr("builtins.input", _boom)

    def feed(*values):
        queue.extend(values)

    return calls, feed


def _run(*argv):
    return cli.main(list(argv))


# 1. Existing primary command unchanged ---------------------------------

def test_set_key_primary_unchanged(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_PRIMARY)
    assert _run("set-key", "gemini") == 0
    assert store[PRIMARY] == FAKE_PRIMARY
    assert MANIFEST not in store          # no manifest for primary-only
    assert calls["getpass"] == 1          # hidden input used


# 2. Named credential ---------------------------------------------------

def test_set_key_named_stores_under_alias_and_updates_manifest(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02)
    assert _run("set-key", "gemini", "--name", "gemini_02") == 0
    assert store["gemini_02"] == FAKE_02
    names = json.loads(store[MANIFEST])
    assert "gemini_02" in names
    assert names[0] == PRIMARY


# 3. Hidden input -------------------------------------------------------

def test_hidden_input_used_not_plain_input(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02)
    # builtins.input is patched to raise; using it would fail the test.
    assert _run("set-key", "gemini", "--name", "gemini_02") == 0
    assert calls["getpass"] == 1


# 4 & 5. Manifest + ordering --------------------------------------------

def test_manifest_ordering_primary_first(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, FAKE_03)
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_03")
    assert json.loads(store[MANIFEST]) == [PRIMARY, "gemini_02", "gemini_03"]


# 6. Duplicate alias ----------------------------------------------------

def test_duplicate_alias_no_duplicate_manifest_entry(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, FAKE_02)
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_02")
    names = json.loads(store[MANIFEST])
    assert names.count("gemini_02") == 1


# 7. Updating an existing alias replaces its secret ---------------------

def test_update_existing_alias_replaces_secret(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, "FAKE_GEMINI_KEY_02_ROTATED")
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_02")
    assert store["gemini_02"] == "FAKE_GEMINI_KEY_02_ROTATED"
    assert json.loads(store[MANIFEST]).count("gemini_02") == 1


# 8. list-keys shows names only -----------------------------------------

def test_list_keys_shows_names_only(store, enter_key, capsys):
    calls, feed = enter_key
    feed(FAKE_PRIMARY, FAKE_02)
    _run("set-key", "gemini")
    _run("set-key", "gemini", "--name", "gemini_02")
    capsys.readouterr()  # clear prior output
    assert _run("list-keys") == 0
    out = capsys.readouterr().out
    assert PRIMARY in out and "gemini_02" in out
    assert FAKE_PRIMARY not in out           # never show values
    assert FAKE_02 not in out


# 9. remove-key ---------------------------------------------------------

def test_remove_key_deletes_secret_and_manifest_entry(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02)
    _run("set-key", "gemini", "--name", "gemini_02")
    assert "gemini_02" in store
    assert _run("remove-key", "gemini_02") == 0
    assert "gemini_02" not in store
    # Only primary remains -> manifest removed (pristine single-key state).
    assert MANIFEST not in store


def test_remove_key_keeps_other_additional(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_02, FAKE_03)
    _run("set-key", "gemini", "--name", "gemini_02")
    _run("set-key", "gemini", "--name", "gemini_03")
    assert _run("remove-key", "gemini_02") == 0
    assert "gemini_02" not in store and "gemini_03" in store
    assert json.loads(store[MANIFEST]) == [PRIMARY, "gemini_03"]


def test_remove_nonexistent_alias_is_clean(store):
    assert _run("remove-key", "ghost") == 1


# 10. Primary protection ------------------------------------------------

def test_primary_cannot_be_removed_via_remove_key(store, enter_key):
    calls, feed = enter_key
    feed(FAKE_PRIMARY)
    _run("set-key", "gemini")
    assert _run("remove-key", PRIMARY) == 1
    assert store[PRIMARY] == FAKE_PRIMARY   # still present


def test_remove_key_will_not_delete_stop_pin_or_manifest(store, enter_key):
    # Reserved/unrelated names are not "additional credentials" and are safe.
    store[secrets.STOP_PIN] = "FAKE_PIN"
    assert _run("remove-key", secrets.STOP_PIN) == 1
    assert store[secrets.STOP_PIN] == "FAKE_PIN"


# 11. Invalid aliases ---------------------------------------------------

@pytest.mark.parametrize("bad", [
    "", "   ", "bad name", "gemini/02", "gemini.02", "a b",
    PRIMARY, MANIFEST, "stop_pin",
])
def test_invalid_alias_rejected_before_prompting(store, enter_key, bad):
    calls, feed = enter_key
    assert _run("set-key", "gemini", "--name", bad) == 1
    assert bad not in store            # nothing stored
    assert MANIFEST not in store       # manifest untouched
    assert calls["getpass"] == 0       # never even prompted


# 12. Keyring failures --------------------------------------------------

def test_set_key_keyring_failure_is_clean(store, enter_key, monkeypatch, capsys):
    calls, feed = enter_key
    feed(FAKE_02)

    def boom(k, v):
        raise secrets.SecretStoreError("raw backend text should not surface")

    monkeypatch.setattr(secrets, "set_secret", boom)
    assert _run("set-key", "gemini", "--name", "gemini_02") == 1
    out = capsys.readouterr().out
    assert FAKE_02 not in out
    assert "raw backend text" not in out


# 13. Backward compatibility --------------------------------------------

def test_backward_compat_only_primary(store, enter_key, capsys):
    calls, feed = enter_key
    feed(FAKE_PRIMARY)
    _run("set-key", "gemini")
    capsys.readouterr()
    assert _run("list-keys") == 0
    out = capsys.readouterr().out
    assert PRIMARY in out
    assert "gemini_02" not in out
    assert MANIFEST not in store


# Secret non-disclosure on success paths --------------------------------

def test_set_key_success_output_has_no_value(store, enter_key, capsys):
    calls, feed = enter_key
    feed(FAKE_02)
    _run("set-key", "gemini", "--name", "gemini_02")
    out = capsys.readouterr().out
    assert FAKE_02 not in out
    assert "gemini_02" in out  # the alias name is fine to show


# --- approve / deny CLI wiring (+ clarify regression) -------------------
#
# void.cli.Assistant is monkeypatched to a factory that returns REAL
# Assistant instances (Assistant.__new__, so approve/deny/resume/clarify/
# _load_awaiting/_load_blocked_disambiguation/_agent are the actual,
# unmodified production methods) wired to a shared KillSwitch/TaskStore/
# ToolRegistry over tmp_path. Only Config.load(), the network ProviderRegistry,
# and the Windows app/computer-control tool construction are skipped - none
# of those are relevant to CLI wiring, RiskGate, or KillSwitch behavior.

from pathlib import Path as _Path

from void.actions.files import FileActions as _FileActions
from void.actions.registry import ToolRegistry as _ToolRegistry
from void.app import Assistant as _RealAssistant
from void.core.agent import Agent as _Agent
from void.core.kill_switch import KillSwitch as _KillSwitch
from void.core.task import Status as _Status
from void.core.task import Task as _Task
from void.core.task import TaskStore as _TaskStore
from void.providers.base import LLMResponse as _LLMResponse
from void.security.risk import RiskGate as _RiskGate

from tests.helpers import FakeProvider as _FakeProvider
from tests.helpers import tool_call as _tool_call


class _StubConfig:
    """Just enough of Config for Assistant._agent(): agent.max_steps/retries."""
    def get(self, key, default=None):
        return default


class _OneShotProviders:
    """Swappable single-provider registry: select() returns whatever provider
    is CURRENTLY assigned, mirroring a fresh CLI process picking up the
    provider live each invocation."""
    def __init__(self, provider):
        self.provider = provider

    def select(self):
        return self.provider


@pytest.fixture
def cli_rig(tmp_path, monkeypatch):
    files = _FileActions(allowed_roots=[tmp_path])
    tools = _ToolRegistry()
    tools.register_all(files.tools())
    ks = _KillSwitch()
    task_store = _TaskStore(tmp_path / "t.sqlite")
    providers = _OneShotProviders(_FakeProvider([]))

    def _factory(confirm_fn=None, on_event=None):
        a = _RealAssistant.__new__(_RealAssistant)
        a.config = _StubConfig()
        a.on_event = on_event or (lambda _m: None)
        a._confirm_fn = confirm_fn
        a.kill_switch = ks
        a.risk_gate = _RiskGate(confirm_at_or_above="high", confirm_fn=confirm_fn)
        a.store = task_store
        a.tools = tools
        a.providers = providers
        return a

    monkeypatch.setattr(cli, "Assistant", _factory)
    return {"tmp_path": tmp_path, "ks": ks, "store": task_store,
            "tools": tools, "providers": providers}


def _headless_agent(rig, script):
    """A headless (defer_confirmation=True) Agent sharing the rig's store,
    tools, and killswitch - seeds AWAITING_CONFIRMATION/BLOCKED tasks exactly
    as a real headless (e.g. voice) Assistant would, independent of the
    interactive provider slot the CLI factory itself uses."""
    gate = _RiskGate(confirm_at_or_above="high")
    return _Agent(_FakeProvider(script), rig["tools"], gate, rig["ks"],
                 rig["store"], max_retries=0, defer_confirmation=True)


def _set_next_cli_response(rig, script):
    rig["providers"].provider = _FakeProvider(script)


# 1. approve a valid AWAITING_CONFIRMATION task --------------------------

def test_cli_approve_valid_awaiting_task_executes_once(cli_rig):
    rig = cli_rig
    target = rig["tmp_path"] / "keep.txt"
    target.write_text("precious")
    seed = _headless_agent(rig, [
        _LLMResponse(tool_calls=[_tool_call("delete_file", path=str(target))]),
    ])
    seeded = seed.run("delete keep.txt")
    assert seeded.status == _Status.AWAITING_CONFIRMATION
    assert target.exists()

    _set_next_cli_response(rig, [_LLMResponse(text="Deleted it.")])
    rc = _run("approve", seeded.task.id)

    assert rc == 0
    assert not target.exists()
    final = rig["store"].load(seeded.task.id)
    assert final.status == _Status.COMPLETED
    assert final.pending is None


# 2. deny a valid AWAITING_CONFIRMATION task -----------------------------

def test_cli_deny_valid_awaiting_task_prevents_execution(cli_rig):
    rig = cli_rig
    target = rig["tmp_path"] / "keep.txt"
    target.write_text("precious")
    seed = _headless_agent(rig, [
        _LLMResponse(tool_calls=[_tool_call("delete_file", path=str(target))]),
    ])
    seeded = seed.run("delete keep.txt")
    assert seeded.status == _Status.AWAITING_CONFIRMATION

    _set_next_cli_response(rig, [_LLMResponse(text="Did not delete it.")])
    rc = _run("deny", seeded.task.id)

    assert rc == 0
    assert target.exists() and target.read_text() == "precious"
    final = rig["store"].load(seeded.task.id)
    assert final.pending is None


# 3 / 4. invalid / nonexistent task id -----------------------------------

def test_cli_approve_nonexistent_task_id_fails_safely(cli_rig, capsys):
    rc = _run("approve", "no-such-task")
    assert rc == 1
    assert "no such task" in capsys.readouterr().out.lower()


def test_cli_deny_nonexistent_task_id_fails_safely(cli_rig, capsys):
    rc = _run("deny", "no-such-task")
    assert rc == 1
    assert "no such task" in capsys.readouterr().out.lower()


# 5. approve a terminal task (with a stale pending) does not execute ----

def test_cli_approve_terminal_task_does_not_execute(cli_rig, capsys):
    rig = cli_rig
    target = rig["tmp_path"] / "keep.txt"
    target.write_text("precious")
    task = _Task(goal="delete keep.txt", status=_Status.CANCELLED)
    task.pending = {
        "assistant_text": None,
        "tool_calls": [{"name": "delete_file", "arguments": {"path": str(target)},
                        "id": None, "signature": None,
                        "risk": "HIGH", "requires_confirmation": True}],
    }
    rig["store"].save(task)

    rc = _run("approve", task.id)

    assert rc == 1
    assert target.exists() and target.read_text() == "precious"
    final = rig["store"].load(task.id)
    assert final.status == _Status.CANCELLED
    assert "no pending confirmation" in capsys.readouterr().out.lower()


# 6. deny a terminal task does not mutate it -----------------------------

def test_cli_deny_terminal_task_does_not_mutate_it(cli_rig):
    rig = cli_rig
    task = _Task(goal="anything", status=_Status.COMPLETED, result="done already")
    rig["store"].save(task)
    before = rig["store"].load(task.id)

    rc = _run("deny", task.id)

    assert rc == 1
    after = rig["store"].load(task.id)
    assert after.status == before.status == _Status.COMPLETED
    assert after.result == before.result
    assert after.updated_at == before.updated_at   # not even re-saved/touched


# 7. approval while KillSwitch is engaged does not execute --------------

def test_cli_approve_with_killswitch_engaged_does_not_execute(cli_rig):
    rig = cli_rig
    target = rig["tmp_path"] / "keep.txt"
    target.write_text("precious")
    seed = _headless_agent(rig, [
        _LLMResponse(tool_calls=[_tool_call("delete_file", path=str(target))]),
    ])
    seeded = seed.run("delete keep.txt")
    assert seeded.status == _Status.AWAITING_CONFIRMATION

    rig["ks"].engage(reason="test: engaged before approval")

    rc = _run("approve", seeded.task.id)

    assert target.exists() and target.read_text() == "precious"
    final = rig["store"].load(seeded.task.id)
    assert final.status == _Status.PAUSED
    assert rc == 2                                  # not COMPLETED


# 8. existing clarify behavior remains intact ----------------------------

def test_cli_clarify_still_works_after_approve_deny_wiring(cli_rig):
    rig = cli_rig
    a = rig["tmp_path"] / "aa" / "Projects"
    b = rig["tmp_path"] / "bb" / "Projects"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    seed = _Agent(_FakeProvider([
        _LLMResponse(tool_calls=[_tool_call("find_directory", query="Projects")]),
    ]), rig["tools"], _RiskGate(confirm_at_or_above="high"), rig["ks"],
        rig["store"], max_retries=0)
    blocked = seed.run("create note.txt in Projects")
    assert blocked.status == _Status.BLOCKED

    pending = rig["store"].load(blocked.task.id).pending
    chosen = pending["candidates"][0]
    chosen_dir = _Path(chosen["path"])

    _set_next_cli_response(rig, [
        _LLMResponse(tool_calls=[_tool_call(
            "write_file", path=str(chosen_dir / "note.txt"), content="hi")]),
        _LLMResponse(text="done"),
    ])
    rc = _run("clarify", blocked.task.id, str(chosen["index"]))

    assert rc == 0
    assert (chosen_dir / "note.txt").read_text() == "hi"
