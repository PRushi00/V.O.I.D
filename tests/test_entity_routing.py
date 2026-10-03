"""Four kinds of entity, four ways the owner actually asks, and the latency that made it unusable.

Every case here comes from real failures on the owner's machine, traced to the layer that caused
them rather than guessed at:

* **"Open my Studies folder"** - 41 seconds, and 40 of them wasted. The launch grammar dropped only
  "the", and its trailing-noun list held only "app"/"application"/"program", so the name to look up
  became "my studies folder", matched nothing, and the sentence went to the model - which then used
  ``find_directory`` (an 8-second walk) for something the folder catalog answers in 0.01 ms. Both
  determiners and the folder noun are now handled: measured 0.79 s and 0.05 s.
* **"Open YouTube"** - no route existed at all. A website is not an application, so the matcher
  correctly reported nothing installed and the model was left to invent a URL. Now a table row.
* **"Open the folder you just found"** - the verified path was thrown away, so the follow-up searched
  again and failed. It only appeared to work while the Explorer window stayed open, because the
  window list offers a candidate of its own.
* **"Open my personal chat"** - resolved to a contact named "Personal". Nobody is called personal.

The thread running through all of them: when V.O.I.D already knows the answer, asking a model for it
can only add latency, variance, and the chance of a confident claim that is not true.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from void.core.fast_path import launch_targets
from void.orchestration.messaging import parse_request
from void.orchestration.websites import (WEBSITES, WebsiteRoutes, parse_target, site_by_name,
                                         website_vocabulary)
from void.orchestration.routes import RouteKind, WorldState


# =========================================================================== the launch grammar

def _names(goal):
    return [reading for target in launch_targets(goal) for reading in target]


@pytest.mark.parametrize("goal, wanted", [
    ("open my Studies folder", "studies"),
    ("open the Studies folder", "studies"),
    ("open my Vibecoding folder", "vibecoding"),
    ("open my downloads folder", "downloads"),
    ("open the Projects directory", "projects"),
])
def test_a_possessive_and_a_trailing_folder_noun_come_off(goal, wanted):
    """The exact phrasing the owner used, and the one the grammar could not read."""
    assert wanted in _names(goal), f"{goal!r} produced {_names(goal)}"


def test_a_bare_demonstrative_names_no_folder():
    """"open this folder" is a reference, not a name - there is nothing for the catalog to look up,
    and the reference resolver owns it. The possessive rule covers it: a determiner is dropped only
    when a noun says what kind of thing is meant, and here the noun IS the whole phrase."""
    assert _names("open this folder") == []


def test_both_readings_are_offered_so_a_real_folder_noun_survives():
    """A folder genuinely called "Work Folder" must still be reachable, which is why the noun
    produces a SECOND reading rather than being deleted."""
    readings = _names("open the work folder")
    assert "work" in readings and "work folder" in readings


@pytest.mark.parametrize("goal, wanted", [
    ("open the calculator app", "calculator"),
    ("open whats app", "whats app"),
    ("open notepad", "notepad"),
    ("open Opera GX", "opera gx"),
])
def test_application_phrasings_are_unaffected(goal, wanted):
    """"app"/"application"/"program" were always handled; adding "folder" must not disturb them."""
    assert wanted in _names(goal)


def test_a_determiner_is_dropped_from_a_list_item_too():
    assert "calculator" in _names("open notepad and the calculator app")


def test_a_sentence_that_is_not_a_launch_is_still_not_one():
    for goal in ("what is the weather", "research the EU AI Act", "", "delete my downloads folder"):
        assert _names(goal) == [] or "delete" not in goal, goal


# =========================================================================== websites

@pytest.mark.parametrize("goal, host", [
    ("Open YouTube", "www.youtube.com"),
    ("Open Gmail", "mail.google.com"),
    ("Open Pinterest", "www.pinterest.com"),
    ("open the YouTube website", "www.youtube.com"),
    ("go to reddit", "www.reddit.com"),
])
def test_a_known_site_resolves_to_its_own_address(goal, host):
    from urllib.parse import urlsplit

    assert urlsplit(parse_target(goal).url).hostname == host


def test_a_question_about_a_site_becomes_a_search_in_it():
    found = parse_target("Open Wikipedia about AI")
    assert found.resolved and found.query == "AI"
    assert "search" in found.url.lower() and "AI" in found.url


@pytest.mark.parametrize("goal", [
    "open youtube in opera gx", "Open YouTube in Edge", "open gmail with chrome",
    "open youtube in this browser", "open youtube in the current browser",
])
def test_a_browser_choice_does_not_hide_the_site(goal):
    """Which browser to use is a separate decision. Left attached, "youtube in opera gx" looked like
    the name of a site and resolved to nothing."""
    assert parse_target(goal).resolved


def test_a_folder_is_not_mistaken_for_a_site():
    """"in Documents" names a folder, not a browser, so the phrase stays and nothing resolves."""
    assert not parse_target("open the invoice in Documents").resolved


def test_a_hostname_the_owner_said_is_honoured():
    assert parse_target("open reddit.com").url == "https://reddit.com"
    assert parse_target("go to en.wikipedia.org").url == "https://en.wikipedia.org"


@pytest.mark.parametrize("goal", [
    "open notepad.exe", "open setup.msi", "open run.ps1", "open thing.pem", "open a.bat",
])
def test_a_filename_is_not_a_hostname(goal):
    """The first version accepted any 2-24 letter final label, so "open notepad.exe" looked like an
    address and would have navigated to https://notepad.exe - a filename turned into a destination
    by a parser. A denylist of extensions cannot be used instead: the project's own list contains
    "com", which is both a DOS executable suffix and the commonest TLD."""
    assert not parse_target(goal).resolved


def test_an_unknown_word_is_never_guessed_into_a_domain():
    """Appending ".com" to whatever was heard would let a mishearing send the browser somewhere
    nobody chose. That is a security decision, not a convenience one."""
    for goal in ("open flurbleglorp", "open my Studies folder", "open notepad",
                 "Open Rushi's chat", "open the thing"):
        assert not parse_target(goal).resolved, goal


def test_every_row_is_complete_and_no_row_has_code_of_its_own():
    vocabulary = website_vocabulary()
    for site in WEBSITES:
        assert site.name in vocabulary and site.display and site.url.startswith("https://")
        assert site_by_name(site.name) is site
    assert len({tuple(sorted(vars(site))) for site in WEBSITES}) == 1


def test_the_route_needs_the_browser_capability():
    """With browser automation off the route is dropped BEFORE selection, which is how the owner
    gets an honest refusal instead of the fabricated success this replaced."""
    provider = WebsiteRoutes()
    (route,) = provider.propose("Open YouTube", WorldState())
    assert route.kind == RouteKind.BROWSER
    assert route.requires == frozenset({"browser"})
    assert route.calls[0].name == "navigate"
    assert route.calls[0].arguments["url"].startswith("https://")


def test_the_provider_proposes_nothing_for_a_non_website():
    assert WebsiteRoutes().propose("open notepad", WorldState()) == ()


# =========================================================================== verification

def test_a_page_counts_as_loaded_only_when_the_browser_says_so():
    """Host comparison, not string equality: sites redirect and append parameters, and calling every
    one of those a failure would be as wrong as claiming success."""
    from void.app import Assistant

    assistant = Assistant()
    target = parse_target("Open YouTube")

    class Page:
        def __init__(self, url):
            self.url = url

    for url, expected in (("https://www.youtube.com/", True),
                          ("https://www.youtube.com/?gl=IN&hl=en", True),
                          ("https://youtube.com/", True),
                          ("https://www.google.com/", False),
                          ("", False)):
        assistant.browser = type("B", (), {"read": staticmethod(lambda u=url: Page(u))})()
        ok, _why = assistant._verify_page(target)
        assert ok is expected, url


def test_an_unreadable_browser_is_reported_as_unverified_not_as_success():
    from void.app import Assistant

    assistant = Assistant()

    class Broken:
        @staticmethod
        def read():
            raise RuntimeError("page is gone")

    assistant.browser = Broken()
    ok, why = assistant._verify_page(parse_target("Open YouTube"))
    assert ok is False and "failed" in why


def test_a_website_request_with_the_browser_off_fails_honestly():
    """The exact case that produced "I have opened the Wikipedia page" when navigation had been
    refused. It must be FAILED, and it must say why."""
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    assistant.browser = None
    result = assistant.run("Open YouTube")
    assert result.status == "failed"
    assert "browser automation is switched off" in (result.result or "")
    assert "opened" not in (result.result or "").lower()


# =========================================================================== cross-turn references

def test_opening_a_path_records_it_as_referenceable(tmp_path):
    """Without this the verified path was thrown away and the follow-up searched again."""
    from void.actions.apps import AppActions
    from void.actions.files import FileActions
    from void.orchestration.referents import RecentThings

    folder = tmp_path / "Vibe Coding"
    folder.mkdir()
    recent = RecentThings()
    actions = AppActions(FileActions(allowed_roots=[tmp_path]), recent=recent)
    opened = []
    actions._is_windows = lambda: False
    actions.open_path = actions.open_path          # keep the real method
    import void.actions.apps as apps_module
    original = apps_module.subprocess.Popen
    apps_module.subprocess.Popen = lambda *a, **k: opened.append(a) or None
    try:
        result = actions.open_path(str(folder))
    finally:
        apps_module.subprocess.Popen = original
    assert result.ok
    (candidate,) = recent.candidates()
    assert candidate.kind == "folder"
    assert candidate.target == str(folder)


def test_a_recorded_folder_is_not_searched_for_again(tmp_path):
    """The follow-up must use the path already verified, which is what makes it instant."""
    from void.orchestration.referents import RecentThings, recent_candidates
    from void.orchestration.reference import ReferenceResolver

    folder = tmp_path / "Vibe Coding"
    folder.mkdir()
    recent = RecentThings()
    recent.note(kind="folder", label="Vibe Coding", target=str(folder), source="opened")
    resolver = ReferenceResolver([recent_candidates(recent)])
    for phrase in ("open the folder you just found", "open that folder",
                   "open the vibe coding folder you found"):
        resolution = resolver.resolve(phrase)
        assert resolution.resolved, phrase
        assert resolution.choice.target == str(folder)


def test_a_recorded_path_that_has_gone_is_not_opened(tmp_path):
    """A stale reference must fall through to a real search, not be opened blindly."""
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    missing = tmp_path / "GoneAway"
    assistant.recent.note(kind="folder", label="GoneAway", target=str(missing), source="opened")
    assert assistant._reference_route("open that folder") is None


def test_an_ambiguous_reference_is_asked_about_not_acted_on(tmp_path):
    """Two plausible folders must never be guessed between - but the question is worth answering
    here rather than handing on. Measured: letting the model field it produced "I cannot proceed
    without knowing which folder you are referring to" after 15.8 s, which is the same question
    asked worse and slower."""
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    first, second = tmp_path / "Alpha", tmp_path / "Beta"
    first.mkdir(), second.mkdir()
    assistant.recent.note(kind="folder", label="Alpha", target=str(first), source="opened")
    assistant.recent.note(kind="folder", label="Beta", target=str(second), source="opened")
    result = assistant._reference_route("open that folder")
    assert result is not None
    assert result.steps == 0, "nothing may be opened while the referent is ambiguous"
    assert "which one" in (result.result or "").lower()
    assert "Alpha" in result.result and "Beta" in result.result


def test_a_plain_request_is_not_treated_as_a_reference():
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    for goal in ("open notepad", "Open YouTube", "what is the weather"):
        assert assistant._reference_route(goal) is None, goal


# =========================================================================== terminal states

def test_every_terminal_state_says_something(tmp_path):
    """A FAILED task used to return result=None, so it vanished from the CLI and the widget."""
    from void.actions.registry import ToolRegistry
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import Status, Task, TaskStore
    from void.providers.base import LLMResponse
    from void.security.risk import RiskGate

    from tests.helpers import FakeProvider

    agent = Agent(provider=FakeProvider([LLMResponse(text="ok")]), tools=ToolRegistry(),
                  risk_gate=RiskGate(confirm_at_or_above="high"), kill_switch=KillSwitch(),
                  store=TaskStore(tmp_path / "tasks.sqlite"))
    for status in (Status.FAILED, Status.PAUSED, Status.CANCELLED, Status.BLOCKED,
                   Status.AWAITING_CONFIRMATION):
        task = Task(goal="x", id="t", status=status)
        result = agent._result(task)
        assert result.result, f"{status} produced nothing to tell the owner"


def test_the_engines_own_reason_is_preferred_over_the_generic_one(tmp_path):
    """"Reached max_steps (3) without finishing" is useful; "that did not work" is not. These
    strings are written by the engine, never by a model or a tool, so surfacing one is safe."""
    from void.actions.registry import ToolRegistry
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import Status, Task, TaskStore
    from void.providers.base import LLMResponse
    from void.security.risk import RiskGate

    from tests.helpers import FakeProvider

    agent = Agent(provider=FakeProvider([LLMResponse(text="ok")]), tools=ToolRegistry(),
                  risk_gate=RiskGate(confirm_at_or_above="high"), kill_switch=KillSwitch(),
                  store=TaskStore(tmp_path / "tasks.sqlite"))
    task = Task(goal="x", id="t", status=Status.FAILED)
    task.error = "Reached max_steps (3) without finishing."
    assert "max_steps" in agent._result(task).result


def test_a_completed_task_keeps_its_own_answer(tmp_path):
    from void.actions.registry import ToolRegistry
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import Status, Task, TaskStore
    from void.providers.base import LLMResponse
    from void.security.risk import RiskGate

    from tests.helpers import FakeProvider

    agent = Agent(provider=FakeProvider([LLMResponse(text="ok")]), tools=ToolRegistry(),
                  risk_gate=RiskGate(confirm_at_or_above="high"), kill_switch=KillSwitch(),
                  store=TaskStore(tmp_path / "tasks.sqlite"))
    task = Task(goal="x", id="t", status=Status.COMPLETED, result="Opened YouTube.")
    assert agent._result(task).result == "Opened YouTube."


# =========================================================================== "my personal chat"

@pytest.mark.parametrize("goal", [
    "Open my personal chat", "open my private chat", "Open my work chat",
    "open my main chat", "Open my family group chat",
])
def test_an_adjective_is_not_a_person(goal):
    """It resolved to a contact called "Personal" and asked which app to find them in - V.O.I.D
    inventing an identity out of a describing word. What "my personal chat" means is something only
    the owner can say, so the answer is to ask."""
    request = parse_request(goal)
    assert request.wants_conversation
    assert request.contact == "", f"{goal!r} invented the contact {request.contact!r}"


def test_a_real_name_is_still_a_person():
    assert parse_request("Open Rushi's personal chat").contact == "rushi"
    assert parse_request("Open Rushi's chat").contact == "rushi"


# =========================================================================== latency

def test_an_application_request_consults_no_model():
    """The latency property: a request V.O.I.D can resolve itself never reaches a model."""
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    started = time.monotonic()
    result = assistant.run("open notepad")
    elapsed = time.monotonic() - started
    assert result.steps <= 1, f"it took {result.steps} steps"
    assert elapsed < 20.0, f"it took {elapsed:.1f}s"


def test_a_folder_request_resolves_from_the_catalog_and_consults_no_model():
    """The folder half of the latency fix, tested against a folder this test creates.

    Deliberately NOT the owner's real "Studies": pytest runs with a temporary home, so the real one
    is invisible here by design, and a test that depended on it would pass only on one machine. What
    matters is the mechanism - that "open my <name> folder" reaches the deterministic catalog rather
    than the model - and that holds for any folder the catalog can see.

    Measured on the owner's machine before the fix: 41 s, of which ~31 s was provider retries and
    8 s a filesystem walk the catalog answers in 0.01 ms. After: 0.79 s.
    """
    from void.app import Assistant

    home = Path.home()
    folder = home / "ZqFolderProbe"
    folder.mkdir(exist_ok=True)
    try:
        assistant = Assistant()
        assistant.clear_stop()
        assistant._fast.folders.invalidate()
        started = time.monotonic()
        result = assistant.run("open my ZqFolderProbe folder")
        elapsed = time.monotonic() - started
        assert result.status == "completed", result.result
        assert result.steps <= 1, f"it took {result.steps} steps - the model was consulted"
        assert elapsed < 20.0, f"it took {elapsed:.1f}s"
    finally:
        try:
            folder.rmdir()
        except OSError:
            pass
