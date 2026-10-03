"""Cross-domain workflows: the capabilities combining into answers no single tool gives.

V2's eight domains are useful separately, but the point of building them on one capability layer is that the
agent can chain them. These tests drive the three workflows the V2 scope names, end to end, through the real
``Agent`` loop with a scripted model standing in for the reasoning:

    "Find the project I worked on yesterday and open it"
        memory -> filesystem search -> application resolution -> launch
    "Why is my laptop slow?"
        system telemetry -> processes -> analysis -> answer
    "Is my headset connected?"
        device discovery -> connection status -> answer

Two things are asserted every time. First that the chain actually produces the answer - a workflow that
needs three tools must really run three tools, against real probes where the probe is local. Second, and more
important, that chaining changes no boundary: each step is authorized on its own, a HIGH-risk step in the
middle still stops for the owner, the kill switch still ends the chain mid-way, and a tool that refuses does
not get retried into succeeding. A chain is not a credential either.
"""
import json

import re

import pytest

from tests.helpers import FakeProvider, tool_call
from tests.test_memory_integration import make_assistant
from void.core.kill_switch import StopRequested
from void.core.task import Status
from void.providers.base import LLMResponse, ToolCall
from void.providers.registry import ProviderRegistry


def _script(assistant, *responses):
    provider = FakeProvider(list(responses))
    assistant.providers = ProviderRegistry({"fake": provider}, ["fake"])
    return provider


def _recording(assistant):
    """Record every tool that actually executes, in order."""
    executed = []
    real = assistant.tools.execute
    assistant.tools.execute = lambda name, args: (executed.append(name), real(name, args))[1]
    return executed


def _call(tool, /, **arguments):
    """One scripted tool call.

    Builds the ToolCall directly rather than going through ``tests.helpers.tool_call``, whose first
    parameter is also called ``name`` - and ``find_device`` takes an argument of that name, so the two
    collide. Changing the shared helper for this would touch every test that uses it.
    """
    return LLMResponse(tool_calls=[ToolCall(name=tool, arguments=dict(arguments))])


# --- "why is my laptop slow?" -----------------------------------------------------------------------

def test_why_is_my_laptop_slow_collects_real_observations_and_answers(tmp_path):
    """Domain 3 end to end, against this actual machine: telemetry -> analysis -> answer."""
    a = make_assistant(tmp_path)
    executed = _recording(a)
    _script(a, _call("diagnose_slowness"), LLMResponse(text="Your machine looks healthy."))
    result = a.run("why is my laptop slow?")
    assert result.status is Status.COMPLETED, result.status
    assert executed == ["diagnose_slowness"]


def test_the_slowness_answer_is_built_from_measurements_not_invented(tmp_path):
    """The model is handed what was actually measured; nothing is fabricated upstream of it.

    Asserts that a MEASUREMENT reached the model, which is what the title claims, rather than that
    this particular machine is currently under strain. The previous version looked for one of
    "overloaded", "busy" or "nearly full", and that was testing the host's mood instead of the
    plumbing: ``host.pressure`` also emits notes containing none of those words - "Drive C: is 92%
    full", "The battery is low at 15%", and "Busiest right now: ..." (which contains "Busi", not
    "busy"). Any of those makes ``notes`` non-empty, which skips the "nothing looks overloaded"
    fallback the assertion depended on.

    It therefore passed on an idle machine and failed on a loaded one - reproducibly, inside a full
    regression run, where pytest itself pushes individual processes over the 10% threshold. Every
    branch does report a figure with a unit, so that is what is checked.
    """
    a = make_assistant(tmp_path)
    provider = _script(a, _call("diagnose_slowness"), LLMResponse(text="ok"))
    a.run("why is my laptop slow?")
    # The tool's own result reaches the model as a tool message.
    tool_messages = [m for turn in provider.seen_messages for m in turn if m.get("role") == "tool"]
    assert tool_messages, "the model was never shown the measurement"
    blob = " ".join(m.get("content") or "" for m in tool_messages)
    assert re.search(r"\d+(\.\d+)?\s?(%|GB)", blob), f"no measured figure reached the model: {blob[:200]}"


def test_a_deeper_investigation_can_chain_processes_after_the_diagnosis(tmp_path):
    """The chain the owner actually wants when something IS wrong: diagnose, then name the culprits."""
    a = make_assistant(tmp_path)
    executed = _recording(a)
    _script(a,
            _call("diagnose_slowness"),
            _call("list_processes", order="memory", limit=5),
            LLMResponse(text="Memory is tight and these are the heaviest programs."))
    result = a.run("why is my laptop slow, and what is using the most memory?")
    assert result.status is Status.COMPLETED
    assert executed == ["diagnose_slowness", "list_processes"]


def test_a_system_question_needs_no_confirmation(tmp_path):
    """Reads are LOW, so the owner is not interrupted to be told their own CPU load."""
    asked = []
    a = make_assistant(tmp_path, confirm_fn=lambda description: asked.append(description) or True)
    _script(a, _call("get_system_status"), LLMResponse(text="ok"))
    assert a.run("how is my computer doing?").status is Status.COMPLETED
    assert asked == [], f"a read-only observation asked the owner: {asked}"


# --- "is my headset connected?" ---------------------------------------------------------------------

def test_is_my_headset_connected_answers_from_real_device_discovery(tmp_path):
    """Domain 6 end to end, against this machine's actual device list."""
    a = make_assistant(tmp_path)
    executed = _recording(a)
    provider = _script(a, _call("find_device", name="headset"), LLMResponse(text="Here is what I found."))
    result = a.run("is my headset connected?")
    assert result.status is Status.COMPLETED
    assert executed == ["find_device"]
    tool_messages = [m for turn in provider.seen_messages for m in turn if m.get("role") == "tool"]
    blob = " ".join(m.get("content") or "" for m in tool_messages)
    # Either answer is correct depending on what is plugged in; what must not happen is an empty one.
    assert ("attached" in blob or "cannot see anything matching" in blob or "of that kind" in blob), blob


def test_a_device_question_about_something_absent_gets_a_definite_no(tmp_path):
    a = make_assistant(tmp_path)
    provider = _script(a, _call("find_device", name="Logitech Brio 4K"), LLMResponse(text="Not here."))
    a.run("is my Logitech Brio connected?")
    tool_messages = [m for turn in provider.seen_messages for m in turn if m.get("role") == "tool"]
    blob = " ".join(m.get("content") or "" for m in tool_messages)
    assert "cannot see anything matching" in blob, blob[:200]


def test_a_device_question_can_chain_into_the_audio_inventory(tmp_path):
    a = make_assistant(tmp_path)
    executed = _recording(a)
    _script(a, _call("find_device", name="headset"), _call("list_devices", kind="audio"),
            LLMResponse(text="These are your audio devices."))
    assert a.run("is my headset connected, and what else could I use?").status is Status.COMPLETED
    assert executed == ["find_device", "list_devices"]


# --- "find the project I worked on yesterday and open it" --------------------------------------------

def test_memory_then_filesystem_then_launch(tmp_path):
    """Domains 8 -> 2 -> 1 in one request, which is the workflow the V2 scope names.

    The model is scripted, but every step is real: the memory is really stored and retrieved, the folder is
    really found under the allowed roots, and the launch really goes through the application layer.
    """
    a = make_assistant(tmp_path)
    project = tmp_path / "work" / "aurora"
    project.mkdir(parents=True)
    (project / "main.py").write_text("print('hi')", encoding="utf-8")
    a.memory.remember("yesterday I was working on the aurora project", channel="cli")

    executed = _recording(a)
    _script(a,
            _call("find_directory", query="aurora"),
            _call("open_path", target=str(project)),
            LLMResponse(text="Opened the aurora project."))
    result = a.run("find the project I worked on yesterday and open it")
    assert result.status is Status.COMPLETED, result.status
    assert executed == ["find_directory", "open_path"]


def test_the_memory_is_actually_retrieved_for_that_question(tmp_path):
    """The first link in the chain: the model must be shown what the owner told V.O.I.D earlier."""
    a = make_assistant(tmp_path)
    a.memory.remember("yesterday I was working on the aurora project", channel="cli")
    provider = _script(a, LLMResponse(text="The aurora project."))
    a.run("which project was I working on yesterday?")
    context = " ".join(m.get("content") or ""
                       for turn in provider.seen_messages for m in turn)
    assert "aurora" in context.lower(), "the stored memory never reached the model"


def test_a_project_that_was_never_mentioned_is_not_invented(tmp_path):
    """Nothing in the chain may substitute a guess for a retrieval."""
    a = make_assistant(tmp_path)
    provider = _script(a, LLMResponse(text="I do not have that stored."))
    a.run("which project was I working on yesterday?")
    context = " ".join(m.get("content") or ""
                       for turn in provider.seen_messages for m in turn).lower()
    assert "retrieved memory" not in context or "aurora" not in context


def test_the_filesystem_step_stays_inside_the_allowed_roots(tmp_path):
    """A chain starting in memory must not end up reaching outside confinement."""
    a = make_assistant(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    a.memory.remember(f"my real work lives in {outside}", channel="cli")
    executed = _recording(a)
    _script(a, _call("read_file", path=str(outside / "secret.txt")), LLMResponse(text="done"))
    a.run("open the project my memory points at")
    out = a.tools.execute("read_file", {"path": str(outside / "secret.txt")})
    assert out.ok is False, "a memory-sourced path escaped the allowed roots"


# --- chaining grants nothing ------------------------------------------------------------------------

def test_a_high_risk_step_in_the_middle_of_a_chain_still_stops_for_the_owner(tmp_path):
    """The claim: being step three of a workflow the owner started is not an authorization."""
    a = make_assistant(tmp_path)
    victim = tmp_path / "work" / "old.txt"
    victim.write_text("keep", encoding="utf-8")
    executed = _recording(a)
    _script(a,
            _call("get_system_status"),
            _call("list_processes"),
            _call("delete_file", path=str(victim)),
            LLMResponse(text="cleaned up"))
    result = a.run("check my system, then clean up old.txt")
    assert result.status is Status.AWAITING_CONFIRMATION, result.status
    assert victim.exists(), "a destructive step rode along on a chain of reads"
    assert executed == ["get_system_status", "list_processes"]


def test_the_kill_switch_ends_a_chain_part_way_through(tmp_path):
    a = make_assistant(tmp_path)
    executed = _recording(a)

    class StopsAfterFirst(FakeProvider):
        def generate(self, messages, tools=None):
            if self.calls == 1:
                a.kill_switch.engage("owner said stop")
            return super().generate(messages, tools)

    provider = StopsAfterFirst([_call("get_system_status"), _call("list_devices"),
                               LLMResponse(text="done")])
    a.providers = ProviderRegistry({"fake": provider}, ["fake"])
    result = a.run("check my system and my devices")
    assert result.status is Status.PAUSED, result.status
    assert executed == ["get_system_status"], f"work continued past the kill switch: {executed}"


def test_a_refused_step_does_not_get_retried_into_succeeding(tmp_path):
    """A tool that says no stays no, however many turns the model takes."""
    a = make_assistant(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "x.txt").write_text("private", encoding="utf-8")
    target = str(outside / "x.txt")
    executed = _recording(a)
    _script(a, _call("read_file", path=target), _call("read_file", path=target),
            _call("get_file_info", path=target), LLMResponse(text="gave up"))
    a.run("read x.txt from elsewhere")
    assert executed.count("read_file") >= 1
    for name in executed:
        assert name in ("read_file", "get_file_info")
    assert a.tools.execute("read_file", {"path": target}).ok is False


def test_a_chain_cannot_reach_the_camera_without_the_owner(tmp_path):
    """Domain 5's gate is not relaxed by being reached from a longer workflow."""
    a = make_assistant(tmp_path)
    executed = _recording(a)
    _script(a, _call("get_system_status"), _call("enable_camera"), _call("look"),
            LLMResponse(text="done"))
    result = a.run("check my system and then tell me what you can see")
    assert a.vision.gate.captures == 0, "a chain produced a camera frame"
    assert result.status in (Status.AWAITING_CONFIRMATION, Status.COMPLETED, Status.FAILED)
    assert "look" not in executed or a.vision.gate.captures == 0


def test_every_cross_domain_step_is_recorded_in_the_task_history(tmp_path):
    """Auditability of a chain: what ran must be reconstructable afterwards."""
    a = make_assistant(tmp_path)
    _script(a, _call("get_system_status"), _call("list_devices"), LLMResponse(text="done"))
    result = a.run("check my system and devices")
    stored = a.store.load(result.task.id)
    history = json.dumps(stored.messages)
    assert "get_system_status" in history and "list_devices" in history


def test_an_observation_chain_consults_no_model_beyond_its_own_turns(tmp_path):
    """Bounded work: the loop does not keep calling the model after it has an answer."""
    a = make_assistant(tmp_path)
    provider = _script(a, _call("get_network_status"), LLMResponse(text="You are online."))
    a.run("am I online?")
    assert provider.calls == 2, f"the loop made {provider.calls} model calls for a two-step answer"


# --- the funnel is the same for every domain ----------------------------------------------------------

def test_every_v2_capability_is_reachable_only_through_the_one_funnel(tmp_path):
    """There is exactly one path from a model's request to an action, for all eight domains.

    Asserted by routing one tool from each new domain through ``Agent.invoke_tool`` and checking they all
    come back in the same shape - and, for the camera, that the funnel's refusal is what stops it.
    """
    from void.core.agent import Agent

    class NoModel:
        name = "none"

        def available(self):
            return True

        def generate(self, messages, tools=None):
            raise AssertionError("a capability call must not consult a model")

    a = make_assistant(tmp_path)
    agent = Agent(provider=NoModel(), tools=a.tools, risk_gate=a.risk_gate,
                  kill_switch=a.kill_switch, store=a.store, on_event=lambda _m: None,
                  defer_confirmation=True)
    for name, arguments, expected in (
            ("get_system_status", {}, "ok"),                 # domain 3
            ("list_devices", {}, "ok"),                      # domain 6
            ("get_network_status", {}, "ok"),                # domain 7
            ("get_active_window", {}, "ok"),                 # domain 1
            ("get_file_info", {"path": str(tmp_path / "work")}, "ok"),   # domain 2
            ("get_camera_status", {}, "ok"),                 # domain 5
            ("enable_camera", {}, "unauthorized"),           # domain 5, gated
    ):
        out = agent.invoke_tool(name, arguments)
        assert out.kind == expected, f"{name}: {out.kind} ({out.summary[:90]})"


def test_no_new_capability_consults_a_model(tmp_path):
    """Deterministic by construction: observation and device answers are measurements, not inferences."""
    from void.core.agent import Agent

    calls = []

    class Counting:
        name = "fake"

        def available(self):
            return True

        def generate(self, messages, tools=None):
            calls.append(1)
            return LLMResponse(text="(model)")

    a = make_assistant(tmp_path)
    a.providers = ProviderRegistry({"fake": Counting()}, ["fake"])
    agent = Agent(provider=Counting(), tools=a.tools, risk_gate=a.risk_gate,
                  kill_switch=a.kill_switch, store=a.store, on_event=lambda _m: None,
                  defer_confirmation=True)
    for name, arguments in (("get_system_status", {}), ("diagnose_slowness", {}),
                            ("list_processes", {}), ("list_devices", {}),
                            ("find_device", {"name": "headset"}), ("get_network_status", {}),
                            ("list_connections", {}), ("get_camera_status", {}),
                            ("get_active_window", {})):
        agent.invoke_tool(name, arguments)
    assert calls == [], f"{len(calls)} model calls were made by deterministic capabilities"
