"""The focus countdown card: a small always-on-top panel that tints when attention drifts.

Astra's ninth prompt asks for "a desktop countdown card pinned top-right that tints on drift". This
module is the half of it that can be reasoned about: a pure mapping from :class:`void.focus.SessionState`
to everything a view needs to draw, with no Qt, no window and no I/O, so every rule below is asserted
headlessly. :class:`FocusCardView` at the bottom is the only part that touches PySide6, and it imports
it inside its own methods - importing this module never requires Qt, exactly as ``void.ui.orb`` does.

Two properties are deliberate rather than incidental.

**The card cannot leak what you were looking at.** It is built from ``SessionState``, which holds a
closed set of booleans, counters and durations and has no field able to carry an application name, a
window title, a host or a URL - not as text and not as a hash. A card rendered from it therefore
cannot display one either, however the view changes later. ``test_the_card_cannot_render_an_identity``
pins that with a canary.

**Tint carries the drift, text carries the detail.** The pack wants drift legible at a glance from
the corner of the eye, which is a colour job; the counters are for when you actually look at it. So
the tint is a function of phase and ``adrift`` alone, and never of the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass

from void.focus import Phase, SessionState

#: Seconds left at which the card starts reading as urgent. One minute: long enough to finish a
#: sentence, short enough that it is not a second nag.
URGENT_S = 60.0

#: Tints, as plain RGB so this module needs no Qt. Chosen for what they must do at the edge of
#: vision rather than for prettiness: ON_TARGET recedes, ADRIFT is the only warm one in the set so
#: that drift is the single thing that can catch the eye, and PAUSED/DEFERRED are desaturated
#: because neither is a state to be alarmed about.
ON_TARGET = (64, 150, 160)
ADRIFT = (198, 124, 56)
DEFERRED = (118, 122, 130)
PAUSED = (92, 96, 104)
URGENT = (86, 168, 150)

#: Phases with nothing to show. A finished session's card is the report card's business, not this one's.
HIDDEN_PHASES = frozenset({Phase.IDLE, Phase.ENDED, Phase.ABORTED})


def clock(seconds_left: float) -> str:
    """``seconds_left`` as m:ss, never negative and never a bare number of seconds.

    Rounds UP, so a card reading 1:00 means "up to a minute left" rather than "a minute ago": a
    countdown that shows 0:00 while time remains is the one reading that would make the owner
    distrust it.
    """
    remaining = max(0, int(-(-float(seconds_left) // 1)))
    return f"{remaining // 60}:{remaining % 60:02d}"


@dataclass(frozen=True)
class Card:
    """Everything a view needs, and nothing a view has to decide.

    Frozen because a card is a reading of one moment: a view that wants a different one asks for
    another. ``share`` and ``progress`` are 0.0-1.0 so a ring or bar needs no arithmetic of its own.
    """

    visible: bool
    countdown: str
    headline: str
    detail: str
    tint: tuple[int, int, int]
    progress: float
    share: float
    adrift: bool
    urgent: bool


def _detail(state: SessionState) -> str:
    """The second line: minutes on target, and the drift count once there is one.

    Minutes rather than seconds, because this line is read, not timed - and a second-by-second
    number in the corner of the screen is its own small distraction.
    """
    on_target = int(state.on_target_s // 60)
    planned = int(state.planned_minutes)
    line = f"{on_target} of {planned} min on target"
    if state.drifts:
        line += f" · {state.drifts} drift" + ("" if state.drifts == 1 else "s")
    if state.excused:
        line += f", {state.excused} excused"
    return line


def card_for(state: SessionState) -> Card:
    """Map a session's published state onto a card. Total: every phase returns something.

    An unknown phase is treated as hidden rather than guessed at. ``Phase.ALL`` is a closed set and
    a new member should be a deliberate decision here, not a card that renders whatever the last
    branch happened to leave.
    """
    if state.phase in HIDDEN_PHASES or state.phase not in Phase.ALL:
        return Card(visible=False, countdown=clock(0), headline="", detail="",
                    tint=PAUSED, progress=0.0, share=0.0, adrift=False, urgent=False)

    planned_s = max(1.0, float(state.planned_minutes) * 60.0)
    progress = min(1.0, max(0.0, 1.0 - (float(state.seconds_left) / planned_s)))
    share = (state.on_target_s / state.active_s) if state.active_s > 0 else 0.0
    urgent = state.phase == Phase.RUNNING and 0 < state.seconds_left <= URGENT_S

    if state.phase == Phase.PAUSED:
        headline, tint = "Paused", PAUSED
    elif state.deferred or state.phase == Phase.DEFERRED:
        # Running, but nothing is locked yet. Saying so is the honest headline: a countdown with no
        # target is not yet measuring anything, and a card that implied otherwise would be claiming
        # a lock the engine has not taken.
        headline, tint = "Waiting to lock", DEFERRED
    elif state.adrift:
        headline, tint = "Drifting", ADRIFT
    else:
        headline = "Locked on"
        tint = URGENT if urgent else ON_TARGET

    detail = _detail(state)
    if state.quiet:
        # A snooze or an excuse is holding the callouts. The card stays - hiding it would make the
        # quiet period invisible, and the owner asked for quiet, not for blindness.
        detail += " · quiet"

    return Card(visible=True, countdown=clock(state.seconds_left), headline=headline,
                detail=detail, tint=tint, progress=progress, share=min(1.0, max(0.0, share)),
                adrift=bool(state.adrift), urgent=urgent)


class FocusCardView:
    """The Qt half: a frameless, always-on-top card pinned to the top-right of the active screen.

    The only part of this module that touches PySide6, and it does so inside its methods so that
    importing ``void.ui.focus_card`` - which the tests and the CLI both do - never requires Qt.
    ``create`` returns None rather than raising when there is no display, because a missing panel
    must never be able to stop a focus session: the session is the feature, the card is a courtesy.
    """

    #: Distance from the screen's top-right corner, in logical pixels.
    MARGIN = 18
    SIZE = (232, 92)

    def __init__(self, widget, label_clock, label_headline, label_detail):
        self._widget = widget
        self._clock = label_clock
        self._headline = label_headline
        self._detail = label_detail
        self._last: Card | None = None

    @classmethod
    def create(cls, parent=None) -> "FocusCardView | None":
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
        except Exception:                       # noqa: BLE001 - no Qt, or no display: not an error
            return None
        try:
            widget = QWidget(parent)
            widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            widget.setAttribute(Qt.WA_ShowWithoutActivating)
            # Never take focus. A card that stole the caret from whatever the owner is concentrating
            # on would be sabotaging the thing it exists to protect.
            widget.setFocusPolicy(Qt.NoFocus)
            widget.resize(*cls.SIZE)
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(14, 10, 14, 10)
            layout.setSpacing(2)
            clock_label = QLabel("", widget)
            headline = QLabel("", widget)
            detail = QLabel("", widget)
            for lbl in (clock_label, headline, detail):
                lbl.setFocusPolicy(Qt.NoFocus)
                layout.addWidget(lbl)
            return cls(widget, clock_label, headline, detail)
        except Exception:                       # noqa: BLE001 - same reasoning as the import guard
            return None

    def render(self, state: SessionState) -> Card:
        """Draw one reading, and return the card drawn so a caller can assert on it."""
        card = card_for(state)
        self._last = card
        try:
            if not card.visible:
                self._widget.hide()
                return card
            r, g, b = card.tint
            self._clock.setText(card.countdown)
            self._headline.setText(card.headline)
            self._detail.setText(card.detail)
            self._clock.setStyleSheet(f"color: rgb({r},{g},{b}); font-size: 26px; font-weight: 600;")
            self._headline.setStyleSheet(f"color: rgb({r},{g},{b}); font-size: 12px;")
            self._detail.setStyleSheet("color: rgba(230,232,236,170); font-size: 10px;")
            self._widget.setStyleSheet(
                "QWidget { background-color: rgba(18,20,24,228); border-radius: 12px; "
                f"border: 1px solid rgb({r},{g},{b}); }}")
            # Placement is guarded SEPARATELY from showing. It was inside the same try, which meant
            # a screen that could not be resolved - an unplugged monitor, a compositor mid-restart -
            # stopped the card appearing at all rather than appearing in the wrong corner. A card in
            # the wrong corner is still a card.
            try:
                self._place()
            except Exception:                   # noqa: BLE001
                pass
            self._widget.show()
            self._widget.raise_()
        except Exception:                       # noqa: BLE001 - a card must never break a session
            pass
        return card

    def _place(self) -> None:
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.screenAt(self._widget.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        size = self._widget.size()
        self._widget.move(area.right() - size.width() - self.MARGIN, area.top() + self.MARGIN)

    def hide(self) -> None:
        try:
            self._widget.hide()
        except Exception:                       # noqa: BLE001
            pass

    @property
    def last(self) -> "Card | None":
        return self._last
