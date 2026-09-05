"""Phase 5B: atomic checkpoints, mid-step interruption, durable confirmation,
and resume safety. Deterministic - FakeProvider only, no Gemini, no keys."""
import json

import pytest

from void.actions.base import Tool, ToolResult
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import Status, TaskStore
from void.providers.base import LLMProvider, LLMResponse
from void.security.risk import RiskGate, RiskLevel

from tests.helpers import FakeProvider, tool_call


def _trip_tool(ks):
    """A LOW-risk tool whose execution engages the kill switch (test hook)."""
    def handler():
        ks.engage(reason="test trip")
        return ToolResult.success("tripped")
    return Tool(name="trip", description="engage stop (test)",
                parameters={"type": "object", "properties": {}},
                handler=handler, risk=RiskLevel.LOW)


class _CountingRiskGate(RiskGate):
    """RiskGate that records every authorize() call (to prove no bypass)."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.authorize_calls = []

    def authorize(self, level, description, owner_decision=None):
        self.authorize_calls.append((level, description, owner_decision))
        return super().authorize(level, description, owner_decision=owner_decision)


def _build(tmp_path, provider, *, ks=None, confirm_fn=None, defer=False,
           max_steps=12, max_retries=0, extra=None, gate=None):
    files = FileActions(allowed_roots=[tmp_path])
    tools = ToolRegistry()
    tools.register_all(files.tools())
    if extra:
        tools.register_all(extra)
    ks = ks or KillSwitch()
    gate = gate or RiskGate(confirm_at_or_above="high", confirm_fn=confirm_fn)
    agent = Agent(provider, tools, gate, ks, TaskStore(tmp_path / "t.sqlite"),
                  max_steps=max_steps, max_retries=max_retries,
                  defer_confirmation=defer)
    return agent, ks


def _assistant_calls(msgs):
    return [m for m in msgs if m["role"] == "assistant" and m.get("tool_calls")]


def _tool_msgs(msgs):
    return [m for m in msgs if m["role"] == "tool"]


def _no_dangling(task):
    """Every function call in history has a matching tool response."""
    calls = sum(len(m["tool_calls"]) for m in _assistant_calls(task.messages))
    responses = len(_tool_msgs(task.messages))
    return calls == responses


# --- C/D/I: atomic checkpoint + mid-step interruption -------------------

def test_interrupt_after_first_tool_is_consistent(tmp_path):
    # Step: [trip, write]. trip engages kill switch; write must be cancelled,
    # not executed, and the checkpoint must be matched (no dangling).
    target = tmp_path / "should_not_exist.txt"
    ks = KillSwitch()
    provider = FakeProvider([LLMResponse(tool_calls=[
        tool_call("trip"),
        tool_call("write_file", path=str(target), content="nope"),
    ])])
    agent, ks = _build(tmp_path, provider, ks=ks, extra=[_trip_tool(ks)])
    result = agent.run("do two things")

    assert result.status == Status.PAUSED
    assert not target.exists()                 # write was cancelled, not run
    task = agent.store.load(result.task.id)
    assert _no_dangling(task)                  # matched exchange
    tmsgs = _tool_msgs(task.messages)
    assert "tripped" in tmsgs[0]["content"]
    assert "did not run" in tmsgs[1]["content"]  # honest cancellation, not fake


def test_interrupt_before_any_tool_in_step_cancels_all(tmp_path):
    # Provider engages the kill switch during generate; both tools cancelled.
    target = tmp_path / "nope.txt"
    ks = KillSwitch()

    class KsEngaging(LLMProvider):
        name = "fake"
        def available(self): return True
        def generate(self, messages, tools=None):
            ks.engage(reason="during generate")
            return LLMResponse(tool_calls=[
                tool_call("write_file", path=str(target), content="x"),
                tool_call("read_file", path=str(target)),
            ])

    agent, ks = _build(tmp_path, KsEngaging(), ks=ks)
    result = agent.run("go")
    assert result.status == Status.PAUSED
    assert not target.exists()
    task = agent.store.load(result.task.id)
    assert _no_dangling(task)
    assert all("did not run" in m["content"] for m in _tool_msgs(task.messages))


def test_interrupt_after_all_tools_commits_step(tmp_path):
    # Step: [write(new), trip]. Both run; step commits fully; loop then pauses.
    target = tmp_path / "created.txt"
    ks = KillSwitch()
    provider = FakeProvider([LLMResponse(tool_calls=[
        tool_call("write_file", path=str(target), content="hello"),
        tool_call("trip"),
    ])])
    agent, ks = _build(tmp_path, provider, ks=ks, extra=[_trip_tool(ks)])
    result = agent.run("go")
    assert result.status == Status.PAUSED
    assert target.exists() and target.read_text() == "hello"  # write committed
    task = agent.store.load(result.task.id)
    assert _no_dangling(task)
    assert not any("did not run" in m["content"] for m in _tool_msgs(task.messages))


def test_provider_failure_during_step_no_dangling(tmp_path):
    class Boom(LLMProvider):
        name = "fake"
        def available(self): return True
        def generate(self, messages, tools=None):
            raise RuntimeError("transient boom")

    agent, ks = _build(tmp_path, Boom(), max_retries=0)
    result = agent.run("go")
    assert result.status == Status.FAILED
    task = agent.store.load(result.task.id)
    assert _no_dangling(task)                  # no assistant tool_calls appended
    assert _assistant_calls(task.messages) == []


def test_ordinary_successful_step(tmp_path):
    (tmp_path / "note.txt").write_text("hi")
    provider = FakeProvider([
        LLMResponse(tool_calls=[tool_call("search_files", query="note")]),
        LLMResponse(text="found it"),
    ])
    agent, ks = _build(tmp_path, provider)
    result = agent.run("find note")
    assert result.status == Status.COMPLETED
    assert _no_dangling(agent.store.load(result.task.id))


# --- Resume safety: no duplicate side effects --------------------------

def test_resume_does_not_reexecute_committed_side_effect(tmp_path):
    target = tmp_path / "created.txt"
    ks = KillSwitch()
    provider = FakeProvider([
        LLMResponse(tool_calls=[tool_call("write_file", path=str(target),
                                          content="v1"), tool_call("trip")]),
        LLMResponse(text="all done"),   # served on resume
    ])
    agent, ks = _build(tmp_path, provider, ks=ks, extra=[_trip_tool(ks)])
    r1 = agent.run("go")
    assert r1.status == Status.PAUSED and target.read_text() == "v1"
    ks.reset()
    task = agent.store.load(r1.task.id)
    r2 = agent.resume(task)
    assert r2.status == Status.COMPLETED
    assert target.read_text() == "v1"          # NOT rewritten on resume


# --- E/F: durable confirmation -----------------------------------------

def _delete_setup(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("precious")
    provider = FakeProvider([
        LLMResponse(tool_calls=[tool_call("delete_file", path=str(target))]),
        LLMResponse(text="finished"),   # served after approve/deny continues
    ])
    return target, provider


def test_highrisk_defers_to_awaiting_confirmation(tmp_path):
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, defer=True)  # headless
    result = agent.run("delete keep.txt")
    assert result.status == Status.AWAITING_CONFIRMATION
    assert target.exists()                     # NOTHING executed
    task = agent.store.load(result.task.id)
    assert task.pending is not None
    call = task.pending["tool_calls"][0]
    assert call["name"] == "delete_file" and call["requires_confirmation"] is True
    assert _assistant_calls(task.messages) == []   # no dangling proposal committed
    assert task.steps == 0


def test_awaiting_survives_reload_and_plain_resume(tmp_path):
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, defer=True)
    r = agent.run("delete keep.txt")
    reloaded = agent.store.load(r.task.id)      # simulate process restart
    assert reloaded.status == Status.AWAITING_CONFIRMATION and reloaded.pending
    r2 = agent.resume(reloaded)                 # plain resume, NO approval
    assert r2.status == Status.AWAITING_CONFIRMATION
    assert target.exists()                      # still not executed


def test_approval_executes_once_then_continues(tmp_path):
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, defer=True)
    r = agent.run("delete keep.txt")
    task = agent.store.load(r.task.id)
    r2 = agent.resume_pending(task, decision=True)   # owner approves
    assert r2.status == Status.COMPLETED
    assert not target.exists()                  # deleted exactly once
    final = agent.store.load(r.task.id)
    assert final.pending is None
    assert _no_dangling(final)


def test_denial_does_not_execute(tmp_path):
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, defer=True)
    r = agent.run("delete keep.txt")
    task = agent.store.load(r.task.id)
    r2 = agent.resume_pending(task, decision=False)  # owner denies
    assert r2.status == Status.COMPLETED
    assert target.exists()                      # NOT deleted
    tmsgs = _tool_msgs(agent.store.load(r.task.id).messages)
    assert any("not authorized" in m["content"] for m in tmsgs)


def test_headless_highrisk_never_auto_executes(tmp_path):
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, defer=True)
    r = agent.run("delete keep.txt")
    # Without an explicit approval, the file must remain.
    assert r.status == Status.AWAITING_CONFIRMATION and target.exists()


def test_interactive_high_risk_synchronous_deny_still_works(tmp_path):
    # Non-defer (confirmer present) preserves the original skip-and-continue.
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, confirm_fn=lambda d: False, defer=False)
    r = agent.run("delete keep.txt")
    assert r.status == Status.COMPLETED and target.exists()


# --- J: every execution passes through RiskGate ------------------------

def test_all_executions_go_through_riskgate(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    gate = _CountingRiskGate(confirm_at_or_above="high")
    provider = FakeProvider([
        LLMResponse(tool_calls=[tool_call("search_files", query="a"),
                                tool_call("read_file", path=str(tmp_path / "a.txt"))]),
        LLMResponse(text="done"),
    ])
    agent, ks = _build(tmp_path, provider, gate=gate)
    agent.run("go")
    described = [d for (_lvl, d, _od) in gate.authorize_calls]
    assert any("search_files" in d for d in described)
    assert any("read_file" in d for d in described)


def test_approved_execution_passes_owner_decision_through_gate(tmp_path):
    target, provider = _delete_setup(tmp_path)
    gate = _CountingRiskGate(confirm_at_or_above="high")
    agent, ks = _build(tmp_path, provider, defer=True, gate=gate)
    task = agent.store.load(agent.run("delete").task.id)
    agent.resume_pending(task, decision=True)
    # The delete authorize call carried owner_decision=True (durable approval).
    delete_calls = [od for (_l, d, od) in gate.authorize_calls if "delete_file" in d]
    assert delete_calls and delete_calls[-1] is True


# --- K/L: policy + secret isolation ------------------------------------

def test_agent_has_no_policy_tool(tmp_path):
    files = FileActions(allowed_roots=[tmp_path])
    from void.actions.apps import AppActions
    names = {t.name for t in files.tools()} | {t.name for t in AppActions(files).tools()}
    assert not (names & {"add_root", "remove_root", "add_protected",
                         "remove_protected", "roots", "protect", "approve"})


def test_pending_persists_no_secrets(tmp_path):
    target, provider = _delete_setup(tmp_path)
    agent, ks = _build(tmp_path, provider, defer=True)
    task = agent.store.load(agent.run("delete").task.id)
    blob = json.dumps(task.pending).lower()
    assert "api_key" not in blob and "aiza" not in blob and "secret" not in blob
    # Only tool name/args/risk metadata is present.
    call = task.pending["tool_calls"][0]
    assert set(call) == {"name", "arguments", "id", "signature",
                         "risk", "requires_confirmation"}
