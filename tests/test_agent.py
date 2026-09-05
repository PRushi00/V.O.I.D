"""Integration tests for the agent loop: tool execution, checkpointing,
the risk gate, the kill switch, and the safety cap."""
import pytest

from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import Status, TaskStore
from void.providers.base import LLMResponse
from void.security.risk import RiskGate, RiskLevel

from tests.helpers import FakeProvider, tool_call


def build_agent(tmp_path, script, confirm_fn=None, max_steps=12):
    files = FileActions(allowed_roots=[tmp_path], delete_to_recycle_bin=True)
    tools = ToolRegistry()
    tools.register_all(files.tools())
    return Agent(
        provider=FakeProvider(script),
        tools=tools,
        risk_gate=RiskGate(confirm_at_or_above="high", confirm_fn=confirm_fn),
        kill_switch=KillSwitch(),
        store=TaskStore(tmp_path / "tasks.sqlite"),
        max_steps=max_steps,
    )


def test_completes_and_runs_tool(tmp_path):
    (tmp_path / "cybersecurity_notes.md").write_text("x")
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("search_files", query="cyber")]),
        LLMResponse(text="I found your cybersecurity notes."),
    ])
    result = agent.run("find my cybersecurity notes")
    assert result.status == Status.COMPLETED
    assert "cybersecurity" in result.result
    assert result.steps == 2
    # The tool result was fed back into the conversation.
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert tool_msgs and "cybersecurity_notes.md" in tool_msgs[0]["content"]


def test_checkpoint_persisted(tmp_path):
    agent = build_agent(tmp_path, [LLMResponse(text="hi")])
    result = agent.run("say hi")
    reloaded = agent.store.load(result.task.id)
    assert reloaded is not None
    assert reloaded.status == Status.COMPLETED
    assert reloaded.result == "hi"


def test_high_risk_delete_denied(tmp_path):
    target = tmp_path / "important.txt"
    target.write_text("keep me")
    agent = build_agent(
        tmp_path,
        [LLMResponse(tool_calls=[tool_call("delete_file", path=str(target))]),
         LLMResponse(text="I did not delete it.")],
        confirm_fn=lambda desc: False,  # owner refuses
    )
    result = agent.run(f"delete {target}")
    assert target.exists()  # NOT deleted
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert any("not authorized" in m["content"] for m in tool_msgs)


def test_high_risk_delete_allowed_when_confirmed(tmp_path):
    target = tmp_path / "trash.txt"
    target.write_text("bye")
    agent = build_agent(
        tmp_path,
        [LLMResponse(tool_calls=[tool_call("delete_file", path=str(target))]),
         LLMResponse(text="Deleted.")],
        confirm_fn=lambda desc: True,  # owner approves
    )
    result = agent.run(f"delete {target}")
    assert not target.exists()
    assert result.status == Status.COMPLETED


def test_overwrite_existing_file_requires_confirmation(tmp_path):
    # Decision 2: modifying an EXISTING file must be gated (HIGH) and is
    # refused when the owner declines.
    target = tmp_path / "keep.txt"
    target.write_text("original")
    agent = build_agent(
        tmp_path,
        [LLMResponse(tool_calls=[tool_call(
            "write_file", path=str(target), content="HACKED", overwrite=True)]),
         LLMResponse(text="Did not modify it.")],
        confirm_fn=lambda desc: False,  # owner refuses
    )
    result = agent.run(f"overwrite {target}")
    assert target.read_text() == "original"  # unchanged
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert any("not authorized" in m["content"] for m in tool_msgs)


def test_new_file_write_is_autonomous(tmp_path):
    # Creating a NEW file is MEDIUM -> allowed without confirmation.
    target = tmp_path / "fresh.txt"
    agent = build_agent(
        tmp_path,
        [LLMResponse(tool_calls=[tool_call(
            "write_file", path=str(target), content="hello")]),
         LLMResponse(text="Created it.")],
        confirm_fn=lambda desc: False,  # even if owner would refuse HIGH
    )
    result = agent.run(f"create {target}")
    assert target.exists() and target.read_text() == "hello"
    assert result.status == Status.COMPLETED


def test_kill_switch_pauses_before_running(tmp_path):
    agent = build_agent(tmp_path, [LLMResponse(text="should not reach")])
    agent.kill_switch.engage(reason="test stop")
    result = agent.run("do something")
    assert result.status == Status.PAUSED
    assert result.steps == 0


def test_max_steps_cap(tmp_path):
    # Provider never finishes - always asks for a tool.
    endless = [LLMResponse(tool_calls=[tool_call("search_files", query="x")])
               for _ in range(10)]
    agent = build_agent(tmp_path, endless, max_steps=3)
    result = agent.run("loop forever")
    assert result.status == Status.FAILED
    assert result.steps == 3
    assert "max_steps" in (result.task.error or "")
