"""Screen watch: notice that the screen has been still too long, without looking at what is on it.

Astra's fourteenth prompt asks for an organ that diffs a thumbnail locally and "thinks only when
something has been still too long". This module is the noticing. It was declined in an earlier pass
on the grounds that its payoff is a model call about a picture of whatever the owner has open, which
is the most privacy-expensive thing in the corpus. Built now, deliberately, with that objection
answered in the design rather than in a comment:

**It decides WHEN, never WHAT.** The whole of this module runs on a 32x18 grid of block means. It
can tell that the picture changed; it cannot tell what the picture is, and it never asks. Acting on
a nudge - taking a real capture, sending it anywhere - stays where it already was: behind
``ScreenPolicy.allow_cloud_analysis``, the risk gate and the existing vision path. Nothing here
calls a model, and nothing here has a provider to call one with.

**Nothing is kept.** The only thing held between ticks is the previous grid of 576 floats. There is
no image, no path, no file, no window title, no hash of any of those - the same structural rule the
focus engine follows, for the same reason: the smaller the thing that can hold a record of what you
were doing, the less there is to protect. :class:`WatchState` is a closed set of numbers and
booleans, and ``test_the_state_cannot_carry_an_identity`` is what keeps it that way.

**Off unless asked for.** ``screen.watch_enabled`` defaults to false, under a ``screen.enabled``
that also defaults to false. A watch that has not been switched on takes no captures at all.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields

_log = logging.getLogger(__name__)

#: The thumbnail's shape, as (columns, rows). 32x18 keeps the 16:9 of most screens and is 576 block
#: means - coarse enough that no glyph, face or line of text survives it, fine enough that a window
#: changing in one corner still registers. Deliberately not configurable: the privacy argument above
#: depends on this being small, and a knob that could raise it would be a knob that could undo it.
GRID = (32, 18)

#: Mean absolute difference per cell, on a 0-255 scale, below which two thumbnails count as the same
#: screen. Two is above the noise of a blinking cursor and video compression artefacts in a window,
#: and well below a scroll, a window switch or a page of text appearing.
STILL_THRESHOLD = 2.0

#: How long the screen must be unchanged before a nudge is even considered. Ten minutes: long enough
#: that reading a long page, thinking, or being on a call does not trigger it.
STILL_FOR_S = 600.0

#: And never more often than this, however long the stillness lasts. A nudge repeated every ten
#: minutes at a screen the owner has walked away from is just an alarm.
NUDGE_EVERY_S = 1800.0

#: A screen this flat is asleep, a lock screen or a blanked monitor. Still, but not interesting -
#: nudging someone about their own screensaver is noise, so a blank screen never produces one.
BLANK_CONTRAST = 5.0


@dataclass(frozen=True)
class WatchState:
    """Everything a caller may see about a watch.

    A closed set of numbers and booleans. There is no field here that can hold an application name,
    a window title, a host, a URL or a pixel - not as text and not as a hash - and the test of the
    same name is what keeps it that way as this grows.
    """

    watching: bool = False
    #: Seconds the screen has been unchanged, by the watch's own clock.
    still_s: float = 0.0
    #: Ticks taken, and how many of them saw a change.
    ticks: int = 0
    changes: int = 0
    #: Nudges emitted. Never more than one per NUDGE_EVERY_S.
    nudges: int = 0
    #: True when the last tick's screen was too flat to be worth watching - asleep, locked, blanked.
    blank: bool = False
    #: The last tick's mean absolute difference per cell. A number, not a picture.
    change: float = 0.0
    #: True when the watch could not read the screen at all, with no reason attached.
    unavailable: bool = False


def thumbnail(array, grid: tuple[int, int] = GRID):
    """Reduce an image array to a coarse grid of block MEANS.

    Means rather than sampled pixels: a sampled grid misses a change that happens between its
    sample points, and a watch that can miss a window switch is not a watch. Averaging also
    destroys detail on purpose - whatever was on screen is gone by the time this returns, which is
    the property the rest of the module's privacy argument rests on.

    Returns a rows-by-columns float array. Raises nothing of its own: a caller that cannot make a
    thumbnail gets the exception from numpy and reports the tick as unavailable.
    """
    import numpy

    a = numpy.asarray(array, dtype=numpy.float32)
    if a.ndim == 3:
        a = a.mean(axis=2)
    if a.ndim != 2 or a.size == 0:
        raise ValueError(f"not an image array: shape {getattr(a, 'shape', None)}")
    height, width = a.shape
    cols, rows = grid
    # reduceat over edges computed with linspace handles a screen whose size is not a multiple of
    # the grid, which is every real screen.
    yedges = numpy.linspace(0, height, min(rows, height) + 1).astype(int)
    xedges = numpy.linspace(0, width, min(cols, width) + 1).astype(int)
    ycounts = numpy.maximum(1, numpy.diff(yedges))
    xcounts = numpy.maximum(1, numpy.diff(xedges))
    banded = numpy.add.reduceat(a, yedges[:-1], axis=0) / ycounts[:, None]
    return numpy.add.reduceat(banded, xedges[:-1], axis=1) / xcounts[None, :]


def difference(previous, current) -> float:
    """Mean absolute difference per cell between two thumbnails, on the array's own scale.

    Mean rather than max: one cell changing by a lot is a cursor or a clock, and the owner has not
    stopped working because a colon blinked.
    """
    import numpy

    if previous is None or current is None:
        return 0.0
    a, b = numpy.asarray(previous, dtype="float32"), numpy.asarray(current, dtype="float32")
    if a.shape != b.shape:
        # A resolution change, a monitor unplugged, a window captured instead of the desktop. That
        # is certainly a change, and comparing different shapes would be meaningless.
        return float("inf")
    return float(numpy.mean(numpy.abs(a - b)))


@dataclass(frozen=True)
class WatchPolicy:
    """What the owner's configuration permits for the WATCH, separately from screen capture itself.

    A separate type from ``ScreenPolicy`` on purpose: switching on "V.O.I.D may look at my screen
    when I ask" and "V.O.I.D may watch my screen continuously" are different decisions, and one
    should never imply the other.
    """

    enabled: bool = False
    still_for_s: float = STILL_FOR_S
    nudge_every_s: float = NUDGE_EVERY_S
    threshold: float = STILL_THRESHOLD

    @classmethod
    def from_config(cls, config) -> "WatchPolicy":
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                   # noqa: BLE001
                return default

        def positive(key, default):
            try:
                value = float(get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        # BOTH switches. screen.enabled is "may you capture at all"; watch_enabled is "may you do it
        # on a loop". Requiring both means a single mis-set key cannot start a continuous watch.
        on = bool(get("screen.enabled", False)) and bool(get("screen.watch_enabled", False))
        return cls(enabled=on,
                   still_for_s=positive("screen.watch_still_for_s", STILL_FOR_S),
                   nudge_every_s=positive("screen.watch_nudge_every_s", NUDGE_EVERY_S),
                   threshold=positive("screen.watch_threshold", STILL_THRESHOLD))


class StillnessWatch:
    """Notices that the screen has been still too long. Takes no action of its own.

    ``capture`` is injected so this is testable without a screen and so the caller - not this class -
    decides what a capture is. ``on_nudge`` receives a single content-free sentence; there is nothing
    else it could receive, because nothing else exists here.
    """

    def __init__(self, policy: WatchPolicy | None = None, *, capture=None, now=time.monotonic,
                 on_nudge=None):
        self.policy = policy or WatchPolicy()
        self._capture = capture
        self._now = now
        self._on_nudge = on_nudge
        self._previous = None
        self._still_since: float | None = None
        self._last_nudge: float | None = None
        self._state = WatchState(watching=self.policy.enabled)

    # --- the loop ---------------------------------------------------------------------------------

    def tick(self) -> WatchState:
        """One reading. Returns the state after it, and never raises.

        Driven by explicit ticks rather than by wall-clock arithmetic inside, for the same reason the
        focus engine is: a state machine whose behaviour depends on how long the caller slept is a
        state machine nobody can test.
        """
        if not self.policy.enabled:
            self._state = WatchState(watching=False)
            return self._state
        now = float(self._now())
        try:
            image = self._capture() if self._capture is not None else None
            if image is None:
                raise ScreenWatchUnavailable("no capture source")
            current = thumbnail(getattr(image, "array", image))
        except Exception:                       # noqa: BLE001 - a watch must survive a bad frame
            _log.debug("SCREEN_WATCH_TICK_UNAVAILABLE", exc_info=True)
            self._state = WatchState(watching=True, ticks=self._state.ticks + 1,
                                     changes=self._state.changes, nudges=self._state.nudges,
                                     still_s=self._state.still_s, unavailable=True)
            return self._state

        change = difference(self._previous, current)
        first = self._previous is None
        self._previous = current
        blank = _is_blank(current)

        if first:
            # The first tick establishes a baseline and nothing else. Treating it as "unchanged"
            # would start the stillness clock from a comparison that never happened.
            self._still_since = now
            self._state = WatchState(watching=True, ticks=self._state.ticks + 1, changes=0,
                                     nudges=self._state.nudges, blank=blank, change=0.0,
                                     still_s=0.0)
            return self._state

        changed = change > self.policy.threshold
        if changed:
            self._still_since = now
        elif self._still_since is None:
            self._still_since = now
        still_s = max(0.0, now - self._still_since)

        nudges = self._state.nudges
        if self._should_nudge(now, still_s, blank):
            nudges += 1
            self._last_nudge = now
            self._say(still_s)

        self._state = WatchState(watching=True, still_s=still_s,
                                 ticks=self._state.ticks + 1,
                                 changes=self._state.changes + (1 if changed else 0),
                                 nudges=nudges, blank=blank, change=change)
        return self._state

    def state(self) -> WatchState:
        return self._state

    def reset(self) -> None:
        """Forget the baseline and the stillness clock. The counters in ``state`` are kept.

        Called when the owner does something that makes the previous reading meaningless - a focus
        session starting, a machine waking - so the next tick establishes a fresh baseline instead
        of reporting a resolution change as a burst of activity.
        """
        self._previous = None
        self._still_since = None

    # --- the decision -----------------------------------------------------------------------------

    def _should_nudge(self, now: float, still_s: float, blank: bool) -> bool:
        """The whole of "think only when something has been still too long", and nothing more.

        A blank screen never nudges: asleep, locked and blanked are all still, and none of them is
        the owner staring at an unchanged window. Nudging someone about their own screensaver is
        noise, and noise is what makes a feature like this get switched off.
        """
        if blank or still_s < self.policy.still_for_s:
            return False
        if self._last_nudge is not None and (now - self._last_nudge) < self.policy.nudge_every_s:
            return False
        return True

    def _say(self, still_s: float) -> None:
        if self._on_nudge is None:
            return
        try:
            self._on_nudge(nudge_line(still_s))
        except Exception:                       # noqa: BLE001 - a listener may not break the watch
            _log.debug("SCREEN_WATCH_NUDGE_FAILED", exc_info=True)


class ScreenWatchUnavailable(RuntimeError):
    """The screen could not be read for a tick. Carries no detail about what was on it."""


def nudge_line(still_s: float) -> str:
    """What the watch says. Content-free by construction: it has nothing else to say.

    The sentence names a duration and asks a question. It cannot describe the screen, because
    nothing in this module ever knew what was on it - which is also why this is a plain function
    with no access to a model.
    """
    minutes = int(max(0.0, float(still_s)) // 60)
    return (f"Your screen has not changed in {minutes} minutes. Still on this, or shall I leave "
            f"you to it?")


def _is_blank(thumb) -> bool:
    import numpy

    try:
        return bool(float(numpy.std(numpy.asarray(thumb, dtype="float32"))) < BLANK_CONTRAST)
    except Exception:                           # noqa: BLE001
        return False


def from_config(config, *, capture=None, on_nudge=None) -> "StillnessWatch | None":
    """Build a watch from configuration, or None when it has not been switched on.

    None rather than a disabled object, so a caller cannot accidentally drive a watch the owner
    never asked for: there is nothing to drive.
    """
    policy = WatchPolicy.from_config(config)
    if not policy.enabled:
        return None
    if capture is None:
        from void.perception import screen as screen_module

        screen_policy = screen_module.ScreenPolicy.from_config(config)

        def capture():
            return screen_module.capture(max_width=screen_policy.max_width,
                                         foreground_only=screen_policy.foreground_only)

    return StillnessWatch(policy, capture=capture, on_nudge=on_nudge)


#: The field names a caller may rely on. Mirrors LEDGER_KEYS in void.focus: a published surface that
#: a test can assert has not quietly grown something it should not hold.
STATE_KEYS = tuple(f.name for f in fields(WatchState))
