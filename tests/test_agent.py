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


# --- folder resolution (agent integration) -----------------------------

def test_system_prompt_has_folder_resolution_policy():
    # The behavioral change is prompt-driven; assert the policy text is present.
    from void.core.agent import SYSTEM_PROMPT
    p = SYSTEM_PROMPT.lower()
    assert "list_directory" in p                 # directory discovery tool named
    assert "search_files" in p                   # distinguished from file search
    assert "invent" in p                         # do not invent paths
    assert "ask" in p                            # clarification path exists


def test_A_resolves_existing_directory_then_writes(tmp_path):
    # User asks to create a file in an EXISTING directory. The agent discovers
    # the folder with list_directory, then writes to the path it returned.
    projects = tmp_path / "Projects"
    projects.mkdir()
    target = projects / "Testcase.txt"
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("list_directory")]),
        LLMResponse(tool_calls=[tool_call(
            "write_file", path=str(target), content="hi")]),
        LLMResponse(text="Created Testcase.txt in the Projects folder."),
    ])
    result = agent.run("Create Testcase.txt in the Projects folder")
    assert result.status == Status.COMPLETED
    assert target.exists() and target.read_text() == "hi"
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    # list_directory actually ran and surfaced the Projects directory.
    assert any(m["name"] == "list_directory" and "Projects" in m["content"]
               for m in tool_msgs)


def test_B_missing_directory_agent_can_ask_without_inventing_path(tmp_path):
    # The Projects folder does NOT exist. The agent lists, sees it is absent,
    # and asks for clarification instead of inventing a path or writing blindly.
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("list_directory")]),
        LLMResponse(text="I don't see a 'Projects' folder in your workspace. "
                         "Should I create it?"),
    ])
    result = agent.run("Create Testcase.txt in the Projects folder")
    assert result.status == Status.COMPLETED
    # Nothing was created: no Projects dir, no Testcase.txt anywhere.
    assert not (tmp_path / "Projects").exists()
    assert list(tmp_path.rglob("Testcase.txt")) == []
    # No write_file was ever executed.
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert all(m["name"] != "write_file" for m in tool_msgs)
    assert "create it" in (result.result or "").lower()


def test_C_ambiguous_request_can_stop_and_ask(tmp_path):
    # An ambiguous request: the agent stops and asks rather than broadening
    # searches. A text-only turn ends the task cleanly in one step.
    agent = build_agent(tmp_path, [
        LLMResponse(text="Which folder do you mean by 'the project folder'? "
                         "I can list your workspace if that helps."),
    ])
    result = agent.run("put this in the project folder")
    assert result.status == Status.COMPLETED
    assert result.steps == 1
    # No tools were called at all - no search flailing.
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert tool_msgs == []
    assert "which folder" in (result.result or "").lower()


def test_D_search_files_behavior_not_regressed(tmp_path):
    # Existing search_files flow still works and still returns files.
    (tmp_path / "cybersecurity_notes.md").write_text("x")
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("search_files", query="cyber")]),
        LLMResponse(text="Found your cybersecurity notes."),
    ])
    result = agent.run("find my cybersecurity notes")
    assert result.status == Status.COMPLETED
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert any(m["name"] == "search_files"
               and "cybersecurity_notes.md" in m["content"] for m in tool_msgs)


# --- find_directory agent integration (Phase 6) ------------------------

def test_find_directory_is_registered(tmp_path):
    agent = build_agent(tmp_path, [LLMResponse(text="hi")])
    assert "find_directory" in agent.tools.names()
    # No policy-mutating tools are exposed to the agent.
    assert not (set(agent.tools.names()) & {
        "add_root", "remove_root", "add_protected", "remove_protected",
        "roots", "protect"})


def test_find_directory_output_is_untrusted(tmp_path):
    (tmp_path / "Hackathon").mkdir()
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Hackathon")]),
        LLMResponse(text="found it"),
    ])
    result = agent.run("find the hackathon folder")
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"].startswith(
        "[UNTRUSTED TOOL OUTPUT")
    assert "Hackathon" in tool_msgs[0]["content"]


def test_find_directory_unique_then_write(tmp_path):
    hack = tmp_path / "a" / "Hackathon"
    hack.mkdir(parents=True)
    target = hack / "notes.txt"
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Hackathon")]),
        LLMResponse(tool_calls=[tool_call(
            "write_file", path=str(target), content="hello")]),
        LLMResponse(text="created notes.txt in Hackathon"),
    ])
    result = agent.run("open Hackathon and create notes.txt")
    assert result.status == Status.COMPLETED
    assert target.exists() and target.read_text() == "hello"


def test_find_directory_multiple_flagged_ambiguous_no_autoselect(tmp_path):
    (tmp_path / "x" / "Data").mkdir(parents=True)
    (tmp_path / "y" / "Data").mkdir(parents=True)
    # The model only searches, then asks (no write/open) - deterministic script.
    agent = build_agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Data")]),
        LLMResponse(text="I found two 'Data' folders - which one do you mean?"),
    ])
    result = agent.run("open the Data folder")
    assert result.status == Status.COMPLETED
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert "ambiguous" in tool_msgs[0]["content"].lower()
    # No file was created/opened automatically.
    assert not any(m["role"] == "tool" and m["name"] == "write_file"
                   for m in result.task.messages)


def test_E_resolution_flow_preserves_high_risk_gate(tmp_path):
    # Discovering a folder must NOT bypass the HIGH-risk confirmation gate:
    # overwriting an existing file in a resolved folder is still refused.
    projects = tmp_path / "Projects"
    projects.mkdir()
    existing = projects / "keep.txt"
    existing.write_text("original")
    agent = build_agent(
        tmp_path,
        [LLMResponse(tool_calls=[tool_call("list_directory")]),
         LLMResponse(tool_calls=[tool_call(
             "write_file", path=str(existing), content="HACKED", overwrite=True)]),
         LLMResponse(text="I did not modify it.")],
        confirm_fn=lambda desc: False,  # owner refuses the HIGH-risk overwrite
    )
    result = agent.run("overwrite keep.txt in the Projects folder")
    assert existing.read_text() == "original"  # unchanged
    tool_msgs = [m for m in result.task.messages if m["role"] == "tool"]
    assert any("not authorized" in m["content"] for m in tool_msgs)
