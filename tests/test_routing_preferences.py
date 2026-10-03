"""Preference-aware routing: a default that an instruction can overrule, and nothing more.

Two things are under test. First the precedence chain, because the failure the owner would actually
notice is V.O.I.D answering "open Gmail in Edge" with Opera GX. Second - and more important - that a
preference stays a *signal*: it chooses between routes the security layer already permitted, and can
neither select something unavailable nor relax anything.

The integration is deliberately inside the EXISTING resolver. There is no second routing engine here:
preferences arrive as :attr:`WorldState.preferences`, which ``routes.py`` already consulted, and the
new part is only that an utterance can contradict them.
"""
from __future__ import annotations

import pytest

from void.orchestration.apps import PREFERENCE_KEYS
from void.orchestration.overrides import (ALIASES, OVERRIDABLE_ROLES, browser_vocabulary,
                                          extract_overrides, role_vocabularies)
from void.orchestration.routes import Route, RouteKind, WorldState
from void.state import StateStore

BROWSERS = frozenset({"opera gx", "opera", "edge", "msedge", "chrome", "chromium"})
VOCAB = {"browser": BROWSERS}


# --------------------------------------------------------------------------- extraction

@pytest.mark.parametrize("goal, expected", [
    ("Open Gmail in Edge", "edge"),
    ("open this in chrome", "chrome"),
    ("Use Opera for this", "opera"),
    ("open youtube with opera gx", "opera gx"),
    ("open gmail using microsoft edge", "edge"),
    ("use ms edge for this", "edge"),
    ("Open this in Google Chrome", "chrome"),
])
def test_an_explicit_browser_choice_is_heard(goal, expected):
    assert extract_overrides(goal, VOCAB) == {"browser": expected}


@pytest.mark.parametrize("goal", [
    "Open Gmail",
    "open youtube",
    "research the EU AI Act",
    "open this chart",
    "Open the invoice in Documents",          # a folder, not an application
    "open it in the background",              # not an application at all
    "Edge is slow, open Gmail",               # a mention, not a choice
    "chrome keeps crashing",
    "",
    "   ",
])
def test_an_ordinary_request_yields_no_override(goal):
    """A false positive sends the owner's work to the wrong application, so this errs towards nothing."""
    assert extract_overrides(goal, VOCAB) == {}


def test_an_alias_resolves_to_the_name_the_browser_layer_drives():
    """Returning "microsoft edge" where the launcher expects "edge" would route nowhere."""
    assert extract_overrides("open it in microsoft edge", VOCAB) == {"browser": "edge"}
    assert set(ALIASES.values()) <= BROWSERS | {"opera gx"}


def test_an_alias_for_an_unavailable_browser_yields_nothing():
    """An override may choose between browsers V.O.I.D can drive; it cannot name one into existence."""
    assert extract_overrides("open it in microsoft edge", {"browser": frozenset({"opera"})}) == {}


def test_the_longer_browser_name_wins():
    """Picking "opera" when the owner said "opera gx" would silently start a different browser."""
    assert extract_overrides("open it with opera gx", VOCAB) == {"browser": "opera gx"}


def test_the_vocabulary_comes_from_the_browser_layer_not_a_second_list():
    """Reused so that teaching V.O.I.D a new browser does not require editing routing too."""
    from void.browser.playwright_adapter import _BROWSERS
    vocabulary = browser_vocabulary()
    for row in _BROWSERS:
        assert str(row[0]).strip().lower() in vocabulary


def test_only_roles_the_registry_understands_are_overridable():
    assert OVERRIDABLE_ROLES <= PREFERENCE_KEYS


def test_roles_without_capability_data_are_not_claimed():
    """Recognising an override for a role V.O.I.D cannot act on would be a promise it cannot keep."""
    vocabularies = role_vocabularies()
    assert set(vocabularies) == {"browser"}
    assert vocabularies["browser"], "the one claimed role must actually have a vocabulary"


# --------------------------------------------------------------------------- precedence

def test_a_stored_preference_is_used_when_nothing_was_said():
    state = WorldState(preferences={"browser": "opera gx"})
    assert state.preferred("browser") == "opera gx"
    assert state.overridden("browser") is False


def test_an_explicit_instruction_outranks_a_stored_preference():
    """The failure the owner would notice: "open Gmail in Edge" answered with Opera GX."""
    state = WorldState(preferences={"browser": "opera gx"}, overrides={"browser": "edge"})
    assert state.preferred("browser") == "edge"
    assert state.overridden("browser") is True


def test_an_override_for_one_role_does_not_affect_another():
    state = WorldState(preferences={"browser": "opera gx", "editor": "vs code"},
                       overrides={"browser": "edge"})
    assert state.preferred("browser") == "edge"
    assert state.preferred("editor") == "vs code"


def test_no_preference_and_no_override_means_no_opinion():
    assert WorldState().preferred("browser") is None
    assert WorldState().overridden("browser") is False


def test_a_blank_preference_is_treated_as_absent():
    assert WorldState(preferences={"browser": "   "}).preferred("browser") is None
    assert WorldState(overrides={"browser": ""}).preferred("browser") is None


def test_a_non_string_preference_is_ignored_rather_than_crashing():
    """Preferences arrive from config and from a database; neither is guaranteed well-typed."""
    assert WorldState(preferences={"browser": 42}).preferred("browser") is None
    assert WorldState(overrides={"browser": None}, preferences={"browser": "opera"}
                      ).preferred("browser") == "opera"


def test_an_override_is_not_remembered_as_a_new_default(tmp_path):
    """Saying "in Edge" once must not change which browser V.O.I.D reaches for tomorrow, which is why
    overrides and preferences are separate fields rather than one merged map."""
    store = StateStore(tmp_path / "state.sqlite")
    store.set_preference("browser", "opera gx")
    state = WorldState(preferences=store.as_routing_map(), overrides={"browser": "edge"})
    assert state.preferred("browser") == "edge"
    assert store.preference("browser").value == "opera gx", "the stored default was altered"


# --------------------------------------------------------------------------- preferences are not permissions

def test_a_preference_cannot_select_a_route_whose_capability_is_absent():
    """Security and availability are enforced before preferences are consulted: a route requiring a
    capability the machine lacks is dropped from the field, so preferring it changes nothing."""
    from void.orchestration.routes import RouteResolver

    class _Provider:
        name = "test"

        def propose(self, goal, state):
            return [Route(kind=RouteKind.BROWSER_LAUNCH, target="gmail",
                          requires=frozenset({"browser"}), why="preferred browser")]

    state = WorldState(capabilities=frozenset({"native_app"}),      # no browser capability
                       preferences={"browser": "opera gx"},
                       overrides={"browser": "edge"})
    assert RouteResolver([_Provider()]).candidates("open gmail in edge", state) == []


def test_preferences_and_overrides_carry_no_authorization_fields():
    """A preference is a routing signal. There is nowhere in this structure to put a permission."""
    state = WorldState(preferences={"browser": "opera gx"}, overrides={"browser": "edge"})
    for forbidden in ("authorize", "allow", "risk", "confirmed", "granted", "elevated", "token"):
        assert not hasattr(state, forbidden)
    assert set(state.preferences) <= PREFERENCE_KEYS | {"browser"}


def test_the_override_module_grants_nothing_and_executes_nothing():
    import ast
    import inspect
    import void.orchestration.overrides as module
    code = ast.unparse(ast.parse(inspect.getsource(module)))
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "RiskGate", "kill_switch",
                      "authorize", "Popen"):
        assert forbidden not in code, f"the override module references {forbidden!r}"


def test_an_override_cannot_invent_a_role():
    """Only the closed role vocabulary is read; a made-up role in an utterance yields nothing."""
    assert extract_overrides("open it in Edge", {"shell": BROWSERS}) == {}


# --------------------------------------------------------------------------- existing state still wins where it should

def test_an_open_tab_in_the_preferred_browser_is_still_recognised():
    """The resolver already preferred reusing an existing session; preferences must not have broken it."""
    state = WorldState(open_tabs=(("opera gx", "https://mail.google.com/", "Gmail"),),
                       capabilities=frozenset({"browser"}),
                       preferences={"browser": "opera gx"})
    assert state.tabs_matching("mail.google.com")
    assert state.preferred("browser") == "opera gx"


def test_tab_matching_is_unchanged_by_the_new_fields():
    state = WorldState(open_tabs=(("edge", "https://youtube.com/watch", "YouTube"),),
                       overrides={"browser": "chrome"})
    assert state.tabs_matching("youtube")
    assert state.tabs_matching("pinterest") == ()


def test_running_application_detection_is_unchanged():
    state = WorldState(running_apps=frozenset({"opera.exe"}), overrides={"browser": "edge"})
    assert state.is_running("opera.exe") and state.is_running("opera")
    assert not state.is_running("chrome.exe")


# --------------------------------------------------------------------------- end to end through the assistant

def test_the_assistant_feeds_stored_preferences_and_overrides_into_routing(tmp_path, monkeypatch):
    """The wiring, not the pieces: a goal string in, a resolved preference out."""
    from void.app import Assistant
    assistant = Assistant()
    try:
        plain = assistant.world_state("Open Gmail")
        assert plain.preferred("browser") == "opera gx", "the shipped default should apply"
        assert plain.overridden("browser") is False

        explicit = assistant.world_state("Open Gmail in Edge")
        assert explicit.preferred("browser") == "edge"
        assert explicit.overridden("browser") is True
    finally:
        pass


def test_a_broken_state_store_does_not_stop_routing(monkeypatch):
    """Config preferences must still apply if the database cannot be read."""
    from void.app import Assistant
    assistant = Assistant()

    def exploding():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(assistant.state, "as_routing_map", exploding)
    state = assistant.world_state("Open Gmail")
    assert state.preferred("browser") == "opera gx", "config preferences should survive"
