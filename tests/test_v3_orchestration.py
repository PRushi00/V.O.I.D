"""The V3 orchestration layer: events, control semantics, routes, registry, verification, replanning.

This is the layer that turns V.O.I.D from "a set of capabilities with a security model" into "an agent that
decides how to reach a goal and checks whether it worked". It sits *above* the V2 capability layer, so most
of what is asserted here is decision-making over observed state - pure functions, tested as such.

The security claim that runs through the whole file: **orchestration decides what to attempt, never what is
permitted.** A route is a proposal, a plan is a proposal, a verification result is an observation, a
modification is an instruction about work. None of them can authorize a tool, raise or lower a risk level,
skip a confirmation, or touch the kill switch. Several tests assert that directly, including against the
source, because it is the property most likely to be eroded by a later convenience.
"""
import time

import pytest

from void.core.fast_path import DirectCall
from void.core.task import Status, Task, TaskStore
from void.orchestration import replan as replan_mod
from void.orchestration import trace as trace_mod
from void.orchestration.apps import Application, ApplicationRegistry
from void.orchestration.commands import (ControlCommand, ControlContext, ControlIntent, ControlRouter,
                                         classify)
from void.orchestration.events import EventLog, TaskEvent, TaskEventKind, summarise
from void.orchestration.replan import (FailureKind, apply_modification, boundary, can_modify, checkpoint,
                                       classify_failure, decide_recovery, note_artifact, record_failure)
from void.orchestration.routes import (ExistingTabRoutes, FastPathRoutes, Resolution, Route, RouteKind,
                                       RouteResolver, WorldState, score)
from void.orchestration.verify import (VerificationMethod, VerificationResult, Verdict, Verifier,
                                       summarise as verify_summary)
from void.security.risk import RiskLevel



def _code_of(module_or_func) -> str:
    """Source with docstrings stripped.

    These modules *document* that they never touch RiskGate, never screenshot and never engage the kill
    switch, so a raw-text scan for those words flags the explanation rather than a defect. Unparsing the
    AST keeps the executable code and drops the prose.
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(module_or_func)))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ================================================================= interaction model

def test_an_event_must_be_a_known_kind():
    """A closed set, so a UI adapter can switch exhaustively and a typo is a crash, not a silent 'unknown'."""
    TaskEvent(kind=TaskEventKind.TASK_STARTED, task_id="t1")
    with pytest.raises(ValueError):
        TaskEvent(kind="task_exploded", task_id="t1")


def test_event_detail_is_flattened_and_bounded():
    event = TaskEvent(kind=TaskEventKind.TASK_PROGRESS, task_id="t1",
                      detail="line one\nline two\t" + "x" * 500)
    assert "\n" not in event.detail and "\t" not in event.detail
    assert len(event.detail) <= 200


def test_an_event_is_immutable():
    """History must not be editable by whatever consumed it."""
    event = TaskEvent(kind=TaskEventKind.TASK_STARTED, task_id="t1")
    with pytest.raises(Exception):
        event.kind = TaskEventKind.TASK_FAILED


def test_an_event_dict_omits_unset_fields():
    event = TaskEvent(kind=TaskEventKind.TASK_STARTED, task_id="t1")
    assert set(event.as_dict()) == {"kind", "task_id", "at"}
    full = TaskEvent(kind=TaskEventKind.ACTION_EXECUTED, task_id="t1", tool="launch_app",
                     step=2, ok=True, duration_s=0.5, route="route-a", detail="done")
    assert full.as_dict()["tool"] == "launch_app" and full.as_dict()["ok"] is True


def test_the_log_is_append_only_and_bounded():
    log = EventLog(max_events=5)
    for index in range(12):
        log.emit(TaskEventKind.TASK_PROGRESS, "t1", step=index)
    assert len(log) == 5
    assert [event.step for event in log.events()] == [7, 8, 9, 10, 11], "the newest must be kept"


def test_the_log_returns_copies_so_history_cannot_be_rewritten():
    log = EventLog()
    log.emit(TaskEventKind.TASK_STARTED, "t1")
    snapshot = log.events()
    snapshot.clear()
    assert len(log) == 1


def test_subscribers_receive_events_live():
    seen = []
    log = EventLog(on_event=seen.append)
    log.emit(TaskEventKind.TASK_STARTED, "t1")
    log.subscribe(seen.append)
    log.emit(TaskEventKind.TASK_COMPLETED, "t1")
    assert [event.kind for event in seen] == [TaskEventKind.TASK_STARTED,
                                              TaskEventKind.TASK_COMPLETED,
                                              TaskEventKind.TASK_COMPLETED]


def test_a_failing_subscriber_cannot_break_a_task():
    """A UI crashing must not stop work."""
    good = []

    def explodes(_event):
        raise RuntimeError("the UI fell over")
    log = EventLog(on_event=explodes)
    log.subscribe(good.append)
    log.emit(TaskEventKind.TASK_STARTED, "t1")
    assert len(good) == 1 and len(log) == 1


def test_terminal_kinds_are_marked():
    assert TaskEvent(kind=TaskEventKind.TASK_COMPLETED, task_id="t").terminal is True
    assert TaskEvent(kind=TaskEventKind.TASK_PROGRESS, task_id="t").terminal is False


def test_a_run_summary_is_built_from_what_happened():
    log = EventLog()
    log.emit(TaskEventKind.ACTION_EXECUTED, "t1")
    log.emit(TaskEventKind.ACTION_EXECUTED, "t1")
    log.emit(TaskEventKind.ACTION_VERIFIED, "t1", ok=True)
    log.emit(TaskEventKind.TASK_REPLANNED, "t1")
    text = summarise(log.events())
    assert "2 action(s)" in text and "1 verified" in text and "replanned 1 time(s)" in text
    assert summarise([]) == "nothing ran"


def test_the_event_model_does_not_depend_on_any_ui_protocol():
    """The whole point of owning the model: AG-UI/A2UI are adapters, not the definition."""
    from void.orchestration import events
    code = _code_of(events).lower()
    for foreign in ("ag_ui", "agui", "a2ui", "requests", "websocket", "fastapi"):
        assert foreign not in code, f"{foreign} leaked into the internal event model"


# ================================================================= control semantics

CONTEXTS = {
    "speaking": ControlContext(speaking=True),
    "working": ControlContext(task_running=True),
    "speaking+working": ControlContext(speaking=True, task_running=True),
    "idle": ControlContext(),
}


def test_a_bare_stop_while_speaking_means_be_quiet():
    assert classify("stop", CONTEXTS["speaking"]).intent == ControlIntent.STOP_SPEAKING


def test_a_bare_stop_while_working_pauses_rather_than_cancels():
    """The asymmetry that matters: a wrongly-paused task costs a word, a wrongly-cancelled one may cost
    everything. So "stop" alone is never a cancel."""
    assert classify("stop", CONTEXTS["working"]).intent == ControlIntent.PAUSE_TASK


@pytest.mark.parametrize("label", list(CONTEXTS))
def test_a_bare_stop_can_never_cancel_a_task_from_any_state(label):
    """The single most important test in this file."""
    for word in ("stop", "stop it", "stop that", "quiet", "enough", "shut up", "ok stop"):
        command = classify(word, CONTEXTS[label])
        assert command.intent != ControlIntent.CANCEL_TASK, f"{word!r} in {label} became a cancel"


def test_speech_is_prioritised_when_both_are_happening():
    """Speaking is the thing the owner is reacting to, so it wins."""
    assert classify("stop", CONTEXTS["speaking+working"]).intent == ControlIntent.STOP_SPEAKING


def test_an_explicit_speech_request_is_never_a_task_command():
    for phrase in ("stop talking", "stop speaking", "be quiet now", "mute"):
        for label in CONTEXTS:
            assert classify(phrase, CONTEXTS[label]).intent == ControlIntent.STOP_SPEAKING, phrase


@pytest.mark.parametrize("phrase", ["cancel", "cancel that", "abort", "forget the whole thing",
                                    "stop the task", "stop everything", "scrap it", "abandon the task"])
def test_an_explicit_cancel_cancels(phrase):
    assert classify(phrase, CONTEXTS["working"]).intent == ControlIntent.CANCEL_TASK


@pytest.mark.parametrize("phrase", ["pause", "pause that", "hold on", "wait a second", "one moment"])
def test_an_explicit_pause_pauses(phrase):
    assert classify(phrase, CONTEXTS["working"]).intent == ControlIntent.PAUSE_TASK


@pytest.mark.parametrize("phrase", ["resume", "continue", "carry on", "keep going", "go ahead"])
def test_an_explicit_resume_resumes(phrase):
    assert classify(phrase, CONTEXTS["working"]).intent == ControlIntent.RESUME_TASK


def test_a_modification_is_recognised_with_its_content():
    command = classify("instead of that, add a section on costs", CONTEXTS["working"])
    assert command.intent == ControlIntent.MODIFY_TASK
    assert command.modification == "add a section on costs"


def test_a_modification_opener_with_nothing_after_it_is_not_a_command():
    """"Actually" on its own is a hesitation, not an instruction."""
    for hesitation in ("actually", "instead", "no wait"):
        assert classify(hesitation, CONTEXTS["working"]).intent != ControlIntent.MODIFY_TASK


@pytest.mark.parametrize("phrase", [
    "stop the music", "don't stop", "stop sign", "what does stop mean",
    "cancel my subscription", "pause the video", "continue reading the file to me",
    "tell me about pausing", "resume my document", "open stop.txt",
])
def test_an_ordinary_request_containing_a_control_word_is_not_a_control_command(phrase):
    """Whole-utterance matching: these are work, not control signals."""
    assert classify(phrase, CONTEXTS["working"]).intent == ControlIntent.NONE, phrase


def test_leading_courtesies_are_peeled():
    for phrase in ("ok stop", "okay, stop", "void stop", "hey void, please stop", "um, stop"):
        assert classify(phrase, CONTEXTS["speaking"]).intent == ControlIntent.STOP_SPEAKING, phrase


def test_classification_survives_odd_input():
    for junk in (None, 12345, "", "   ", "\x00\x00", "?" * 500, [], {}):
        assert classify(junk, CONTEXTS["idle"]).intent in ControlIntent.ALL


def test_classification_is_pure():
    """No side effects: classifying must not stop speech or touch a task."""
    from void.orchestration import commands
    code = _code_of(commands.classify)
    for forbidden in ("self.", "store", "save(", "stop(", "engage"):
        assert forbidden not in code, f"classify touches {forbidden}"


def test_the_router_runs_the_matching_effect_and_only_that_one():
    calls = []
    router = ControlRouter(stop_speech=lambda: calls.append("speech"),
                           pause_task=lambda: calls.append("pause"),
                           resume_task=lambda: calls.append("resume"),
                           cancel_task=lambda: calls.append("cancel"),
                           modify_task=lambda text: calls.append(f"modify:{text}"))
    router.handle("stop", CONTEXTS["speaking"])
    assert calls == ["speech"]
    router.handle("cancel that", CONTEXTS["working"])
    assert calls == ["speech", "cancel"]
    router.handle("instead of that, use a table", CONTEXTS["working"])
    assert calls[-1] == "modify:use a table"


def test_the_router_ignores_an_ordinary_request():
    calls = []
    router = ControlRouter(stop_speech=lambda: calls.append("speech"),
                           cancel_task=lambda: calls.append("cancel"))
    command = router.handle("what is the weather", CONTEXTS["working"])
    assert command.intent == ControlIntent.NONE
    assert calls == [], "an ordinary request triggered a control effect"


def test_a_failing_effect_does_not_propagate():
    """A failing "stop speaking" must not crash the loop trying to listen to the owner."""
    audit = []
    router = ControlRouter(stop_speech=lambda: (_ for _ in ()).throw(RuntimeError("no audio")),
                           on_audit=audit.append)
    command = router.handle("stop", CONTEXTS["speaking"])
    assert command.intent == ControlIntent.STOP_SPEAKING
    assert any("CONTROL_EFFECT_FAILED" in line for line in audit)


def test_the_control_layer_never_touches_the_kill_switch():
    """The kill switch keeps its own deliberate full phrase; this layer is about the task."""
    from void.orchestration import commands
    code = _code_of(commands)
    for forbidden in ("kill_switch", "KillSwitch", "engage(", "STOP EVERYTHING"):
        assert forbidden not in code, f"the control layer references {forbidden}"


# ================================================================= routes

def _route(**kwargs):
    kwargs.setdefault("kind", RouteKind.NATIVE_APP)
    kwargs.setdefault("describes", "do the thing")
    return Route(**kwargs)


def test_a_route_must_be_a_known_kind():
    _route(kind=RouteKind.BROWSER)
    with pytest.raises(ValueError):
        _route(kind="telepathy")


def test_route_kind_preference_follows_the_blueprint_ladder():
    """Structured state, then structured interfaces, then native, browser, desktop UI, vision, raw input."""
    order = [RouteKind.EXISTING_STATE, RouteKind.STRUCTURED, RouteKind.NATIVE_APP, RouteKind.BROWSER,
             RouteKind.DESKTOP_UI, RouteKind.VISION, RouteKind.INPUT_FALLBACK]
    assert [RouteKind.rank(kind) for kind in order] == sorted(RouteKind.rank(k) for k in order)
    assert RouteKind.rank("something new") == len(order), "an unknown kind must sort last"


def test_reusing_existing_state_beats_everything_else():
    """A browser route that reuses a session beats a native route that starts something new."""
    reuse = _route(kind=RouteKind.BROWSER, reuses_existing=True)
    fresh = _route(kind=RouteKind.STRUCTURED, reuses_existing=False)
    assert score(reuse) < score(fresh)


def test_among_equals_lower_risk_wins():
    low = _route(risk=RiskLevel.LOW)
    high = _route(risk=RiskLevel.HIGH)
    assert score(low) < score(high)


def test_among_equals_reliability_beats_latency():
    reliable_slow = _route(reliability=0.95, latency_s=5.0)
    flaky_fast = _route(reliability=0.4, latency_s=0.1)
    assert score(reliable_slow) < score(flaky_fast), "speed was preferred over correctness"


def test_route_scoring_is_total_and_stable():
    routes = [_route(id=f"route-{i}") for i in range(5)]
    assert sorted(routes, key=score) == sorted(list(reversed(routes)), key=score)


def test_reliability_and_latency_are_clamped():
    assert _route(reliability=5.0).reliability == 1.0
    assert _route(reliability=-1.0).reliability == 0.0
    assert _route(latency_s=-3.0).latency_s == 0.0


def test_a_route_dict_never_contains_arguments():
    """A route is inspectable without exposing what it would pass to a tool."""
    route = _route(calls=(DirectCall("launch_app", {"name": "app-id-secret"}, "ok"),))
    assert route.as_dict()["calls"] == ["launch_app"]
    assert "app-id-secret" not in str(route.as_dict())


def test_the_resolver_prefers_an_open_tab_over_launching_a_browser():
    """The blueprint's headline example, as behaviour."""
    state = WorldState(open_tabs=(("Opera GX", "https://mail.google.com/", "Inbox - Gmail"),),
                       capabilities=frozenset({"browser", "native_app"}),
                       preferences={"browser": "opera gx"})

    class Launcher:
        name = "launcher"

        def propose(self, goal, state):
            return [_route(describes="launch a browser", latency_s=3.0,
                           requires=frozenset({"native_app"}))]
    resolution = RouteResolver([ExistingTabRoutes(), Launcher()]).resolve("open gmail", state)
    assert resolution.resolved
    assert resolution.selected.kind == RouteKind.EXISTING_STATE
    assert resolution.selected.reuses_existing is True


def test_with_no_open_tab_the_launch_route_is_used():
    class Launcher:
        name = "launcher"

        def propose(self, goal, state):
            return [_route(describes="launch a browser", requires=frozenset({"native_app"}))]
    state = WorldState(capabilities=frozenset({"browser", "native_app"}))
    resolution = RouteResolver([ExistingTabRoutes(), Launcher()]).resolve("open gmail", state)
    assert resolution.selected.kind == RouteKind.NATIVE_APP


def test_a_route_whose_capability_is_missing_is_dropped_before_selection():
    """It must never be chosen and then discovered unavailable - that looks like a bug to the owner."""
    audit = []
    state = WorldState(open_tabs=(("Opera", "https://pinterest.com/", "Pinterest"),),
                       capabilities=frozenset())                 # no browser adapter installed
    resolution = RouteResolver([ExistingTabRoutes()], on_audit=audit.append).resolve(
        "open pinterest", state)
    assert resolution.resolved is False
    assert any("ROUTE_UNAVAILABLE" in line for line in audit)


def test_a_broken_provider_does_not_stop_the_others():
    class Broken:
        name = "broken"

        def propose(self, goal, state):
            raise RuntimeError("provider fell over")

    class Works:
        name = "works"

        def propose(self, goal, state):
            return [_route()]
    audit = []
    resolution = RouteResolver([Broken(), Works()], on_audit=audit.append).resolve("do it")
    assert resolution.resolved
    assert any("ROUTE_PROVIDER_FAILED" in line for line in audit)


def test_a_provider_returning_rubbish_is_ignored():
    class Rubbish:
        name = "rubbish"

        def propose(self, goal, state):
            return ["not a route", None, 42]
    assert RouteResolver([Rubbish()]).resolve("do it").resolved is False


def test_two_materially_different_equally_good_routes_are_reported_ambiguous():
    """The blueprint says ask rather than guess."""
    class Two:
        name = "two"

        def propose(self, goal, state):
            return [_route(kind=RouteKind.NATIVE_APP, id="route-a"),
                    _route(kind=RouteKind.BROWSER, id="route-b", reliability=0.5, latency_s=1.0)]

    class TwoSame:
        name = "same"

        def propose(self, goal, state):
            return [_route(kind=RouteKind.NATIVE_APP, id="route-c"),
                    _route(kind=RouteKind.NATIVE_APP, id="route-d")]
    # Different kinds, identical scores apart from the id tie-break -> ambiguous.
    ambiguous = RouteResolver([Two()]).resolve("do it")
    if ambiguous.ambiguous:
        assert ambiguous.selected is None and len(ambiguous.candidates) == 2
    # Same kind -> not an ambiguity the owner would care about.
    assert RouteResolver([TwoSame()]).resolve("do it").ambiguous is False


def test_no_route_at_all_is_reported_rather_than_invented():
    resolution = RouteResolver([]).resolve("do something impossible")
    assert resolution.resolved is False and "no route" in resolution.why


def test_world_state_answers_from_observation_only():
    state = WorldState(running_apps=frozenset({"opera.exe", "code.exe"}),
                       open_tabs=(("Opera", "https://x.com/", "X"),),
                       preferences={"browser": "Opera GX"})
    assert state.is_running("opera") is True
    assert state.is_running("notepad") is False
    assert state.is_running("") is False
    assert state.tabs_matching("x.com") and not state.tabs_matching("pinterest")
    assert state.preferred("browser") == "opera gx"
    assert state.preferred("editor") is None


def test_the_fast_path_provider_marks_an_already_running_app_as_reuse():
    class FakeTarget:
        label = "Opera"
        alternatives = (DirectCall("launch_app", {"name": "opera-id"}, "Opening Opera."),)

    class FakePlan:
        targets = (FakeTarget(),)

    class FakeDecision:
        plan = FakePlan()

    class FakeFast:
        def decide(self, goal):
            return FakeDecision()
    provider = FastPathRoutes(FakeFast())
    running = list(provider.propose("open opera", WorldState(running_apps=frozenset({"opera.exe"}))))
    assert running[0].reuses_existing is True
    fresh = list(provider.propose("open opera", WorldState()))
    assert fresh[0].reuses_existing is False


def test_the_fast_path_provider_proposes_nothing_when_the_fast_path_declines():
    class NoPlan:
        plan = None

    class FakeFast:
        def decide(self, goal):
            return NoPlan()
    assert list(FastPathRoutes(FakeFast()).propose("philosophise", WorldState())) == []
    assert list(FastPathRoutes(None).propose("anything", WorldState())) == []


def test_a_goal_that_is_not_an_open_request_yields_no_tab_routes():
    state = WorldState(open_tabs=(("Opera", "https://mail.google.com/", "Gmail"),),
                       capabilities=frozenset({"browser"}))
    assert list(ExistingTabRoutes().propose("what is the capital of France", state)) == []


def test_the_preferred_browser_tab_outranks_the_same_page_elsewhere():
    state = WorldState(open_tabs=(("Chrome", "https://pinterest.com/", "Pinterest"),
                                  ("Opera GX", "https://pinterest.com/", "Pinterest")),
                       capabilities=frozenset({"browser"}),
                       preferences={"browser": "opera gx"})
    routes = list(ExistingTabRoutes().propose("open pinterest", state))
    assert "Opera GX" in routes[0].describes


def test_orchestration_never_imports_an_authorization_control():
    """Routes propose; RiskGate decides. The resolver must not be able to consult or alter it."""
    from void.orchestration import routes
    code = _code_of(routes)
    # RiskLevel is imported for SCORING - a route records how much authority it would need so a cheap
    # safe route can be preferred. RiskGate itself, and anything that authorizes, must be absent.
    assert "RiskGate" not in code
    assert ".authorize(" not in code and "requires_confirmation" not in code
    assert "kill_switch" not in code


# ================================================================= application registry

class FakeEntry:
    def __init__(self, app_id, name, kind="exe"):
        self.app_id, self.name, self.kind = app_id, name, kind


def _registry(entries=(), running=(), preferences=None):
    return ApplicationRegistry(
        catalog_entries=lambda: list(entries),
        running_apps=lambda: [{"name": name, "pid": 1} for name in running],
        preferences=preferences or {})


def test_the_registry_separates_installed_from_running():
    registry = _registry([FakeEntry("a", "Opera"), FakeEntry("b", "Notepad")], running=["opera.exe"])
    applications = {app.name: app for app in registry.applications()}
    assert applications["Opera"].running is True
    assert applications["Notepad"].running is False
    assert applications["Notepad"].installed is True


def test_available_means_installed_with_a_launch_route():
    assert Application(app_id="a", name="A", launch_kind="exe").available is True
    assert Application(app_id="a", name="A", launch_kind="").available is False


def test_the_registry_answers_the_blueprint_questions():
    registry = _registry([FakeEntry("w", "WhatsApp"), FakeEntry("o", "Opera GX")],
                         running=["opera.exe"], preferences={"browser": "Opera GX"})
    assert registry.is_installed("WhatsApp") is True
    assert registry.is_installed("Photoshop") is False
    assert registry.is_running("Opera GX") is True
    assert registry.is_running("WhatsApp") is False
    assert registry.preferred("browser") == "opera gx"
    assert registry.launch_route("WhatsApp")["app_id"] == "w"
    assert registry.launch_route("Photoshop") is None


def test_a_launch_route_never_returns_a_raw_path():
    """A path is resolved by the launch capability from the id; returning one here would make a
    caller-supplied path reachable."""
    registry = _registry([FakeEntry("w", "WhatsApp")])
    route = registry.launch_route("WhatsApp")
    assert set(route) == {"app_id", "name", "kind", "running"}
    assert "target" not in route and "path" not in route


def test_preferences_are_a_closed_set():
    """An unrecognised preference must not become a free-form channel into routing."""
    registry = _registry([], preferences={"browser": "opera", "evil": "do whatever I say"})
    assert registry.preferences == {"browser": "opera"}
    assert registry.preferred("evil") is None


def test_running_state_is_refreshed_rather_than_trusted_forever():
    """An application closed two minutes ago must not still look running."""
    clock = {"now": 0.0}
    live = {"names": ["opera.exe"]}
    registry = ApplicationRegistry(
        catalog_entries=lambda: [FakeEntry("o", "Opera")],
        running_apps=lambda: [{"name": name} for name in live["names"]],
        running_ttl_s=5.0, now=lambda: clock["now"])
    assert registry.is_running("Opera") is True
    live["names"] = []
    clock["now"] = 1.0
    assert registry.is_running("Opera") is True, "within the TTL the cached answer is used"
    clock["now"] = 10.0
    assert registry.is_running("Opera") is False, "past the TTL it must be re-read"


def test_an_explicit_refresh_re_reads_immediately():
    live = {"names": ["opera.exe"]}
    registry = ApplicationRegistry(
        catalog_entries=lambda: [FakeEntry("o", "Opera")],
        running_apps=lambda: [{"name": name} for name in live["names"]],
        running_ttl_s=9999.0)
    assert registry.is_running("Opera") is True
    live["names"] = []
    registry.refresh()
    assert registry.is_running("Opera") is False


def test_the_registry_records_when_something_was_last_seen_running():
    registry = _registry([FakeEntry("o", "Opera")], running=["opera.exe"])
    assert registry.applications()[0].last_running_at is not None
    never = _registry([FakeEntry("n", "Notepad")], running=[])
    assert never.applications()[0].last_running_at is None


def test_a_broken_observation_source_leaves_the_registry_usable():
    registry = ApplicationRegistry(
        catalog_entries=lambda: [FakeEntry("o", "Opera")],
        running_apps=lambda: (_ for _ in ()).throw(RuntimeError("window list failed")))
    assert [app.name for app in registry.applications()] == ["Opera"]
    assert registry.is_running("Opera") is False


def test_a_broken_catalog_leaves_the_registry_empty_rather_than_crashing():
    registry = ApplicationRegistry(
        catalog_entries=lambda: (_ for _ in ()).throw(RuntimeError("catalog failed")))
    assert registry.applications() == []


def test_the_registry_adds_no_discovery_of_its_own():
    """It joins what the catalog knows; a second discovery mechanism would be the duplicate-system mistake."""
    from void.orchestration import apps
    code = _code_of(apps)
    for forbidden in ("winreg", "glob", "os.walk", "subprocess", "startfile", "Popen"):
        assert forbidden not in code, f"the registry does its own discovery via {forbidden}"


def test_the_registry_cannot_launch_anything():
    from void.orchestration import apps
    code = _code_of(apps)
    for forbidden in ("launch(", "execute(", "startfile", "Popen", "system("):
        assert forbidden not in code, f"the registry can {forbidden}"


def test_the_snapshot_carries_counts_not_application_names():
    registry = _registry([FakeEntry("o", "Opera GX")], running=["opera.exe"],
                         preferences={"browser": "Opera GX"})
    snapshot = registry.snapshot()
    assert snapshot["installed"] == 1 and snapshot["running"] == 1
    assert "Opera" not in str(snapshot), "an application name reached the snapshot"


# ================================================================= verification

def test_a_verdict_and_method_must_be_known():
    VerificationResult(Verdict.PASSED, VerificationMethod.WINDOW_STATE)
    with pytest.raises(ValueError):
        VerificationResult("probably", VerificationMethod.WINDOW_STATE)
    with pytest.raises(ValueError):
        VerificationResult(Verdict.PASSED, "crystal ball")


def test_unverified_is_neither_success_nor_failure():
    """The distinction that stops "I could not check" being reported as "it worked" or "it broke"."""
    result = VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE)
    assert result.ok is False and result.failed is False


def test_an_application_is_confirmed_from_the_window_list():
    verifier = Verifier(list_windows=lambda: [{"title": "notes.txt - Notepad", "app": "notepad.exe"}])
    result = verifier.application_present("notepad")
    assert result.ok and result.method == VerificationMethod.WINDOW_STATE


def test_a_populated_window_list_without_the_app_is_positive_evidence_of_absence():
    verifier = Verifier(list_windows=lambda: [{"title": "Inbox", "app": "opera.exe"}])
    assert verifier.application_present("notepad").failed is True


def test_an_empty_window_list_falls_through_to_processes():
    verifier = Verifier(list_windows=lambda: [],
                        running_processes=lambda: {"notepad.exe"})
    result = verifier.application_present("notepad")
    assert result.ok and result.method == VerificationMethod.PROCESS_STATE


def test_with_no_observation_source_the_answer_is_unverified():
    result = Verifier().application_present("notepad")
    assert result.verdict == Verdict.UNVERIFIED and result.method == VerificationMethod.NONE


def test_a_failing_observation_source_yields_unverified_not_a_false_failure():
    verifier = Verifier(list_windows=lambda: (_ for _ in ()).throw(RuntimeError("no desktop")))
    assert verifier.application_present("notepad").verdict == Verdict.UNVERIFIED


def test_focus_is_a_stronger_claim_than_presence():
    verifier = Verifier(active_window=lambda: {"title": "Inbox - Gmail", "app": "opera.exe"})
    assert verifier.application_focused("opera").ok is True
    assert verifier.application_focused("notepad").failed is True


def test_an_artifact_must_exist_and_be_plausibly_sized():
    verifier = Verifier(file_facts=lambda path: {"exists": True, "size_bytes": 5000})
    assert verifier.artifact_created("C:/work/r.pdf").ok is True
    tiny = Verifier(file_facts=lambda path: {"exists": True, "size_bytes": 3})
    result = tiny.artifact_created("C:/work/r.pdf")
    assert result.failed is True and "too small" in result.detail


def test_a_missing_artifact_is_a_failure():
    verifier = Verifier(file_facts=lambda path: {"exists": False, "size_bytes": 0})
    assert verifier.artifact_created("C:/work/r.pdf").failed is True


def test_an_unreadable_location_is_unverified_not_failed():
    """Outside the allowed roots means "I cannot check", which is not "it did not happen"."""
    verifier = Verifier(file_facts=lambda path: None)
    assert verifier.artifact_created("C:/Windows/x.dll").verdict == Verdict.UNVERIFIED


def test_verification_cannot_see_past_the_allowed_roots():
    """The verifier observes through the confined file layer; it is not a way around confinement."""
    seen = []

    def confined(path):
        seen.append(path)
        return None                                             # the file layer refused
    Verifier(file_facts=confined).artifact_created("C:/Windows/System32/config/SAM")
    assert seen == ["C:/Windows/System32/config/SAM"], "the path was not passed to the file layer"


def test_a_tool_result_verdict_is_labelled_as_weak():
    result = Verifier.from_tool_result(True, "launched")
    assert result.ok is True
    assert result.method == VerificationMethod.TOOL_RESULT, "weak evidence was not labelled"


def test_a_verification_summary_is_honest_about_what_was_not_checked():
    results = [VerificationResult(Verdict.PASSED, VerificationMethod.WINDOW_STATE),
               VerificationResult(Verdict.FAILED, VerificationMethod.FILESYSTEM),
               VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE)]
    text = verify_summary(results)
    assert "1 confirmed" in text and "1 did not happen" in text and "1 could not be checked" in text
    assert verify_summary([]) == "nothing was verified"


def test_verification_does_not_screenshot_by_default():
    """Structured state first: the owner's screen is sensitive and a screenshot answers less reliably."""
    from void.orchestration import verify
    code = _code_of(verify).lower()
    for forbidden in ("screenshot", "imagegrab", "cv2", "ocr", "pyautogui"):
        assert forbidden not in code, f"the verifier reaches for {forbidden}"


def test_verification_authorizes_nothing():
    from void.orchestration import verify
    code = _code_of(verify)
    for forbidden in ("RiskGate", ".authorize(", "kill_switch", "execute(", "requires_confirmation"):
        assert forbidden not in code, f"the verifier reaches {forbidden}"


# ================================================================= task state + replanning

def test_the_v3_statuses_were_added_not_substituted():
    """V2 statuses are persisted in the owner's database and asserted in V2 tests; nothing was renamed."""
    for v2_status in ("pending", "running", "paused", "awaiting_confirmation", "blocked",
                      "completed", "failed", "cancelled"):
        assert v2_status in Status.ALL
    for v3_status in (Status.PLANNING, Status.REPLANNING, Status.VERIFYING):
        assert v3_status in Status.ALL and v3_status in Status.RESUMABLE


def test_deciding_phases_are_resumable_not_terminal():
    for status in Status.DECIDING:
        assert status in Status.RESUMABLE and status not in Status.TERMINAL


def test_v3_state_round_trips_through_sqlite(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite")
    task = Task(goal="write a report", status=Status.PLANNING)
    task.v3 = {"intent": "create a document", "applications": ["word"],
               "artifacts": [{"path": "C:/work/r.docx", "kind": "docx", "verified": False}],
               "replans": 1}
    store.save(task)
    loaded = store.load(task.id)
    assert loaded.status == Status.PLANNING
    assert loaded.v3["intent"] == "create a document"
    assert loaded.v3["artifacts"][0]["kind"] == "docx"


def test_a_database_written_before_v3_still_loads(tmp_path):
    """Additive migration: the owner's existing tasks.sqlite must keep working."""
    import sqlite3
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE tasks (id TEXT PRIMARY KEY, goal TEXT NOT NULL,
        status TEXT NOT NULL, messages TEXT NOT NULL, steps INTEGER NOT NULL, result TEXT,
        error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, pending TEXT,
        plan TEXT, current_step INTEGER)""")
    connection.execute("INSERT INTO tasks VALUES ('old1','legacy','completed','[]',1,'done',"
                       "NULL,1.0,2.0,NULL,NULL,0)")
    connection.commit()
    connection.close()
    loaded = TaskStore(path).load("old1")
    assert loaded.goal == "legacy" and loaded.v3 == {}


def test_malformed_v3_state_fails_safely_to_empty(tmp_path):
    import sqlite3
    store = TaskStore(tmp_path / "t.sqlite")
    task = Task(goal="x")
    store.save(task)
    connection = sqlite3.connect(tmp_path / "t.sqlite")
    connection.execute("UPDATE tasks SET v3='{not json' WHERE id=?", (task.id,))
    connection.commit()
    connection.close()
    assert TaskStore(tmp_path / "t.sqlite").load(task.id).v3 == {}


def _ledger_task(statuses):
    task = Task(goal="write a report", status=Status.RUNNING)
    task.plan = [{"index": index, "calls": [], "status": status, "outcome_summary": ""}
                 for index, status in enumerate(statuses)]
    task.current_step = len(statuses) - 1
    return task


def test_the_boundary_is_after_the_last_finished_step():
    assert boundary(_ledger_task(["succeeded", "succeeded", "executing", "pending"])) == 2
    assert boundary(_ledger_task(["pending"])) == 0
    assert boundary(Task(goal="x")) == 0


def test_a_modification_keeps_completed_work_and_supersedes_the_rest():
    task = _ledger_task(["succeeded", "succeeded", "executing", "pending"])
    modification = apply_modification(task, "instead add a costs section")
    assert modification.preserved == 2 and modification.invalidated == 2
    assert [entry["status"] for entry in task.plan] == ["succeeded", "succeeded",
                                                        replan_mod.SUPERSEDED, replan_mod.SUPERSEDED]
    assert task.status == Status.REPLANNING
    assert task.current_step == 2


def test_superseded_is_distinct_from_failed_and_cancelled():
    """"we chose not to do this" must never read as "this failed"."""
    task = _ledger_task(["pending"])
    apply_modification(task, "do something else")
    assert task.plan[0]["status"] == replan_mod.SUPERSEDED
    assert task.plan[0]["status"] not in ("failed", "cancelled")


def test_a_modification_never_undoes_completed_work():
    """Reversing a side effect is a consequential operation with its own authorization."""
    task = _ledger_task(["succeeded"])
    before = dict(task.plan[0])
    apply_modification(task, "actually do it differently")
    assert task.plan[0] == before


def test_a_modification_is_recorded_in_task_state():
    task = _ledger_task(["succeeded", "pending"])
    apply_modification(task, "add a summary")
    assert task.v3["modifications"][0]["asked"] == "add a summary"
    assert task.v3["replans"] == 1


def test_a_terminal_task_cannot_be_amended():
    for status in (Status.COMPLETED, Status.FAILED, Status.CANCELLED):
        task = Task(goal="x", status=status)
        allowed, why = can_modify(task)
        assert allowed is False and status in why
        assert apply_modification(task, "instead do this") is None


def test_an_empty_modification_does_nothing():
    task = _ledger_task(["pending"])
    for empty in ("", "   ", None):
        assert apply_modification(task, empty) is None
    assert task.status == Status.RUNNING


def test_a_modification_request_is_length_bounded():
    task = _ledger_task(["pending"])
    apply_modification(task, "x" * 5000)
    assert len(task.v3["modifications"][0]["asked"]) <= replan_mod.MAX_ASK


@pytest.mark.parametrize("hint,expected", [
    ("unauthorized", FailureKind.DENIED), ("denied", FailureKind.DENIED),
    ("stopped", FailureKind.STOPPED), ("unknown", FailureKind.TARGET_MISSING),
    ("tool_failure", FailureKind.ROUTE_FAILED), ("", FailureKind.UNKNOWN),
    ("something new", FailureKind.UNKNOWN),
])
def test_execution_outcomes_map_onto_failure_kinds(hint, expected):
    """A translation of the outcomes the V2 funnel already produces, not a second classifier."""
    assert classify_failure(hint) == expected


def test_a_denied_action_is_never_retried_around():
    """Replanning must not become a way to badger the owner or route around a refusal."""
    decision = decide_recovery(Task(goal="x"), FailureKind.DENIED)
    assert decision.should_replan is False


def test_the_kill_switch_is_never_replanned_around():
    assert decide_recovery(Task(goal="x"), FailureKind.STOPPED).should_replan is False


def test_a_missing_target_is_not_worth_another_route():
    assert decide_recovery(Task(goal="x"), FailureKind.TARGET_MISSING).should_replan is False


def test_a_route_failure_is_worth_a_different_route():
    assert decide_recovery(Task(goal="x"), FailureKind.ROUTE_FAILED).should_replan is True


def test_replanning_is_bounded_so_it_cannot_loop():
    """"Do not blindly retry forever" needs a number."""
    task = Task(goal="x")
    task.v3 = {"replans": 3}
    decision = decide_recovery(task, FailureKind.ROUTE_FAILED, max_replans=3)
    assert decision.should_replan is False and "looping" in decision.why


def test_a_replan_excludes_what_already_failed():
    """A replan that proposed the route that just failed would be a retry in disguise."""
    decision = decide_recovery(Task(goal="x"), FailureKind.ROUTE_FAILED,
                               attempted_routes=("route-a", "route-b"))
    assert decision.exclude_routes == frozenset({"route-a", "route-b"})


def test_failures_checkpoints_and_artifacts_are_recorded_in_task_state():
    task = Task(goal="x")
    record_failure(task, step=2, kind=FailureKind.ROUTE_FAILED, detail="the locator vanished")
    checkpoint(task, "after research")
    note_artifact(task, "C:/work/r.pdf", "pdf", verified=True)
    assert task.v3["failures"][0]["kind"] == FailureKind.ROUTE_FAILED
    assert task.v3["checkpoints"][0]["label"] == "after research"
    assert task.v3["artifacts"][0]["verified"] is True


def test_an_unknown_failure_kind_is_normalised_rather_than_stored_raw():
    task = Task(goal="x")
    record_failure(task, step=0, kind="made up", detail="")
    assert task.v3["failures"][0]["kind"] == FailureKind.UNKNOWN


def test_replanning_authorizes_nothing():
    code = _code_of(replan_mod)
    # Call-shaped, not bare words: this module legitimately READS the funnel's "unauthorized" outcome name
    # in order to classify a failure, and "authorize" is a substring of it.
    for forbidden in ("RiskGate", ".authorize(", "kill_switch", "tools.execute", "invoke_tool",
                      "requires_confirmation"):
        assert forbidden not in code, f"replanning reaches {forbidden}"


def test_task_state_is_not_memory():
    """The blueprint's distinction, asserted structurally: the task engine must not read memory."""
    from void.core import task as task_mod
    for module in (task_mod, replan_mod):
        code = _code_of(module)
        for forbidden in ("void.memory", "MemoryService", "retrieve("):
            assert forbidden not in code, f"{module.__name__} reaches into memory"


# ================================================================= observability

def test_only_allowlisted_attributes_reach_a_span():
    cleaned = trace_mod._clean({"void.task_id": "abc", "void.tool": "launch_app", "void.ok": True,
                                "goal": "open my bank account", "transcript": "secret",
                                "void.arguments": {"path": "C:/secret"}})
    assert set(cleaned) == {"void.task_id", "void.tool", "void.ok"}


def test_there_is_no_attribute_for_content_at_all():
    """A span travels further than a log line, so content has no slot to travel in."""
    for forbidden in ("goal", "transcript", "arguments", "content", "detail", "text", "memory",
                      "screen", "prompt"):
        assert not any(forbidden in name for name in trace_mod.ATTRIBUTES), forbidden


def test_attribute_types_are_enforced():
    assert trace_mod._clean({"void.step": "three"}) == {}
    assert trace_mod._clean({"void.ok": "yes"}) == {}
    assert trace_mod._clean({"void.step": 3}) == {"void.step": 3}


def test_string_attributes_are_bounded():
    cleaned = trace_mod._clean({"void.tool": "x" * 500})
    assert len(cleaned["void.tool"]) <= trace_mod.MAX_VALUE


def test_a_span_never_changes_control_flow():
    ran = []
    with trace_mod.span(trace_mod.Span.ACTION, **{"void.tool": "launch_app", "goal": "leak"}):
        ran.append(True)
    assert ran == [True]


def test_a_span_survives_a_broken_tracer(monkeypatch):
    monkeypatch.setattr(trace_mod, "_tracer",
                        lambda: (_ for _ in ()).throw(RuntimeError("no sdk")))
    ran = []
    with trace_mod.span(trace_mod.Span.TASK):
        ran.append(True)
    assert ran == [True], "a telemetry failure stopped the work it was measuring"


def test_a_missing_opentelemetry_is_not_an_error(monkeypatch):
    monkeypatch.setattr(trace_mod, "_tracer", lambda: None)
    assert trace_mod.available() is False
    with trace_mod.span(trace_mod.Span.ACTION) as active:
        assert active is None


def test_an_event_projects_onto_attributes_without_its_detail():
    log = EventLog()
    event = log.emit(TaskEventKind.ACTION_EXECUTED, "task-1", tool="launch_app", ok=True,
                     duration_s=0.05, detail="Opera was already open")
    attributes = trace_mod.record(event, task_status="running")
    assert attributes["void.task_id"] == "task-1" and attributes["void.ok"] is True
    assert "Opera" not in str(attributes), "a human detail line reached the trace"


def test_the_existing_perf_stream_is_not_replaced():
    """void/perf stays the local privacy-by-construction record; this is an additional mapping."""
    code = _code_of(trace_mod)
    assert "void.perf" not in code and "perf.emit" not in code


def test_telemetry_is_never_required_for_ordinary_execution():
    """Replaces an assertion that no OpenTelemetry dependency existed.

    That premise was deliberately retired: V3 added the official SDK and OTLP exporter so spans can
    actually be collected. The invariant that matters now is not "no dependency" but "no requirement" -
    V.O.I.D must run identically with telemetry off, missing, or broken. Three cases, all of which must
    leave the caller unaffected.
    """
    from void.obs import TelemetryPolicy, configure
    from void.orchestration.trace import Span, span

    # Off: nothing configured, nothing opened, and spans still work as no-ops.
    status = configure(TelemetryPolicy(enabled=False))
    assert status.enabled is False and status.exporting is False and status.degraded is False
    with span(Span.TASK, **{"void.task_id": "t1"}):
        pass

    # Broken exporter: degradation, not an exception, and the body of a span still runs.
    class _Exploding:
        def export(self, spans):
            raise RuntimeError("collector exploded")

        def shutdown(self):
            raise RuntimeError("no")

        def force_flush(self, timeout_millis=0):
            raise RuntimeError("no")

    configure(TelemetryPolicy(enabled=True, service_name="t"), exporter=_Exploding())
    ran = False
    with span(Span.ACTION, **{"void.tool": "x"}):
        ran = True
    assert ran, "a failing exporter must not prevent the measured work from running"

    from void.obs import shutdown as telemetry_shutdown
    telemetry_shutdown()


def test_the_telemetry_dependency_is_pinned_and_official():
    """A floating telemetry version is a supply-chain risk and a data-model mismatch risk.

    The SDK and exporter must agree with the already-installed API, so the version is pinned in one place
    and asserted here against what is actually importable.
    """
    from void.obs import OTEL_VERSION
    from importlib.metadata import version

    assert OTEL_VERSION == "1.45.0"
    for package in ("opentelemetry-api", "opentelemetry-sdk",
                    "opentelemetry-exporter-otlp-proto-http"):
        assert version(package) == OTEL_VERSION, f"{package} must match the pinned API version"
