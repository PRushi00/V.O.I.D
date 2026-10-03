"""Reference resolution: "open this chart", "that spreadsheet", "Rushi's chat".

The blueprint requires this to be general machinery rather than a special-cased command, so the tests are
written to fail if it ever stops being general: a referent is only ever reachable through a provider, and
the resolver is never given knowledge of what a chart or a chat is.

The other property under test is the one that protects the owner: when a phrase genuinely could mean
several things, V.O.I.D asks. A test that accepted a confident guess here would be testing the wrong thing.
"""
from __future__ import annotations

import time

import pytest

from void.actions.reference import ReferenceActions
from void.orchestration.reference import (DECISIVE_MARGIN, Candidate, Reference, ReferenceResolver,
                                          identifies,
                                          parse_reference, score)
from void.orchestration.referents import (RecentThings, recent_candidates, tab_candidates,
                                          window_candidates)

NOW = 1_000_000.0


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("phrase, kind, noun", [
    ("open this chart", "document", "chart"),
    ("bring up that spreadsheet", "document", "spreadsheet"),
    ("open the document I was looking at", "document", "document"),
    ("go back to the tab I had open", "tab", "tab"),
    ("open Rushi's chat", "conversation", "chat"),
    ("show me that window again", "window", "window"),
])
def test_referring_expressions_are_parsed(phrase, kind, noun):
    reference = parse_reference(phrase)
    assert reference.resolvable
    assert reference.kind == kind
    assert reference.noun == noun


@pytest.mark.parametrize("phrase", [
    "open notepad", "launch chrome", "what is the weather", "set a timer for ten minutes",
    "", "   ", "play some music",
])
def test_ordinary_commands_are_not_treated_as_references(phrase):
    """A launch request must not be diverted into resolution - that would hijack normal handling."""
    assert not parse_reference(phrase).resolvable


@pytest.mark.parametrize("phrase", [
    "create a chart of Q3 revenue", "make a deck about pricing", "write a report on sales",
    "generate a spreadsheet of expenses",
])
def test_creation_requests_are_not_references(phrase):
    """"Create a chart" names a chart to build. Resolving it would open an old one instead."""
    reference = parse_reference(phrase)
    assert reference.creating
    assert not reference.resolvable


def test_deixis_overrides_a_creation_verb():
    """"Make a chart like this one" genuinely refers to something."""
    reference = parse_reference("make a chart like this one")
    assert reference.creating and reference.deictic and reference.resolvable


def test_possessive_qualifier_excludes_the_leading_verb():
    """The possessive pattern can start before the name does; filler must be stripped from it."""
    assert parse_reference("open Rushi's chat").qualifier == "rushi"
    assert parse_reference("bring up Priya's conversation").qualifier == "priya"


def test_recency_phrases_are_recognised():
    for phrase in ["the file I was looking at", "the one I had open", "that thing from earlier"]:
        assert parse_reference(phrase).recency


# --------------------------------------------------------------------------- fixtures

class _Window:
    def __init__(self, handle, title, process, active=False):
        self.handle, self.title, self.process, self.active = handle, title, process, active


class _Desktop:
    def __init__(self, windows):
        self._windows = windows

    def available(self):
        return True

    def windows(self):
        return list(self._windows)


class _Tab:
    def __init__(self, handle, title, url, active=False):
        self.handle, self.title, self.url, self.active = handle, title, url, active


class _Browser:
    def __init__(self, tabs):
        self._tabs = tabs

    def available(self):
        return True

    def tabs(self):
        return list(self._tabs)


@pytest.fixture
def recent():
    store = RecentThings()
    store.note("document", "Q3 revenue chart.pptx", r"C:\docs\Q3 revenue chart.pptx",
               source="artifacts", produced=True)
    store.note("document", "old budget.xlsx", r"C:\docs\old budget.xlsx", source="files")
    return store


@pytest.fixture
def desktop():
    return _Desktop([
        _Window("1001", "Rushi - WhatsApp", "whatsapp.exe"),
        _Window("1002", "Priya - WhatsApp", "whatsapp.exe"),
        _Window("1003", "Inbox - Outlook", "outlook.exe", active=True),
    ])


@pytest.fixture
def resolver(recent, desktop):
    return ReferenceResolver([recent_candidates(recent), window_candidates(desktop)])


# --------------------------------------------------------------------------- resolution

def test_open_this_chart_resolves_to_the_chart(resolver):
    """The blueprint's headline example, resolved without any chart-specific code path."""
    resolution = resolver.resolve("open this chart", now=NOW)
    assert resolution.resolved
    assert "chart" in resolution.choice.label.lower()
    assert resolution.reasons                    # and it can say why


def test_open_that_spreadsheet_resolves_by_file_type(resolver):
    """Nothing in the .xlsx's name says "spreadsheet"; the extension implies it."""
    resolution = resolver.resolve("open that spreadsheet", now=NOW)
    assert resolution.resolved
    assert resolution.choice.label == "old budget.xlsx"


def test_open_a_persons_chat_resolves_by_name(resolver):
    resolution = resolver.resolve("open Rushi's chat", now=NOW)
    assert resolution.resolved
    assert resolution.choice.label == "Rushi - WhatsApp"
    assert resolution.choice.kind == "conversation"


def test_an_ambiguous_reference_asks_instead_of_guessing(resolver):
    """Two equally plausible chats: the correct behaviour is a question."""
    resolution = resolver.resolve("open that chat", now=NOW)
    assert not resolution.resolved
    assert resolution.ambiguous
    question = resolution.question()
    assert "Rushi" in question and "Priya" in question


def test_nothing_referenceable_resolves_to_nothing():
    resolution = ReferenceResolver([]).resolve("open this chart", now=NOW)
    assert resolution.empty
    assert not resolution.resolved and not resolution.ambiguous


def test_a_failing_provider_does_not_break_resolution(recent):
    """One dead layer costs candidates, not the capability."""
    def broken():
        raise RuntimeError("layer is down")

    resolver = ReferenceResolver([broken, recent_candidates(recent)])
    assert resolver.resolve("open this chart", now=NOW).resolved


def test_a_provider_returning_rubbish_is_ignored():
    def rubbish():
        return [None, "a string", 42, Candidate(kind="document", label="x", target="")]

    assert ReferenceResolver([rubbish]).resolve("open this chart", now=NOW).empty


def test_resolution_is_extended_by_adding_a_provider_not_by_editing_the_resolver():
    """The generality requirement, asserted directly."""
    resolver = ReferenceResolver([])
    assert resolver.resolve("open that mixtape", now=NOW).empty
    resolver.add_provider(lambda: [Candidate(kind="document", label="summer mixtape.pptx",
                                             target="t1", source="invented")])
    resolution = resolver.resolve("open that mixtape", now=NOW)
    assert resolution.resolved and resolution.choice.target == "t1"


def test_tabs_are_referenceable_through_the_browser_provider():
    browser = _Browser([_Tab("t1", "Pull requests - GitHub", "https://github.com/pulls", active=True),
                        _Tab("t2", "Gmail", "https://mail.google.com")])
    resolver = ReferenceResolver([tab_candidates(browser)])
    resolution = resolver.resolve("go back to that GitHub tab", now=NOW)
    assert resolution.resolved
    assert resolution.choice.target == "t1"


def test_a_disabled_layer_offers_nothing():
    class _Off:
        def available(self):
            return False

        def tabs(self):
            raise AssertionError("must not be asked when unavailable")

    assert ReferenceResolver([tab_candidates(_Off())]).resolve("that tab", now=NOW).empty


# --------------------------------------------------------------------------- scoring

def test_the_foreground_window_wins_a_bare_reference(desktop):
    resolver = ReferenceResolver([window_candidates(desktop)])
    resolution = resolver.resolve("the window I was looking at", now=NOW)
    assert resolution.resolved
    assert resolution.choice.label == "Inbox - Outlook"


def test_recency_decays_and_stops_counting():
    reference = Reference(phrase="that document", kind="document", deictic=True)
    fresh = Candidate(kind="document", label="a.docx", target="a", at=NOW - 1)
    stale = Candidate(kind="document", label="b.docx", target="b", at=NOW - 100_000)
    assert score(fresh, reference, now=NOW)[0] > score(stale, reference, now=NOW)[0]


def test_a_mismatched_kind_counts_against_a_candidate():
    reference = Reference(phrase="that document", kind="document", deictic=True)
    right = Candidate(kind="document", label="x", target="a")
    wrong = Candidate(kind="window", label="x", target="b")
    assert score(right, reference, now=NOW)[0] > score(wrong, reference, now=NOW)[0]


def test_a_near_tie_is_never_resolved():
    """The margin is what separates a decision from a coin toss."""
    resolver = ReferenceResolver([lambda: [
        Candidate(kind="document", label="report one.docx", target="a"),
        Candidate(kind="document", label="report two.docx", target="b"),
    ]])
    resolution = resolver.resolve("open that document", now=NOW)
    assert resolution.ambiguous
    top = sorted(resolution.scores.values(), reverse=True)
    assert abs(top[0] - top[1]) < DECISIVE_MARGIN


# --------------------------------------------------------------------------- recent store

def test_renoting_a_target_moves_it_rather_than_duplicating_it():
    store = RecentThings()
    store.note("document", "a.docx", "T")
    store.note("document", "b.docx", "U")
    store.note("document", "a.docx", "T")
    targets = [candidate.target for candidate in store.candidates()]
    assert targets.count("T") == 1
    assert targets[0] == "T"                     # and it is the most recent


def test_the_recent_store_is_bounded():
    store = RecentThings(limit=3)
    for index in range(10):
        store.note("document", f"{index}.docx", f"target{index}")
    assert len(store) == 3


def test_the_recent_store_ignores_an_empty_target():
    store = RecentThings()
    store.note("document", "a", "")
    assert len(store) == 0


# --------------------------------------------------------------------------- the tool boundary

def test_resolving_is_not_opening(resolver):
    """There must be no tool that resolves and acts in one step."""
    actions = ReferenceActions(resolver=resolver, recent=RecentThings())
    names = {tool.name for tool in actions.tools()}
    assert names == {"resolve_reference", "remember_reference"}
    for forbidden in ("resolve_and_open", "open_reference", "open_referenced", "act_on_reference"):
        assert forbidden not in names


def test_the_reference_tools_are_low_risk_because_they_do_nothing(resolver):
    from void.security.risk import RiskLevel
    for tool in ReferenceActions(resolver=resolver, recent=RecentThings()).tools():
        assert tool.effective_risk({}) == RiskLevel.LOW


def test_the_tool_returns_the_question_when_ambiguous(resolver):
    actions = ReferenceActions(resolver=resolver, recent=RecentThings())
    result = actions.resolve_reference(phrase="open that chat")
    assert result.ok                             # a question IS a useful answer
    assert "Which one" in result.summary
    assert result.data["ambiguous"] is True
    assert result.data["choice"] is None


def test_the_tool_reports_an_unresolvable_phrase_as_a_failure():
    actions = ReferenceActions(resolver=ReferenceResolver([]), recent=RecentThings())
    assert not actions.resolve_reference(phrase="open this chart").ok
    assert not actions.resolve_reference(phrase="").ok


def test_remember_reference_refuses_an_invented_kind():
    actions = ReferenceActions(resolver=ReferenceResolver([]), recent=RecentThings())
    assert not actions.remember_reference(kind="credential", label="x", target="y").ok
    assert not actions.remember_reference(kind="document", label="x", target="").ok
    assert actions.remember_reference(kind="document", label="x", target="y").ok


# --------------------------------------------------------------------------- describing vs pointing

def _recent_document(label="Q3 revenue chart.pptx"):
    """A document V.O.I.D produced a moment ago - the most attractive candidate there is."""
    store = RecentThings()
    store.note(kind="document", label=label, target="C:/tmp/q3.pptx", source="artifacts",
               produced=True)
    return ReferenceResolver([recent_candidates(store)])


def test_a_described_thing_that_matches_nothing_is_not_answered_with_the_nearest_recent_one():
    """The bug this guards against produced a genuinely wrong action.

    "Open Rushi's chat" used to resolve - with no ambiguity reported - to a PowerPoint V.O.I.D had
    just created. The kind mismatch cost W_KIND/2, recency and "I produced it" paid for more than
    that, the total was positive, and a lone positive candidate is chosen. A request for a person's
    conversation returned a revenue deck.
    """
    resolution = _recent_document().resolve("Open Rushi's chat")
    assert not resolution.resolved
    assert resolution.empty, "nothing described the owner's request, so there is nothing to offer"


def test_pointing_at_something_recent_still_works():
    """The fix must not cost the owner deixis, which is the whole point of the module."""
    for phrase in ("open that one", "open this", "open the document I was looking at",
                   "the one I was just looking at"):
        assert _recent_document().resolve(phrase).resolved, phrase


def test_a_description_that_does_match_still_resolves():
    assert _recent_document().resolve("open this chart").resolved
    assert _recent_document().resolve("open the Q3 revenue chart").resolved


def test_recency_corroborates_but_cannot_identify():
    """Stated directly, because it is the rule the bug broke."""
    recent = Candidate(kind="document", label="Budget.xlsx", target="b", at=time.time(),
                       produced=True)
    described = Reference(phrase="rushi's chat", kind="conversation", qualifier="rushi", noun="chat")
    pointed = Reference(phrase="that one", deictic=True)
    assert not identifies(recent, described)
    assert identifies(recent, pointed)
    # It is not the SCORE that rejects it - the score is positive. It is the evidence test.
    value, _reasons = score(recent, described)
    assert value > 0, "the score alone would have accepted this, which is why the gate exists"


@pytest.mark.parametrize("kind, label, matches", [
    ("conversation", "Rushi - WhatsApp", True),      # kind agrees
    ("document", "Rushi's notes.docx", True),        # the qualifier appears in the name
    ("tab", "Budget spreadsheet", False),            # neither
])
def test_identification_accepts_a_kind_match_or_a_name_match(kind, label, matches):
    candidate = Candidate(kind=kind, label=label, target="t")
    reference = Reference(phrase="rushi's chat", kind="conversation", qualifier="rushi", noun="chat")
    assert identifies(candidate, reference) is matches


def test_a_reference_with_nothing_to_match_on_accepts_anything():
    """A phrase with no kind, qualifier or noun has nothing to check, so the gate must not block it
    and leave the owner with no answer at all."""
    candidate = Candidate(kind="document", label="anything", target="t")
    assert identifies(candidate, Reference(phrase="open it"))
