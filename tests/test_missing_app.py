"""An application V.O.I.D cannot find is answered locally, not hunted for by the model.

Measured cause of the reported 3-5 minute "Open Notepad++" (docs/RESPONSE_TIMING_2026-09-24.md): the fast path
returned "unknown", the agent then made **12 model calls** looking for it, hit ``max_steps`` and reported "The
task failed." At the owner's own Gemini latencies that is 1.4 minutes at p50 and 7.4 at p90.

Every one of those calls was foregone work: the model's ``find_app`` tool reads the SAME ``AppCatalog`` the
resolver just searched, so it cannot find what the resolver could not. What it must NOT do is start guessing -
which is why the tests below care as much about what stays going to the model as about what stops.
"""
import pytest

from void.actions.computer import AppCatalog
from void.core.fast_path import FastPath
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry

from tests.test_computer import FakeBackend, _exe
from tests.test_fast_path import rig  # noqa: F401 - the real Assistant rig


def _fp(tmp_path, *names, **kw):
    be = FakeBackend(apps=[{"name": n, "kind": "exe", "target": _exe(tmp_path, f"{i}.exe")}
                           for i, n in enumerate(names)])
    return FastPath(AppCatalog(be), **kw)


# --- the reported failure ------------------------------------------------------------------------

def test_an_application_that_is_not_installed_is_answered_here(tmp_path):
    d = _fp(tmp_path, "Notepad", "WhatsApp").decide("Open Notepad++")
    assert d.plan is None                       # nothing is launched
    assert d.why == "unknown"
    assert "can't find" in d.reply.lower() and "notepad++" in d.reply.lower()


@pytest.mark.parametrize("said", [
    "Open Notepad++", "open photoshop", "launch sublime text", "start figma",
    "Hey V.O.I.D., open premiere pro",
])
def test_a_missing_application_never_reaches_the_model(rig, said):
    """The whole point: 12 model calls and minutes of waiting, to arrive at a sentence already known locally."""
    a, provider, launched, _ = rig(catalog_apps=[("Notepad", None)] and [])
    r = a.run(said)
    assert provider.calls == 0, f"{said!r} still went to the model"
    assert launched == []
    assert r.status == "completed" and "can't find" in r.result.lower()


def test_the_answer_says_which_name_was_not_found_and_leaks_nothing_else(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=[("Opera GX Browser", _exe(tmp_path, "o.exe"))])
    r = a.run("open sublime text")
    assert "sublime text" in r.result.lower()
    for leak in ("\\", ".exe", "app-", "launch_app", "Traceback", "C:"):
        assert leak not in r.result


def test_it_does_not_cost_a_dozen_agent_steps_any_more(rig):
    a, provider, launched, _ = rig()
    r = a.run("Open Notepad++")
    assert r.steps == 0 and provider.calls == 0


# --- what must still go to the model -------------------------------------------------------------

def test_an_installed_application_still_launches(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=[("Opera GX Browser", _exe(tmp_path, "o.exe"))])
    r = a.run("open opera gx")
    assert r.result == "Opening Opera GX Browser." and provider.calls == 0 and len(launched) == 1


@pytest.mark.parametrize("said", [
    "Explain ARP.", "What is the TCP three-way handshake?", "what time is it",
    "summarise the notes in my workspace", "remind me to buy milk",
])
def test_a_request_that_is_not_a_launch_command_still_reaches_the_model(rig, said):
    """The grammar is what protects this: only "open/launch/start <plain name>" is answered locally."""
    a, provider, _launched, _ = rig()
    a.run(said)
    assert provider.calls == 1


def test_an_ambiguous_name_asks_which_one_rather_than_claiming_it_is_missing(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=[("Editor", _exe(tmp_path, "a.exe")),
                                                 ("Editor", _exe(tmp_path, "b.exe"))])
    r = a.run("open editor")
    assert "more than one" in r.result.lower() and "can't find" not in r.result.lower()
    assert provider.calls == 0 and launched == []


def test_an_excluded_console_still_goes_to_the_model_not_answered_as_missing(tmp_path):
    """Refusing to fast-path an administration console is not the same as saying it is not installed."""
    d = _fp(tmp_path, "Registry Editor").decide("open regedit")
    assert d.plan is None and d.why == "excluded" and d.reply == ""


def test_a_catalog_that_cannot_be_built_never_claims_an_application_is_missing(tmp_path):
    """If discovery failed, V.O.I.D does not know what is installed - and must not pretend it does."""
    fp = FastPath(AppCatalog(FakeBackend(raise_on={"discover_apps"})))
    d = fp.decide("open sublime text")
    assert d.why == "discovery" and d.reply == ""


def test_the_behaviour_can_be_turned_off(tmp_path):
    fp = _fp(tmp_path, "Notepad", answer_unknown=False)
    d = fp.decide("Open Notepad++")
    assert d.why == "unknown" and d.reply == ""      # back to the model, exactly as before


def test_the_switch_is_configurable_and_on_by_default():
    from void.config import Config
    assert Config.load().get("fast_path.answer_unknown_apps", None) is True


# --- the session keeps working -------------------------------------------------------------------

def test_a_missing_application_does_not_disturb_the_next_command(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=[("Opera GX Browser", _exe(tmp_path, "o.exe"))])
    assert "can't find" in a.run("Open Notepad++").result.lower()
    assert a.run("open opera gx").result == "Opening Opera GX Browser."
    assert provider.calls == 0 and len(launched) == 1


def test_an_application_installed_later_stops_being_reported_as_missing(rig, tmp_path):
    a, provider, launched, _ = rig(catalog_apps=[("Notepad", _exe(tmp_path, "n.exe"))])
    assert "can't find" in a.run("open sublime text").result.lower()
    backend = a._fast.catalog._backend
    backend._apps.append({"name": "Sublime Text", "kind": "exe", "target": _exe(tmp_path, "s.exe")})
    a._fast.catalog.invalidate()
    assert a.run("open sublime text").result == "Opening Sublime Text."

@pytest.mark.parametrize("said", ["open my dashboard", "open your inbox", "open that thing"])
def test_a_possessive_alone_is_enough_to_leave_it_to_the_model(rig, said):
    """Something belonging to the owner is theirs to find, even when no file-ish noun is present."""
    a, provider, _launched, _ = rig()
    a.run(said)
    assert provider.calls == 1


@pytest.mark.parametrize("said", ["open report", "open screenshots", "open backup"])
def test_a_content_noun_alone_is_enough_to_leave_it_to_the_model(rig, said):
    """And a container noun is a job for find_directory / open_path, with or without a possessive."""
    a, provider, _launched, _ = rig()
    a.run(said)
    assert provider.calls == 1
