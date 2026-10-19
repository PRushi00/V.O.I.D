"""Focus sessions: the privacy property, the deferred lock, drift accounting, and the controls.

The test that matters most is :func:`test_a_distraction_is_named_out_loud_and_stored_nowhere`. It
drives a real drift with a canary label - a string that appears in no canned line, no map and no
other test in this repository - and then asserts that the label reached the spoken sentence and
reached nothing else. A test that greps for "Instagram" would be satisfied by the example sentences
in ``void/focus/lines.py``, which is exactly the trap the Astra pack warns about.

Everything else here defends properties that are cheap to break by accident: that a session started
in front of V.O.I.D's own window never locks V.O.I.D's own window, that a sensor which cannot see
never accuses the owner of drifting, that an excuse refunds the excursion it excused, and that the
ledger cannot grow a field.
"""
import json
import time
from pathlib import Path

import pytest

from void.app import Assistant
from void.config import Config
from void.focus import (CLEAN_SHARE, DEFER_FALLBACK_S, GRACE_S, LEDGER_KEYS, MAX_MINUTES,
                        MIN_MINUTES, NAG_EVERY_S, SETTLE_TICKS, FocusEngine, Ledger, Phase,
                        ReportCard, SessionState)
from void.focus import commands as focus_commands
from void.focus import lines as focus_lines
from void.focus.commands import FocusIntent, parse
from void.focus.reader import (BROWSER_PROCESSES, HOME_PROCESSES, READ_FAILED, READ_NO_WINDOW,
                               SurfaceReader)

#: A label that appears nowhere else: not in the spoken-host map, not in a callout pool, not in any
#: other test. If this string turns up in session state, a report card or a ledger row, a label was
#: stored - and no amount of care in review would have caught it.
CANARY = "zygalquintaped.example"


class Desk:
    """A scripted foreground. ``app`` and ``host`` are what the next read will see."""

    def __init__(self, app="editor", host=""):
        self.app, self.host = app, host
        self.window_reads = 0

    def window(self):
        self.window_reads += 1
        if self.app is None:
            return None
        return {"app": self.app, "title": "whatever the program chose", "pid": 4242,
                "window_token": "win-x"}

    def tab(self):
        return (f"https://{self.host}/some/path" if self.host else ""), "ok"

    def reader(self) -> SurfaceReader:
        return SurfaceReader(active_window=self.window, front_tab=self.tab)


def engine(desk: Desk, **kw) -> FocusEngine:
    said = kw.pop("said", None)
    return FocusEngine(desk.reader(), speak=(said.append if said is not None else None),
                       ledger=kw.pop("ledger", Ledger()), now=lambda: 0.0,
                       pick=kw.pop("pick", lambda pool: pool[0]), **kw)


def run_ticks(eng: FocusEngine, desk: Desk, seconds, *, start=0.0):
    """Tick once per second, applying each (app, host) in turn. Returns the final time."""
    at = start
    for app, host in seconds:
        at += 1.0
        desk.app, desk.host = app, host
        eng.tick(now=at)
    return at


# ================================================================== the privacy property

def test_the_session_state_is_a_closed_set_of_booleans_and_counters():
    """Structural: there is no field here that could hold an identity."""
    for name, value in SessionState().as_dict().items():
        assert isinstance(value, (bool, int, float, str)), name
        if isinstance(value, str):
            assert name == "phase" and value in Phase.ALL, name


def test_a_distraction_is_named_out_loud_and_stored_nowhere():
    """The canary test. The label reaches the sentence and nothing else."""
    said = []
    desk = Desk(app="chrome", host="work.example.com")
    ledger = Ledger()
    eng = engine(desk, said=said, ledger=ledger)
    eng.start(30, now=0.0, intent="the thumbnail sprint")
    at = run_ticks(eng, desk, [("chrome", "work.example.com")] * 2)
    at = run_ticks(eng, desk, [("chrome", CANARY)] * 3, start=at)

    spoken = " ".join(said)
    assert CANARY in spoken, "the label never reached the spoken line"

    card = eng.end(now=at + 1, completed=True)
    haystacks = {
        "state": json.dumps(eng.state(now=at + 1).as_dict()),
        "diag": json.dumps(eng.diag(now=at + 1)),
        "report": json.dumps(card.as_dict()),
        "spoken report": card.spoken(),
        "ledger": json.dumps(ledger.rows),
    }
    for where, text in haystacks.items():
        assert CANARY not in text, f"the label was stored in the {where}"
        assert "work.example.com" not in text, f"the target's identity is in the {where}"


def test_not_even_a_hash_of_the_surface_escapes():
    """The one value the engine keeps is a digest, and it does not leave the engine either."""
    desk = Desk(app="chrome", host=CANARY)
    eng = engine(desk)
    eng.start(10, now=0.0)
    at = run_ticks(eng, desk, [("chrome", CANARY)] * 3)
    from void.focus.reader import _handle, APP, TAB
    for digest in (_handle(APP, "chrome"), _handle(TAB, CANARY)):
        assert digest not in json.dumps(eng.state(now=at).as_dict())
        assert digest not in json.dumps(eng.diag(now=at))


def test_naming_can_be_switched_off_entirely():
    said = []
    desk = Desk(app="chrome", host="work.example.com")
    eng = engine(desk, said=said, naming=False)
    eng.start(30, now=0.0)
    at = run_ticks(eng, desk, [("chrome", "work.example.com")] * 2)
    run_ticks(eng, desk, [("chrome", CANARY)] * 3, start=at)
    assert said, "nothing was said at all"
    assert CANARY not in " ".join(said)
    assert "zygal" not in " ".join(said).lower()


def test_a_ledger_row_can_only_hold_the_whitelisted_keys():
    ledger = Ledger()
    row = ledger.record(ReportCard(planned_minutes=30, on_target_minutes=29, share=0.97,
                                   clean=True, completed=True))
    assert tuple(row) == LEDGER_KEYS
    assert all(isinstance(v, (int, float, bool)) for v in row.values())


def test_a_new_report_field_cannot_leak_into_the_ledger():
    """Enforced on write rather than trusted: the row is rebuilt from the whitelist."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class Leaky(ReportCard):
        pass

    card = Leaky(planned_minutes=5, completed=True)
    object.__setattr__(card, "host", CANARY)          # a field someone added later
    row = Ledger().record(card)
    assert CANARY not in json.dumps(row) and "host" not in row


# ================================================================== the deferred lock

def test_a_session_started_in_front_of_void_locks_nothing_yet_and_says_so():
    """The one surface the owner is guaranteed to leave must never become the target."""
    desk = Desk(app="pythonw")
    eng = engine(desk)
    line = eng.start(30, now=0.0)
    state = eng.state(now=0.0)
    assert state.deferred and state.phase == Phase.DEFERRED
    assert not state.app_locked and not state.tab_locked
    assert "lock on there" in line.lower()


def test_the_first_surface_that_holds_for_two_reads_becomes_the_target():
    said = []
    desk = Desk(app="pythonw")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("editor", "")])
    assert eng.state(now=1.0).settle_ticks == 1 and eng.state(now=1.0).deferred
    run_ticks(eng, desk, [("editor", "")], start=1.0)
    state = eng.state(now=2.0)
    assert not state.deferred and state.app_locked and state.settle_ticks == 0
    assert "Locked on." in said


def test_a_surface_merely_passing_through_does_not_become_the_target():
    desk = Desk(app="pythonw")
    eng = engine(desk)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("editor", ""), ("explorer", ""), ("terminal", "")])
    assert eng.state(now=3.0).deferred, "a single read should never win the lock"
    assert SETTLE_TICKS == 2


def test_if_the_owner_never_leaves_home_base_the_application_is_locked_instead():
    said = []
    desk = Desk(app="pythonw")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("pythonw", "")] * int(DEFER_FALLBACK_S + 1))
    state = eng.state(now=DEFER_FALLBACK_S + 1)
    assert not state.deferred and state.app_locked and not state.tab_locked
    assert any("where you are" in line for line in said)


def test_a_session_started_on_a_real_surface_locks_it_immediately():
    desk = Desk(app="chrome", host="work.example.com")
    eng = engine(desk)
    line = eng.start(30, now=0.0)
    state = eng.state(now=0.0)
    assert not state.deferred and state.app_locked and state.tab_locked
    assert "tab" in line.lower()


def test_a_foreground_that_cannot_be_read_defers_rather_than_locking_something_plausible():
    desk = Desk(app=None)
    eng = engine(desk)
    eng.start(30, now=0.0)
    assert eng.state(now=0.0).deferred and eng.state(now=0.0).blind


# ================================================================== drift

def test_a_brief_switch_inside_the_grace_window_is_not_a_drift():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    desk.app = "slack"
    eng.tick(now=0.5)                                  # inside GRACE_S
    assert GRACE_S == 0.8
    assert not said
    assert eng.state(now=0.5).drifts == 0


def test_a_switch_that_outlasts_the_grace_window_earns_one_callout():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("slack", ""), ("slack", "")])
    assert len(said) == 1
    assert eng.state(now=2.0).adrift is True


def test_one_drift_does_not_produce_a_callout_every_tick():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("slack", "")] * 10)
    assert len(said) == 1, "the nag cadence is what stops a session being unbearable"
    assert NAG_EVERY_S == 30.0


def test_the_nag_cadence_is_settable_and_fires_again_when_it_elapses():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    eng.nag_every(3)
    run_ticks(eng, desk, [("slack", "")] * 8)
    assert len(said) >= 2


def test_callouts_escalate_over_separate_excursions():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    at = 0.0
    for _ in range(3):
        at = run_ticks(eng, desk, [("slack", ""), ("slack", "")], start=at)
        at = run_ticks(eng, desk, [("editor", "")], start=at)
    assert len(said) == 3
    for tier, line in enumerate(said, start=1):
        expected = {template.format(what="slack") for template in focus_lines.NAMED_LINES[tier]}
        assert line in expected, f"tier {tier} said {line!r}"


def test_every_tier_has_four_lines_so_a_long_drift_does_not_loop_three_phrases():
    for tier, pool in focus_lines.NAMED_LINES.items():
        assert len(pool) == 4, tier
        assert all("{what}" in line for line in pool)
    for tier, pool in focus_lines.NAMELESS_LINES.items():
        assert len(pool) == 4, tier
        assert all("{what}" not in line for line in pool)


def test_coming_back_to_void_itself_is_never_a_drift():
    """Home base: talking to the assistant is part of the work."""
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("pythonw", "")] * 5)
    assert said == [] and eng.state(now=5.0).drifts == 0


def test_a_blind_sensor_never_accuses_the_owner_of_anything():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    desk.app = None
    run_ticks(eng, desk, [(None, "")] * 5)
    assert said == []
    state = eng.state(now=5.0)
    assert state.blind and not state.adrift and state.drifts == 0


def test_the_right_application_with_an_unreadable_tab_is_still_on_target():
    """A tab lane going quiet is a sensor problem, never the owner's fault."""
    desk = Desk(app="chrome", host="work.example.com")
    eng = engine(desk)
    eng.start(30, now=0.0)
    said_before = eng.state(now=0.0).drifts
    run_ticks(eng, desk, [("chrome", "")] * 4)         # same app, no host readable
    assert eng.state(now=4.0).drifts == said_before
    assert eng.state(now=4.0).on_target_s > 0


def test_a_different_site_in_the_same_browser_is_a_drift():
    desk = Desk(app="chrome", host="work.example.com")
    said = []
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("chrome", "instagram.com")] * 2)
    assert said and eng.state(now=2.0).adrift


def test_the_host_is_what_is_locked_and_not_the_path():
    """A work tool whose path changes with every click must not read as a drift."""
    desk = Desk(app="chrome", host="work.example.com")
    said = []
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)

    def other_path():
        return "https://work.example.com/a/totally/different/record", "ok"

    eng._reader._front_tab = other_path                # noqa: SLF001
    run_ticks(eng, desk, [("chrome", "work.example.com")] * 4)
    assert said == [] and eng.state(now=4.0).drifts == 0


# ================================================================== the owner's controls

def test_a_snooze_holds_the_callouts_and_then_lets_them_through():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    eng.snooze(5, now=0.0)
    run_ticks(eng, desk, [("slack", "")] * 4)
    assert said == []
    run_ticks(eng, desk, [("slack", "")] * 3, start=4.0)
    assert said


def test_an_excuse_refunds_the_excursion_it_excused_and_stays_quiet():
    said = []
    desk = Desk(app="editor")
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    at = run_ticks(eng, desk, [("browser", "")] * 4)
    on_target_before = eng.state(now=at).on_target_s
    eng.excuse(now=at)
    state = eng.state(now=at)
    assert state.excused == 1 and state.excused_s > 0
    assert state.on_target_s > on_target_before, "the excused seconds were not refunded"
    said.clear()
    run_ticks(eng, desk, [("browser", "")] * 10, start=at)
    assert said == [], "an excuse must stay quiet until the owner is back"


def test_pausing_stops_the_accounting_and_the_deadline_moves_with_it():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(1, now=0.0)
    run_ticks(eng, desk, [("editor", "")] * 5)
    eng.pause(now=5.0)
    before = eng.state(now=5.0)
    eng.tick(now=40.0)                                 # a long pause
    assert eng.state(now=40.0).active_s == before.active_s
    eng.resume(now=40.0)
    assert eng.state(now=40.0).seconds_left > 50, "a paused session must not expire while paused"


def test_extending_adds_to_the_plan():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(10, now=0.0)
    eng.extend(5)
    assert eng.state(now=0.0).planned_minutes == 15


def test_relocking_takes_where_the_owner_is_now_and_forgives_the_drift():
    desk = Desk(app="editor")
    said = []
    eng = engine(desk, said=said)
    eng.start(30, now=0.0)
    at = run_ticks(eng, desk, [("browser", "")] * 3)
    desk.app = "browser"
    reply = eng.relock(now=at)
    assert "locked on" in reply.lower()
    state = eng.state(now=at)
    assert not state.adrift
    said.clear()
    run_ticks(eng, desk, [("browser", "")] * 4, start=at)
    assert said == [], "the new target must not be reported as a drift"


def test_relocking_from_void_itself_re_arms_the_deferral_instead_of_locking_void():
    """The card trap: "lock here" said to the assistant can only mean "lock where I land"."""
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(30, now=0.0)
    desk.app = "pythonw"
    reply = eng.relock(now=1.0)
    assert "go to it" in reply.lower()
    assert eng.state(now=1.0).deferred and not eng.state(now=1.0).tab_locked


def test_relocking_with_a_blind_sensor_refuses_rather_than_guessing():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(30, now=0.0)
    desk.app = None
    assert "cannot tell" in eng.relock(now=1.0).lower()


def test_a_session_length_is_bounded_in_both_directions():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(100000, now=0.0)
    assert eng.state(now=0.0).planned_minutes == MAX_MINUTES
    eng2 = engine(Desk(app="editor"))
    eng2.start(0, now=0.0)
    assert eng2.state(now=0.0).planned_minutes == MIN_MINUTES


def test_a_control_with_no_session_says_so_rather_than_pretending():
    desk = Desk(app="editor")
    eng = engine(desk)
    for reply in (eng.pause(), eng.resume(), eng.extend(5), eng.abort(), eng.relock()):
        assert "no " in reply.lower()


# ================================================================== the report card

def test_the_report_card_is_minutes_on_target_out_of_planned_and_the_detours():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(1, now=0.0)
    at = run_ticks(eng, desk, [("editor", "")] * 40)
    at = run_ticks(eng, desk, [("slack", "")] * 5, start=at)
    at = run_ticks(eng, desk, [("editor", "")] * 10, start=at)
    card = eng.end(now=at, completed=True)
    assert card.planned_minutes == 1
    assert 0 < card.on_target_minutes <= card.active_minutes
    assert card.drifts == 1
    assert "minutes on target" in card.spoken()


def test_the_streak_grows_on_a_clean_session_and_resets_on_a_poor_one():
    ledger = Ledger()
    for _ in range(2):
        desk = Desk(app="editor")
        eng = engine(desk, ledger=ledger)
        eng.start(1, now=0.0)
        at = run_ticks(eng, desk, [("editor", "")] * 60)   # the session ends itself here
        card = eng.end(now=at, completed=True)             # and can still be asked for its card
        assert card.completed and card.clean and card.share >= CLEAN_SHARE
    assert ledger.streak == 2

    desk = Desk(app="editor")
    eng = engine(desk, ledger=ledger)
    eng.start(1, now=0.0)
    at = run_ticks(eng, desk, [("slack", "")] * 60)
    card = eng.end(now=at, completed=True)
    assert not card.clean and ledger.streak == 0


def test_an_abandoned_session_is_never_clean():
    """Abandoned before the time was up, however well it was going."""
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(1, now=0.0)
    at = run_ticks(eng, desk, [("editor", "")] * 55)       # not yet the full minute
    card = eng.end(now=at, completed=False)
    assert card.completed is False and card.clean is False
    assert card.on_target_minutes > 0                      # the work it did is still reported


def test_a_session_that_ended_itself_can_still_be_asked_for_its_card():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(1, now=0.0)
    at = run_ticks(eng, desk, [("editor", "")] * 61)
    assert eng.state(now=at).phase == Phase.ENDED
    card = eng.end(now=at)
    assert card.completed and card.planned_minutes == 1


def test_a_session_ends_itself_when_the_planned_time_is_up():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(1, now=0.0)
    run_ticks(eng, desk, [("editor", "")] * 61)
    assert eng.state(now=61.0).phase == Phase.ENDED
    assert not eng.running


def test_the_ledger_file_is_one_json_line_per_session_and_owner_only(tmp_path):
    import os
    import stat
    path = tmp_path / "focus.jsonl"
    ledger = Ledger(path)
    ledger.record(ReportCard(planned_minutes=5, completed=True, clean=True))
    ledger.record(ReportCard(planned_minutes=5, completed=True, clean=False))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and all(set(r) == set(LEDGER_KEYS) for r in rows)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


# ================================================================== the diagnostic

def test_the_diagnostic_is_booleans_and_statuses_read_from_the_live_engine():
    desk = Desk(app="chrome", host="work.example.com")
    eng = engine(desk)
    eng.start(30, now=0.0)
    run_ticks(eng, desk, [("chrome", "work.example.com")] * 2)
    diag = eng.diag(now=2.0)
    for key in ("phase", "app_locked", "tab_locked", "deferred", "adrift", "blind",
                "settle_ticks", "naming", "reads", "read_failures", "tick_thread_alive",
                "streak"):
        assert key in diag, key
    for key, value in diag.items():
        assert isinstance(value, (bool, int, float, str)), key
        if isinstance(value, str):
            assert value in Phase.ALL, f"{key} carries free text"


def test_the_reader_is_asked_fresh_every_tick():
    """A cached read freezes in a long-lived process and reports the first surface forever."""
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(30, now=0.0)
    before = desk.window_reads
    run_ticks(eng, desk, [("editor", "")] * 5)
    assert desk.window_reads == before + 5


# ================================================================== the reader

def test_void_itself_is_home_base_and_a_browser_is_a_browser():
    desk = Desk(app="pythonw")
    assert desk.reader().read().is_home
    desk.app = "chrome.exe"
    surface = desk.reader().read()
    assert surface.is_browser and not surface.is_home
    desk.app = "editor"
    assert not desk.reader().read().is_browser


def test_a_foreground_that_cannot_be_read_says_which_way_it_failed():
    assert SurfaceReader(active_window=lambda: None).read().status == READ_NO_WINDOW
    assert SurfaceReader(active_window=None).read().status == READ_FAILED

    def explodes():
        raise RuntimeError("the window layer is unavailable")

    assert SurfaceReader(active_window=explodes).read().status == READ_FAILED


def test_a_browser_with_no_tab_layer_is_locked_at_application_level():
    reader = SurfaceReader(active_window=lambda: {"app": "chrome"}, front_tab=None)
    surface = reader.read()
    assert surface.is_browser and surface.app_handle and not surface.tab_handle
    assert surface.tab_status


def test_the_reader_counts_its_failures_for_the_diagnostic():
    reader = SurfaceReader(active_window=lambda: None)
    for _ in range(3):
        reader.read()
    assert reader.reads == 3 and reader.failures == 3


def test_home_and_browser_process_lists_are_lower_case_and_non_empty():
    for names in (HOME_PROCESSES, BROWSER_PROCESSES):
        assert names and all(n == n.lower() for n in names)


# ================================================================== naming hosts

@pytest.mark.parametrize("host, spoken", [
    ("instagram.com", "Instagram"),
    ("www.instagram.com", "Instagram"),
    ("music.youtube.com", "YouTube"),
    ("news.ycombinator.com", "Hacker News"),
    ("example.com", "example.com"),
    ("www.example.co.uk", "example.co.uk"),
    ("", ""),
])
def test_a_host_becomes_a_word_a_person_would_say(host, spoken):
    assert focus_lines.spoken_host(host) == spoken


def test_the_first_callout_names_both_the_work_and_the_distraction():
    line = focus_lines.callout(1, "Instagram", intent="the thumbnail sprint", first=True)
    assert "Instagram" in line and "the thumbnail sprint" in line


# ================================================================== the commands

@pytest.mark.parametrize("utterance, intent, minutes", [
    ("thirty minutes on this", FocusIntent.START, 30),
    ("25 minutes on this", FocusIntent.START, 25),
    ("focus for 25 minutes", FocusIntent.START, 25),
    ("let's do an hour on this", FocusIntent.START, 60),
    ("start a focus session for 45 minutes", FocusIntent.START, 45),
    ("extend the session another 10 minutes", FocusIntent.EXTEND, 10),
])
def test_a_session_length_said_out_loud_is_understood(utterance, intent, minutes):
    request = parse(utterance)
    assert request.intent == intent and request.minutes == minutes


@pytest.mark.parametrize("utterance, intent", [
    ("end the session", FocusIntent.STOP),
    ("stop the focus", FocusIntent.STOP),
    ("pause the session", FocusIntent.PAUSE),
    ("resume the timer", FocusIntent.RESUME),
    ("give me fifteen seconds", FocusIntent.SNOOZE),
    ("call me out every thirty seconds", FocusIntent.NAG),
    ("lock on this tab", FocusIntent.RELOCK),
    ("keep me in this tab", FocusIntent.RELOCK),
    ("this is the tab", FocusIntent.RELOCK),
    ("how long is left", FocusIntent.STATUS),
    ("it's okay, I'm doing research", FocusIntent.EXCUSE),
    ("that's fine", FocusIntent.EXCUSE),
])
def test_the_session_controls_are_recognised(utterance, intent):
    assert parse(utterance).intent == intent


@pytest.mark.parametrize("utterance", [
    "", "   ", "stop", "pause", "open notepad", "thirty minutes", "lock the door",
    "how long is the pause in the audio", "give me a break", "what is a focus session",
    "remember that I focus best in the morning",
])
def test_an_utterance_that_is_not_a_session_control_is_left_alone(utterance):
    assert parse(utterance).intent == FocusIntent.NONE


def test_an_intent_is_only_heard_inside_the_answer_window():
    assert parse("the thumbnail sprint").intent == FocusIntent.NONE
    answered = parse("the thumbnail sprint", expecting_intent=True)
    assert answered.intent == FocusIntent.INTENT and answered.text == "the thumbnail sprint"


def test_an_intent_is_never_parroted_back_in_full():
    desk = Desk(app="editor")
    eng = engine(desk)
    eng.start(30, now=0.0)
    long_intent = "the thumbnail sprint " * 40
    reply = eng.set_intent(long_intent)
    assert reply == "Noted." and len(reply) < 20


# ================================================================== through the Assistant

def assistant(tmp_path, **over) -> Assistant:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    data = {"app": {"state_dir": str(tmp_path / "state")},
            "security": {"allowed_roots": [str(tmp_path / "work")]},
            "memory": {"enabled": False}}
    data.update(over)
    return Assistant(config=Config(data))


def test_the_whole_session_is_driveable_by_sentence_with_no_model(tmp_path):
    a = assistant(tmp_path)
    for utterance in ("thirty minutes on this", "how long is left", "give me fifteen seconds",
                      "call me out every twenty seconds", "pause the session",
                      "resume the session", "extend the session another 10 minutes",
                      "end the session"):
        out = a.run(utterance)
        assert out.steps == 0 and out.engine_authored is True, utterance
        assert out.result, utterance
    a.focus.stop_ticking()


def test_the_answer_window_attaches_the_intent_and_then_closes(tmp_path):
    a = assistant(tmp_path)
    a.run("thirty minutes on this")
    assert a._awaiting_intent is True
    assert a.run("I'm working on the thumbnail sprint").result == "Noted."
    assert a._awaiting_intent is False
    assert a.focus.state().has_intent
    assert a._focus_command("the thumbnail sprint") is None   # the window is closed
    a.focus.stop_ticking()


def test_switching_focus_off_answers_honestly(tmp_path):
    a = assistant(tmp_path, focus={"enabled": False})
    out = a.run("thirty minutes on this")
    assert a.focus is None
    assert "switched off" in out.result


def test_an_excuse_outside_a_session_is_a_remark_and_not_a_command(tmp_path):
    a = assistant(tmp_path)
    assert a._focus_command("it's okay, I'm doing research") is None


def test_no_focus_reply_can_carry_an_identity(tmp_path):
    """Every focus reply is engine_authored, which means it may be spoken aloud."""
    a = assistant(tmp_path)
    replies = [a.run(u).result for u in
               ("thirty minutes on this", "how long is left", "lock on this tab",
                "end the session")]
    for reply in replies:
        assert CANARY not in reply and "pythonw" not in reply and "chrome" not in reply
    a.focus.stop_ticking()


def test_the_ticking_thread_starts_and_stops(tmp_path):
    a = assistant(tmp_path)
    a.run("five minutes on this")
    assert a.focus.diag()["tick_thread_alive"] is True
    a.focus.stop_ticking()
    assert a.focus.diag()["tick_thread_alive"] is False


def test_a_session_grants_no_capability_and_can_enforce_nothing(tmp_path):
    """Structural: there is nothing in the focus layer that could close, block or hide a window."""
    a = assistant(tmp_path)
    tools_before = set(a.tools.names())
    a.run("thirty minutes on this")
    assert set(a.tools.names()) == tools_before
    source = ""
    for name in ("__init__", "reader", "lines", "commands"):
        source += (Path(__import__("void.focus", fromlist=["x"]).__file__).parent
                   / f"{name}.py").read_text(encoding="utf-8")
    # Tool names and APIs, not bare words: "kill" appears in this layer only as a word the owner
    # can say to end their own session.
    for forbidden in ("close_app", "set_window_state", "launch_app", "taskkill", "TerminateProcess",
                      "firewall", "etc/hosts", "subprocess", "os.system"):
        assert forbidden not in source, f"the focus layer references {forbidden}"
    a.focus.stop_ticking()
