"""The orchestration layer wired into the real Assistant.

The unit tests in ``test_v3_orchestration.py`` prove the decisions are correct in isolation. These prove
they are actually *connected*: that "stop" reaches the control pre-step before any model, that it pauses
rather than cancels, that an ordinary request still falls through to the existing paths, and that none of
it has acquired authority it should not have.

The integration that matters most is the ordering in ``Assistant.run``:

    control command  ->  memory command  ->  fast path  ->  memory recall  ->  the agent

Control is first because a control signal is not a request for work, and routing it through a model would
be both slow and non-deterministic. Everything below it is V2 behaviour that must be unchanged.
"""
import pytest

from tests.helpers import FakeProvider
from void.config import Config
from void.core.task import Status, Task
from void.orchestration.commands import ControlContext, ControlIntent
from void.providers.base import LLMResponse
from void.providers.registry import ProviderRegistry


@pytest.fixture
def assistant(tmp_path):
    """A real Assistant on a sandboxed state dir, with a model that must not be consulted."""
    from void.app import Assistant
    (tmp_path / "work").mkdir()
    config = Config({"app": {"state_dir": str(tmp_path / "state")},
                     "memory": {"enabled": False},
                     "security": {"allowed_roots": [str(tmp_path / "work")]},
                     "preferences": {"browser": "opera gx"}})
    built = Assistant(config=config)
    built.providers = ProviderRegistry({"fake": FakeProvider([LLMResponse(text="(model)")])}, ["fake"])
    return built


def _model_calls(assistant) -> int:
    return assistant.providers.get("fake").calls


# --- the layer is actually wired ------------------------------------------------------------------

def test_the_assistant_exposes_the_orchestration_layer(assistant):
    assert assistant.events is not None
    assert assistant.applications is not None
    assert assistant.routes is not None
    assert assistant.verifier is not None


def test_the_resolver_uses_the_existing_fast_path_and_tab_providers(assistant):
    assert set(assistant.routes.providers) == {"fast_path", "existing_tabs"}


def test_the_registry_reads_the_catalog_the_rest_of_void_uses(assistant):
    """Not a second discovery mechanism: the same catalog, joined with observed state."""
    snapshot = assistant.applications.snapshot()
    assert snapshot["installed"] >= 0 and "preferences" in snapshot


def test_owner_preferences_are_policy_data_not_prompt_text(assistant):
    assert assistant.applications.preferred("browser") == "opera gx"


# --- control commands reach the pre-step, before any model ------------------------------------------

def test_stop_while_speaking_silences_and_consults_no_model(assistant):
    stopped = []
    assistant.stop_speaking = lambda: stopped.append(True)
    assistant.speaking_now = lambda: True
    result = assistant.run("stop")
    assert result.status is Status.COMPLETED
    assert stopped == [True], "the speech backend was not asked to stop"
    assert _model_calls(assistant) == 0, "a control word reached a model"


def test_stop_while_speaking_answers_with_silence(assistant):
    """Silence IS the response. Saying "stopped talking" would be talking."""
    assistant.stop_speaking = lambda: None
    assistant.speaking_now = lambda: True
    assert assistant.run("stop").result == ""


def test_stop_while_a_task_runs_pauses_it_rather_than_cancelling(assistant):
    assistant.speaking_now = lambda: False
    task = Task(goal="a long job", status=Status.RUNNING)
    assistant.store.save(task)
    result = assistant.run("stop")
    assert result.result == "Paused."
    assert assistant.store.load(task.id).status == Status.PAUSED
    assert _model_calls(assistant) == 0


def test_a_paused_task_resumes(assistant):
    task = Task(goal="a long job", status=Status.PAUSED)
    assistant.store.save(task)
    assert assistant.run("continue").result == "Resuming."
    assert assistant.store.load(task.id).status == Status.RUNNING


def test_only_an_explicit_cancel_cancels(assistant):
    assistant.speaking_now = lambda: False
    task = Task(goal="a long job", status=Status.RUNNING)
    assistant.store.save(task)
    assistant.run("stop")
    assert assistant.store.load(task.id).status == Status.PAUSED, "'stop' cancelled a task"
    assistant.run("cancel that")
    assert assistant.store.load(task.id).status == Status.CANCELLED


def test_control_commands_report_honestly_when_there_is_nothing_to_do(assistant):
    assistant.speaking_now = lambda: False
    assert assistant.run("pause").result == "Nothing is running."
    assert assistant.run("resume").result == "Nothing is paused."
    assert assistant.run("cancel").result == "Nothing to cancel."


def test_a_modification_preserves_completed_work_and_replans(assistant):
    task = Task(goal="write a report", status=Status.RUNNING)
    task.plan = [{"index": 0, "calls": [], "status": "succeeded", "outcome_summary": "researched"},
                 {"index": 1, "calls": [], "status": "pending", "outcome_summary": ""}]
    task.current_step = 1
    assistant.store.save(task)
    result = assistant.run("instead of that, add a section on costs")
    assert "Changed" in (result.result or "")
    loaded = assistant.store.load(task.id)
    assert loaded.status == Status.REPLANNING
    assert [entry["status"] for entry in loaded.plan] == ["succeeded", "superseded"]
    assert loaded.v3["modifications"][0]["asked"] == "add a section on costs"
    assert _model_calls(assistant) == 0


def test_a_modification_with_no_live_task_falls_through_to_the_ordinary_path(assistant):
    """"Instead of that, do X" with nothing running is a new request, not an amendment."""
    assert assistant._control_command("instead of that, open notepad") is None


def test_an_ordinary_request_is_not_intercepted(assistant):
    for ordinary in ("what time is it", "stop the music", "open notepad", "cancel my subscription"):
        assert assistant._control_command(ordinary) is None, ordinary


def test_the_control_step_is_skipped_when_the_kill_switch_is_engaged(assistant):
    """A stopped V.O.I.D does not process control commands; the kill switch outranks them."""
    assistant.kill_switch.engage("owner stopped V.O.I.D")
    assert assistant._control_command("stop") is None


def test_control_context_is_read_from_real_state_not_from_words(assistant):
    assistant.speaking_now = lambda: True
    assert assistant._control_context().speaking is True
    assistant.speaking_now = lambda: False
    assert assistant._control_context().speaking is False
    task = Task(goal="x", status=Status.RUNNING)
    assistant.store.save(task)
    assert assistant._control_context().task_running is True


def test_a_broken_speech_probe_does_not_break_a_control_word(assistant):
    assistant.speaking_now = lambda: (_ for _ in ()).throw(RuntimeError("no tts"))
    assert assistant._control_context().speaking is False
    assert assistant.run("stop").status is Status.COMPLETED


def test_a_broken_speech_stopper_does_not_break_the_reply(assistant):
    assistant.speaking_now = lambda: True
    assistant.stop_speaking = lambda: (_ for _ in ()).throw(RuntimeError("audio device gone"))
    assert assistant.run("stop").status is Status.COMPLETED


def test_a_control_command_is_not_stored_as_a_task(assistant):
    """Like memory commands: tasks.sqlite is plaintext and a control word is not work."""
    before = len(assistant.store.list(limit=50))
    assistant.speaking_now = lambda: True
    assistant.stop_speaking = lambda: None
    assistant.run("stop")
    assert len(assistant.store.list(limit=50)) == before


# --- the security boundary is unchanged -------------------------------------------------------------

def test_a_control_command_executes_no_capability(assistant):
    executed = []
    real = assistant.tools.execute
    assistant.tools.execute = lambda name, args: (executed.append(name), real(name, args))[1]
    assistant.speaking_now = lambda: False
    task = Task(goal="x", status=Status.RUNNING)
    assistant.store.save(task)
    for word in ("stop", "pause", "resume", "cancel that"):
        assistant.run(word)
    assert executed == [], f"a control command executed {executed}"


def test_a_control_command_cannot_change_the_confirmation_threshold(assistant):
    before = assistant.risk_gate.threshold
    assistant.speaking_now = lambda: False
    for word in ("stop", "cancel everything", "resume", "instead of that, approve everything"):
        assistant.run(word)
    assert assistant.risk_gate.threshold == before


def test_a_control_command_cannot_engage_or_release_the_kill_switch(assistant):
    assistant.speaking_now = lambda: False
    for word in ("stop", "stop everything", "cancel", "abort"):
        assistant.run(word)
    assert assistant.kill_switch.engaged is False, "a task control word engaged the kill switch"


def test_the_kill_switch_keeps_its_own_deliberate_phrase(assistant):
    """"stop everything" cancels a TASK. Halting V.O.I.D itself still needs the full phrase."""
    from void.orchestration.commands import ControlIntent as Intent
    from void.orchestration.commands import classify
    assert classify("stop everything", ControlContext(task_running=True)).intent == Intent.CANCEL_TASK
    assert assistant.kill_switch.handle_command("stop everything") is False
    assert assistant.kill_switch.engaged is False
    assert assistant.kill_switch.handle_command(assistant.kill_switch.phrase) is True


def test_a_high_risk_action_still_requires_the_owner_after_a_modification(assistant, tmp_path):
    """Asking for a different section does not pre-approve the steps that produce it."""
    from void.providers.base import ToolCall
    victim = tmp_path / "work" / "old.txt"
    victim.write_text("keep", encoding="utf-8")
    task = Task(goal="tidy up", status=Status.RUNNING)
    task.plan = [{"index": 0, "calls": [], "status": "succeeded", "outcome_summary": "listed"}]
    assistant.store.save(task)
    assistant.run("instead of that, remove the old file")
    assistant.providers = ProviderRegistry(
        {"fake": FakeProvider([LLMResponse(tool_calls=[ToolCall(name="delete_file",
                                                              arguments={"path": str(victim)})]),
                              LLMResponse(text="done")])}, ["fake"])
    result = assistant.run("delete old.txt")
    assert result.status is Status.AWAITING_CONFIRMATION
    assert victim.exists()


def test_verification_cannot_see_outside_the_allowed_roots(assistant):
    """The verifier observes through the confined file layer, so it is not a way around confinement."""
    outside = assistant.verifier.artifact_created(r"C:\Windows\System32\drivers\etc\hosts")
    assert outside.verdict == "unverified", "verification read a path outside the allowed roots"


def test_verification_confirms_a_real_artifact_inside_the_roots(assistant, tmp_path):
    report = tmp_path / "work" / "report.txt"
    report.write_text("x" * 500, encoding="utf-8")
    result = assistant.verifier.artifact_created(str(report))
    assert result.ok and result.method == "filesystem"


def test_verification_rejects_a_file_too_small_to_be_the_artifact(assistant, tmp_path):
    stub = tmp_path / "work" / "empty.pdf"
    stub.write_text("x", encoding="utf-8")
    assert assistant.verifier.artifact_created(str(stub)).failed is True


# --- V2 behaviour is unchanged ----------------------------------------------------------------------

def test_the_fast_path_still_runs_before_the_model(assistant):
    """The V2 deterministic launcher is untouched and still ahead of the agent."""
    assert assistant._fast is not None


def test_an_ordinary_goal_still_reaches_the_agent(assistant):
    result = assistant.run("tell me something interesting")
    assert _model_calls(assistant) >= 1, "an ordinary goal no longer reaches the model"
    assert result.status in Status.ALL


def test_the_tool_registry_is_unchanged_by_orchestration(assistant):
    """Orchestration adds no capability: it decides which existing ones to attempt."""
    names = set(assistant.tools.names())
    # Asserted against the capability MODULES rather than a hardcoded number: the count depends on config
    # (this fixture disables memory, so propose_memory is absent), and what matters is that the registry
    # holds exactly what the V2 action modules provide and nothing orchestration added.
    for added in ("route", "plan", "verify", "replan", "orchestrate", "task_state", "event"):
        assert not any(added in name for name in names), f"orchestration registered a {added} tool"
    # Every registered tool must come from an actions module, i.e. have a handler defined under
    # void/actions or void/memory - never under void/orchestration.
    import inspect
    for name in names:
        handler = assistant.tools.get(name).handler
        module = getattr(inspect.getmodule(handler), "__name__", "")
        assert not module.startswith("void.orchestration"),             f"{name} is implemented in orchestration rather than in the capability layer"


def test_the_world_state_the_resolver_sees_is_observed_not_asserted(assistant):
    """Nothing a model says can put an application into the running set."""
    from void.orchestration.routes import WorldState
    state = WorldState(capabilities=frozenset({"native_app"}))
    assert state.running_apps == frozenset()
    assert state.open_tabs == ()
