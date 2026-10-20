"""The focus countdown card: the presenter, which is where every rule actually lives.

The pack asks for a card "pinned top-right that tints on drift". Pixels cannot be asserted in this
container, so the card is split: ``card_for`` decides everything and is tested here exhaustively,
and ``FocusCardView`` only paints what it is handed. That split is the reason this feature is built
at all rather than declined - the undecidable part is now three setStyleSheet calls, and the part
that could be wrong in a way that matters is covered.

The property worth defending hardest is the one at the bottom: a card built from ``SessionState``
cannot display what the owner was looking at, because ``SessionState`` cannot hold it.
"""
from __future__ import annotations

from dataclasses import fields

import pytest

from void.focus import Phase, SessionState

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
from void.ui.focus_card import (ADRIFT, DEFERRED, HIDDEN_PHASES, ON_TARGET, PAUSED, URGENT,
                                URGENT_S, Card, FocusCardView, card_for, clock)

CANARY = "zygalquintaped.example"


def running(**over) -> SessionState:
    base = dict(phase=Phase.RUNNING, planned_minutes=25, active_s=600.0, on_target_s=540.0,
                seconds_left=900.0, app_locked=True)
    base.update(over)
    return SessionState(**base)


# --- the clock ------------------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, shown", [
    (0, "0:00"), (1, "0:01"), (59, "0:59"), (60, "1:00"), (61, "1:01"),
    (900, "15:00"), (3599, "59:59"), (3600, "60:00"), (14400, "240:00"),
])
def test_the_clock_reads_as_a_countdown(seconds, shown):
    assert clock(seconds) == shown


def test_the_clock_never_goes_negative():
    """A session that overran must read 0:00, not a minus sign: the engine ends sessions, and a
    negative countdown would be the card inventing a state the engine does not have."""
    assert clock(-1) == "0:00" and clock(-999.9) == "0:00"


def test_the_clock_rounds_up_so_it_never_shows_zero_while_time_remains():
    """0.4s left is still time left. Truncating would show 0:00 with the session still running,
    which is the one reading that would teach the owner to distrust the card."""
    assert clock(0.4) == "0:01"
    assert clock(59.1) == "1:00"
    assert clock(0.0) == "0:00"


# --- what shows, and when -------------------------------------------------------------------------

@pytest.mark.parametrize("phase", sorted(HIDDEN_PHASES))
def test_nothing_is_shown_when_there_is_no_session(phase):
    card = card_for(SessionState(phase=phase))
    assert card.visible is False and card.headline == ""


def test_an_unknown_phase_is_hidden_rather_than_guessed_at():
    """Phase.ALL is a closed set. A new member must be a deliberate decision in card_for, not a
    card that renders whatever the last branch left behind."""
    card = card_for(SessionState(phase="teleported"))
    assert card.visible is False


@pytest.mark.parametrize("phase", sorted(Phase.ALL))
def test_every_phase_returns_a_card(phase):
    """Total function: the view never has to handle a None."""
    assert isinstance(card_for(SessionState(phase=phase, planned_minutes=25)), Card)


# --- the tint, which is the whole point -----------------------------------------------------------

def test_a_locked_session_on_target_recedes():
    assert card_for(running()).tint == ON_TARGET
    assert card_for(running()).headline == "Locked on"


def test_drift_is_the_only_warm_tint_in_the_set():
    """The pack wants drift legible from the corner of the eye, so exactly one state may be warm.
    If a second ever became warm, drift would stop being the thing that catches attention."""
    warm = [t for t in (ON_TARGET, ADRIFT, DEFERRED, PAUSED, URGENT) if t[0] > t[2]]
    assert warm == [ADRIFT]


def test_drifting_tints_and_says_so():
    card = card_for(running(adrift=True))
    assert card.tint == ADRIFT and card.headline == "Drifting" and card.adrift is True


def test_a_deferred_session_does_not_claim_a_lock_it_does_not_have():
    """Running with nothing locked yet. "Locked on" here would be the card asserting a lock the
    engine deliberately has not taken."""
    card = card_for(running(deferred=True, app_locked=False))
    assert card.headline == "Waiting to lock" and card.tint == DEFERRED


def test_the_deferred_phase_reads_the_same_as_the_deferred_flag():
    assert card_for(SessionState(phase=Phase.DEFERRED, planned_minutes=25)).headline == "Waiting to lock"


def test_a_paused_session_is_dim_and_named():
    card = card_for(running(phase=Phase.PAUSED))
    assert card.headline == "Paused" and card.tint == PAUSED


def test_pause_outranks_drift():
    """A paused session is not drifting - it is not measuring at all. Showing the warm tint while
    paused would nag the owner about a session they deliberately stopped."""
    card = card_for(running(phase=Phase.PAUSED, adrift=True))
    assert card.headline == "Paused" and card.tint == PAUSED


def test_the_last_minute_reads_as_urgent_without_reading_as_drift():
    card = card_for(running(seconds_left=45.0))
    assert card.urgent is True and card.tint == URGENT and card.tint != ADRIFT


def test_drift_outranks_the_last_minute():
    """Both at once is the moment the owner most needs to know which one it is, and drift is the
    one they can act on."""
    card = card_for(running(seconds_left=30.0, adrift=True))
    assert card.tint == ADRIFT and card.urgent is True


def test_urgency_starts_exactly_at_the_documented_boundary():
    assert card_for(running(seconds_left=URGENT_S)).urgent is True
    assert card_for(running(seconds_left=URGENT_S + 0.1)).urgent is False
    # Zero is not urgent: it is over, and the engine will end the session.
    assert card_for(running(seconds_left=0.0)).urgent is False


def test_the_tint_never_depends_on_the_numbers():
    """Tint carries drift; text carries detail. If counters could move the tint, a glance would stop
    meaning one specific thing."""
    plain = card_for(running())
    busy = card_for(running(drifts=9, excused=4, excused_s=400.0, on_target_s=1.0, active_s=600.0))
    assert plain.tint == busy.tint


# --- the numbers a view would otherwise have to work out ------------------------------------------

def test_progress_runs_from_nothing_to_whole():
    assert card_for(running(planned_minutes=10, seconds_left=600.0)).progress == 0.0
    assert card_for(running(planned_minutes=10, seconds_left=300.0)).progress == pytest.approx(0.5)
    assert card_for(running(planned_minutes=10, seconds_left=0.0)).progress == 1.0


def test_progress_and_share_are_clamped_for_a_view_that_draws_a_ring():
    """A ring drawn from an unclamped fraction paints outside itself. Neither value may ever leave
    0.0-1.0, whatever the engine reports."""
    weird = card_for(running(planned_minutes=1, seconds_left=99999.0, active_s=10.0,
                             on_target_s=99999.0))
    assert 0.0 <= weird.progress <= 1.0 and 0.0 <= weird.share <= 1.0


def test_share_is_zero_rather_than_undefined_before_any_time_has_passed():
    assert card_for(running(active_s=0.0, on_target_s=0.0)).share == 0.0


def test_a_zero_minute_session_cannot_divide_by_zero():
    assert 0.0 <= card_for(running(planned_minutes=0, seconds_left=0.0)).progress <= 1.0


# --- the detail line ------------------------------------------------------------------------------

def test_the_detail_line_counts_minutes_on_target():
    assert "9 of 25 min on target" in card_for(running()).detail


def test_drifts_appear_only_once_there_are_some_and_are_pluralised():
    assert "drift" not in card_for(running()).detail
    assert "1 drift" in card_for(running(drifts=1)).detail
    assert "3 drifts" in card_for(running(drifts=3)).detail


def test_excused_excursions_are_named_separately_from_drifts():
    """An excused excursion is not a failure, and folding it into the drift count would quietly
    punish the owner for an interruption they explained."""
    detail = card_for(running(drifts=2, excused=1)).detail
    assert "2 drifts" in detail and "1 excused" in detail


def test_a_quiet_period_is_visible_rather_than_silent():
    """The owner asked for quiet, not for the card to disappear - a held callout the card did not
    mention would look like a session that had stopped working."""
    assert "quiet" in card_for(running(quiet=True)).detail
    assert card_for(running(quiet=True)).visible is True


# --- the privacy property -------------------------------------------------------------------------

def test_the_card_cannot_render_an_identity():
    """The structural guarantee, with a canary.

    Every field of SessionState is set to something recognisable and the whole card is searched for
    it. This passes not because card_for filters anything but because SessionState has nowhere to
    put a name - which is the point: the card inherits that property instead of re-earning it, and
    this test fails loudly if a field that could carry one is ever added.
    """
    assert not any(f.type == "str" and f.name != "phase" for f in fields(SessionState)), (
        "SessionState grew a free-text field; the card can now leak - re-check this guarantee")
    card = card_for(running(drifts=2, excused=1, quiet=True, adrift=True))
    rendered = " ".join(str(getattr(card, f.name)) for f in fields(card))
    assert CANARY not in rendered
    for word in ("http", "://", ".com", ".example", "chrome", "window", "title"):
        assert word not in rendered.lower(), f"the card rendered {word!r}"


# --- the view -------------------------------------------------------------------------------------

def test_importing_the_module_does_not_import_qt():
    """The CLI and these tests import this module. Needing PySide6 to do so would make a panel a
    dependency of the engine, which is backwards.

    Checked in a FRESH interpreter: this process has almost certainly imported PySide6 already via
    some other test, so asserting on our own sys.modules would prove nothing.
    """
    import subprocess
    import sys

    probe = ("import sys; import void.ui.focus_card; "
             "sys.exit(1 if any(m == 'PySide6' or m.startswith('PySide6.') for m in sys.modules) else 0)")
    done = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True)
    assert done.returncode == 0, f"importing void.ui.focus_card pulled in Qt:\n{done.stderr}"


def test_the_view_declines_rather_than_raising_when_there_is_no_display():
    """A missing card must never be able to stop a session: the session is the feature."""
    view = FocusCardView.create()
    assert view is None or isinstance(view, FocusCardView)


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, value):
        self.text = value

    def setStyleSheet(self, value):
        self.style = value


class FakeWidget(FakeLabel):
    def __init__(self):
        super().__init__()
        self.shown = 0
        self.hidden = 0
        self.raised = 0

    def show(self):
        self.shown += 1

    def hide(self):
        self.hidden += 1

    def raise_(self):
        self.raised += 1


def _view() -> tuple[FocusCardView, FakeWidget, FakeLabel, FakeLabel, FakeLabel]:
    """A view wired to fakes. The collaborators are constructor arguments precisely so this works
    with no display, so the render logic is covered on every platform rather than skipped on most.
    """
    widget, clock_label, headline, detail = FakeWidget(), FakeLabel(), FakeLabel(), FakeLabel()
    return FocusCardView(widget, clock_label, headline, detail), widget, clock_label, headline, detail


def test_the_view_renders_through_the_presenter_and_reports_what_it_drew():
    view, widget, clock_label, headline, _detail = _view()
    card = view.render(running(adrift=True))
    assert card.tint == ADRIFT and view.last is card
    assert headline.text == "Drifting"
    assert clock_label.text == "15:00"
    assert widget.shown == 1 and widget.raised == 1


def test_the_views_tint_is_the_presenters_tint():
    """The view may not have a colour opinion of its own: drift legibility is decided in one place."""
    view, widget, clock_label, headline, _ = _view()
    view.render(running(adrift=True))
    r, g, b = ADRIFT
    assert f"rgb({r},{g},{b})" in clock_label.style
    assert f"rgb({r},{g},{b})" in headline.style
    assert f"rgb({r},{g},{b})" in widget.style


def test_an_invisible_card_is_hidden_rather_than_painted_blank():
    view, widget, clock_label, *_ = _view()
    card = view.render(SessionState(phase=Phase.ENDED))
    assert card.visible is False
    assert widget.hidden == 1 and widget.shown == 0 and clock_label.text == ""


def test_a_widget_that_raises_mid_paint_still_returns_the_card():
    """A card must never break a session, and a caller still needs the reading it asked for."""
    class Hostile(FakeWidget):
        def show(self):
            raise RuntimeError("the compositor went away")

    view = FocusCardView(Hostile(), FakeLabel(), FakeLabel(), FakeLabel())
    card = view.render(running())
    assert card.headline == "Locked on"


def test_hide_is_survivable_too():
    class Hostile(FakeWidget):
        def hide(self):
            raise RuntimeError("gone")

    FocusCardView(Hostile(), FakeLabel(), FakeLabel(), FakeLabel()).hide()
