"""Focus sessions: an accountability timer that notices where the owner actually is.

The capability, in one sentence: the owner says "thirty minutes on this", V.O.I.D locks on to whatever
they settle in front of, and says something when they wander off. The interesting part is not the
timer - it is the three properties that make a timer like this tolerable to live with, each of which
the Astra pack reports getting wrong first and which are built in here from the start.

**A wrong lock is worse than no lock.** A session started while the owner is looking at V.O.I.D's own
window must not lock V.O.I.D's own window - that is the one surface they are guaranteed to leave. So
the lock DEFERS: nothing is a target until the owner has stayed somewhere that is not home base for
:data:`SETTLE_TICKS` consecutive reads, and the session says out loud that it is waiting. Nothing is
ever guessed from a background window or a second monitor, and if the owner never leaves home base
the session falls back to app-only after :data:`DEFER_FALLBACK_S` rather than inventing a target.

**Privacy is structural, not a promise.** The name of the application or site the owner drifted into
exists for exactly one tick: it is read, turned into a spoken sentence, and dropped.
:class:`SessionState` - everything any caller can see - is a closed set of booleans and counters, and
:class:`Ledger` writes a closed set of aggregate keys. Neither can carry an identity, not even a
hashed one, and ``tests/test_focus.py`` proves it with a label that appears nowhere else in the
repository.

**Every knob is a named constant with a comment.** They are below, they are tuned from logged
numbers rather than from memory, and the defaults are demo-speed rather than what a person would want
on a Tuesday - which is said here rather than discovered later.

What this layer is not: it is not an authority. It observes the foreground through the capability
layer that already exists (``void/focus/reader.py``), it speaks through the voice path that already
exists, and it cannot launch, close, block or hide anything. There is no enforcement in it at all,
only accounting and a sentence.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field, replace

from void.focus import lines as callout_lines
from void.focus.reader import APP, TAB, SurfaceReader, Surface

_log = logging.getLogger(__name__)

# --- the knobs -------------------------------------------------------------------------------
#: How often the engine looks. One second is what makes a drift audible within three; longer and the
#: callout lands after the owner has already come back, which reads as a machine talking to itself.
TICK_S = 1.0

#: How long a switch is allowed to be before it counts as a drift. Reaching for a reference, a
#: notification stealing focus for an instant, an alt-tab that goes the wrong way: all forgiven.
GRACE_S = 0.8

#: Consecutive reads on one non-home surface before it becomes the target. Two, because one read can
#: catch a window that is passing through.
SETTLE_TICKS = 2

#: How long the engine waits for the owner to settle before locking the application only. If they
#: never leave home base there is no tab to lock, and waiting forever would mean a session that
#: never watched anything.
DEFER_FALLBACK_S = 45.0

#: How often a callout repeats while ONE drift continues. The owner can change it by voice.
NAG_EVERY_S = 30.0

#: How long a "give me fifteen seconds" lasts, and the ceiling on what the owner can ask for.
SNOOZE_S = 15.0
MAX_SNOOZE_S = 300.0

#: A session at or above this share of planned minutes on target grows the streak. 85 per cent is
#: deliberately not 100: a session nobody can ever win is a session nobody runs twice.
CLEAN_SHARE = 0.85

#: Ceiling on a session's planned length, and the floor. Both exist so that a misheard number
#: ("thirty" as "three hundred") cannot start an eight-hour session.
MIN_MINUTES = 1
MAX_MINUTES = 240

#: Longest intent the engine will keep. The intent is the owner's own words, spoken back once in the
#: first callout; anything longer than a phrase is not an intent, it is a paragraph.
MAX_INTENT_CHARS = 120


class Phase:
    """What a session is doing. A closed set, so a client can switch on it safely."""

    IDLE = "idle"
    #: Running, but with no target yet: waiting for the owner to settle somewhere.
    DEFERRED = "deferred"
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"
    ABORTED = "aborted"

    ALL = frozenset({IDLE, DEFERRED, RUNNING, PAUSED, ENDED, ABORTED})


@dataclass(frozen=True)
class SessionState:
    """Everything a caller may see about a running session.

    A closed set of booleans, counters and durations. There is no field here that can hold the name
    of an application, a window title, a host or a URL - not as text and not as a hash - and
    :func:`_assert_no_identities` in the tests is what keeps it that way as this grows.
    """

    phase: str = Phase.IDLE
    planned_minutes: int = 0
    #: Seconds since the session started, excluding time it was paused.
    active_s: float = 0.0
    #: Seconds of ``active_s`` spent on the locked surface.
    on_target_s: float = 0.0
    seconds_left: float = 0.0
    #: Completed excursions away from the target.
    drifts: int = 0
    #: Excursions the owner excused, and the seconds refunded by those excuses.
    excused: int = 0
    excused_s: float = 0.0
    #: True while the owner is away from the target right now.
    adrift: bool = False
    #: True while the lock is deferred: the session is running but nothing is locked yet.
    deferred: bool = False
    #: How many consecutive reads the current candidate surface has held.
    settle_ticks: int = 0
    #: Which lanes are locked. Tab is false whenever the browser layer could not say which tab.
    app_locked: bool = False
    tab_locked: bool = False
    #: True while a snooze or an excuse is holding the callouts.
    quiet: bool = False
    #: True when the owner told the session what they were working on.
    has_intent: bool = False
    #: True when the last foreground read failed. A sensor that cannot see must say so.
    blind: bool = False
    #: Spoken callouts so far this session.
    callouts: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ReportCard:
    """What the owner hears at the end. Aggregates only, by construction."""

    planned_minutes: int = 0
    active_minutes: float = 0.0
    on_target_minutes: float = 0.0
    share: float = 0.0
    drifts: int = 0
    excused: int = 0
    clean: bool = False
    streak: int = 0
    completed: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    def spoken(self) -> str:
        """One engine-authored sentence. No identity, nothing from a model, nothing from a window."""
        minutes = f"{self.on_target_minutes:.0f} of {self.planned_minutes}"
        drift = ("no detours" if not self.drifts else
                 f"{self.drifts} detour{'s' if self.drifts != 1 else ''}")
        tail = (f" That is {self.streak} clean in a row." if self.clean and self.streak > 1
                else " Clean." if self.clean else "")
        return f"{minutes} minutes on target, {drift}.{tail}"


#: The only keys a ledger row may hold. A row is an aggregate record of one session and must never
#: become a history of what the owner had open, so the whitelist is enforced on write rather than
#: trusted - see :meth:`Ledger.record`.
LEDGER_KEYS = ("started_at", "planned_minutes", "active_minutes", "on_target_minutes", "share",
               "drifts", "excused", "clean", "completed")


class Ledger:
    """Aggregates per finished session, and a streak. Nothing else may be written to it.

    Deliberately in-memory plus an optional JSON-lines file: a focus history is not state V.O.I.D
    needs to survive anything, and the smaller the thing that can hold a record of the owner's
    attention, the less there is to protect.
    """

    def __init__(self, path=None, *, now=time.time):
        self.path = path
        self._now = now
        self.rows: list[dict] = []
        self.streak = 0

    def record(self, card: ReportCard, *, started_at: float | None = None) -> dict:
        row = {"started_at": float(started_at if started_at is not None else self._now()),
               "planned_minutes": int(card.planned_minutes),
               "active_minutes": round(float(card.active_minutes), 2),
               "on_target_minutes": round(float(card.on_target_minutes), 2),
               "share": round(float(card.share), 3),
               "drifts": int(card.drifts), "excused": int(card.excused),
               "clean": bool(card.clean), "completed": bool(card.completed)}
        # Enforced, not trusted: a future field added to ReportCard cannot leak into the ledger by
        # being copied in wholesale.
        row = {key: row[key] for key in LEDGER_KEYS}
        self.streak = self.streak + 1 if card.clean else 0
        self.rows.append(row)
        self._append(row)
        return row

    def _append(self, row: dict) -> None:
        if self.path is None:
            return
        import json
        from pathlib import Path

        from void.security import ownership
        try:
            target = Path(self.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
            ownership.restrict(target, directory=False)
        except OSError as exc:
            _log.warning("FOCUS_LEDGER_WRITE_FAILED errno=%s", getattr(exc, "errno", "?"))


class FocusEngine:
    """One focus session at a time, driven by explicit ticks.

    The engine is a state machine over readings, with no clock and no thread of its own: every method
    takes the time it is being called at. That is what makes the drift grace, the nag cadence, the
    settle count and the report card testable to the second instead of approximately - and
    :meth:`start_ticking` is the thin production wrapper that calls :meth:`tick` once a second.
    """

    def __init__(self, reader: SurfaceReader, *, speak=None, ledger: Ledger | None = None,
                 now=time.monotonic, naming: bool = True, pick=None):
        self._reader = reader
        self._speak = speak or (lambda _line: None)
        self._ledger = ledger if ledger is not None else Ledger()
        self._now = now
        #: Naming the distraction out loud can be switched off entirely; the pools without a
        #: placeholder are used instead and no label is ever derived.
        self.naming = bool(naming)
        self._pick = pick
        self._reset()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- lifecycle -----------------------------------------------------

    def _reset(self) -> None:
        self._phase = Phase.IDLE
        self._planned_s = 0.0
        self._started_at = 0.0
        self._started_wall = 0.0
        self._last_tick = 0.0
        self._active_s = 0.0
        self._on_target_s = 0.0
        #: The target, as opaque handles. Never exposed and never written down.
        self._target_app = ""
        self._target_tab = ""
        self._deferred = False
        self._candidate = ""
        self._settle = 0
        self._adrift_since = 0.0
        self._adrift = False
        self._excursion_s = 0.0
        self._drifts = 0
        self._excursions = 0
        self._excused = 0
        self._excused_s = 0.0
        self._quiet_until = 0.0
        self._last_callout = 0.0
        self._callouts = 0
        self._nag_every_s = NAG_EVERY_S
        self._intent = ""
        self._blind = False
        self._said_first = False
        #: When the session was paused, so that neither the deadline nor the countdown ages while it
        #: is. Without this, `tick` moved the last-tick mark during a pause and `resume` then had no
        #: pause duration to give back - a session paused for an hour expired in its own absence.
        self._paused_at = 0.0
        #: The card from the last `end`, so that a session which ended ITSELF when its time was up
        #: can still be asked for its report.
        self._last_card = ReportCard()

    def start(self, minutes: int, *, now: float | None = None, intent: str = "") -> str:
        """Begin a session. Returns the line to say.

        The lock is deferred whenever the owner is at home base or the foreground cannot be read -
        the two cases where locking now would lock the wrong thing - and the line says so, because a
        deferred lock nobody mentions is the same as a broken one.
        """
        at = self._now() if now is None else now
        planned = max(MIN_MINUTES, min(int(minutes), MAX_MINUTES))
        self._reset()
        self._phase = Phase.RUNNING
        self._planned_s = planned * 60.0
        self._started_at = at
        self._started_wall = time.time()
        self._last_tick = at
        self._intent = " ".join(str(intent or "").split())[:MAX_INTENT_CHARS]

        surface = self._reader.read()
        self._blind = not surface.readable
        if surface.readable and not surface.is_home:
            self._lock(surface)
            return (f"{planned} minutes. Locked on." if not self._target_tab
                    else f"{planned} minutes. Locked on this tab.")
        self._deferred = True
        self._phase = Phase.DEFERRED
        return (f"{planned} minutes. Go to what you are working on and I will lock on there."
                + (" And what are we focusing on?" if not self._intent else ""))

    def set_intent(self, intent: str) -> str:
        """Attach what the owner said they are working on. Never parroted back in full."""
        self._intent = " ".join(str(intent or "").split())[:MAX_INTENT_CHARS]
        return "Noted." if self._intent else "Nothing noted."

    def _lock(self, surface: Surface) -> None:
        """Take a surface as the target, keeping only its opaque handles."""
        self._target_app = surface.app_handle
        self._target_tab = surface.tab_handle
        self._deferred = False
        self._candidate = ""
        self._settle = 0
        self._phase = Phase.RUNNING
        # Handles only, and even those are not logged: the count is the diagnostic.
        _log.info("FOCUS_LOCKED tab_lane=%s", bool(self._target_tab))

    # --- the tick ------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        """Advance the session by one reading. Never raises."""
        at = self._now() if now is None else now
        if self._phase == Phase.PAUSED:
            return                                    # a pause freezes the clock: see _paused_at
        if self._phase not in (Phase.RUNNING, Phase.DEFERRED):
            self._last_tick = at
            return
        elapsed = max(0.0, at - self._last_tick)
        self._last_tick = at
        self._active_s += elapsed

        try:
            surface = self._reader.read()
        except Exception as exc:                      # a sensor must not end a session
            _log.info("FOCUS_READ_FAILED reason=%s", type(exc).__name__)
            surface = None
        self._blind = surface is None or not surface.readable

        if self._deferred:
            self._settle_step(surface, at, elapsed)
        elif self._blind:
            # Blind is not adrift. A sensor that cannot see must not accuse the owner of anything;
            # the time is counted as neither on target nor away.
            pass
        else:
            self._target_step(surface, at, elapsed)

        if self._planned_s and (at - self._started_at) >= self._planned_s:
            self.end(now=at, completed=True)

    def _settle_step(self, surface, at: float, elapsed: float) -> None:
        """While the lock is deferred: wait for somewhere that is not home base to hold."""
        if surface is not None and surface.readable and not surface.is_home:
            if surface.app_handle == self._candidate:
                self._settle += 1
            else:
                self._candidate = surface.app_handle
                self._settle = 1
            if self._settle >= SETTLE_TICKS:
                self._lock(surface)
                self._say("Locked on.")
                return
        else:
            self._candidate = ""
            self._settle = 0
        if (at - self._started_at) >= DEFER_FALLBACK_S and surface is not None and surface.readable:
            # The owner never left home base. Lock the application only rather than invent a tab.
            self._target_app = surface.app_handle
            self._target_tab = ""
            self._deferred = False
            self._phase = Phase.RUNNING
            self._say("Locking on where you are, then.")

    def _target_step(self, surface: Surface, at: float, elapsed: float) -> None:
        """While locked: on target, or away, with grace, escalation and a nag cadence."""
        if self._on_target(surface):
            if self._adrift:
                self._end_excursion()
            self._on_target_s += elapsed
            self._adrift = False
            return
        if surface.is_home:
            # Home base is never a drift: coming back to talk to V.O.I.D is part of the work.
            return
        if not self._adrift:
            self._adrift = True
            self._adrift_since = at
            self._excursion_s = 0.0
            return
        self._excursion_s += elapsed
        if (at - self._adrift_since) < GRACE_S:
            return
        if at < self._quiet_until:
            return
        if self._last_callout and (at - self._last_callout) < self._nag_every_s:
            return
        self._callout(surface, at)

    def _on_target(self, surface: Surface) -> bool:
        if not self._target_app:
            return True                               # nothing locked: nothing to be away from
        if surface.app_handle != self._target_app:
            return False
        if self._target_tab and surface.tab_handle and surface.tab_handle != self._target_tab:
            return False
        # The right application with an unreadable tab is on target: the tab lane going quiet is a
        # sensor problem, and a sensor problem must never be reported as the owner's fault.
        return True

    def _callout(self, surface: Surface, at: float) -> None:
        """Say one line. The label is derived here, used here, and not kept.

        The only place in the focus layer where an identity becomes a sentence. Nothing in this
        method writes the label anywhere: it is a local, it goes into ``line``, and the frame is
        discarded when the method returns.
        """
        label = ""
        if self.naming:
            label = (callout_lines.spoken_host(surface.host) if surface.host
                     else surface.app_label)
        tier = min(self._excursions + 1, callout_lines.MAX_TIER)
        first = not self._said_first and bool(self._intent)
        kwargs = {"intent": self._intent, "first": first}
        if self._pick is not None:
            kwargs["pick"] = self._pick
        line = callout_lines.callout(tier, label, **kwargs)
        self._said_first = self._said_first or first
        self._last_callout = at
        self._callouts += 1
        self._say(line)

    def _end_excursion(self) -> None:
        self._drifts += 1
        self._excursions += 1
        self._adrift = False
        self._excursion_s = 0.0
        self._last_callout = 0.0

    def _say(self, line: str) -> None:
        try:
            self._speak(line)
        except Exception as exc:                      # speaking must not end a session either
            _log.info("FOCUS_SPEAK_FAILED reason=%s", type(exc).__name__)

    # --- the owner's controls -----------------------------------------

    def snooze(self, seconds: float = SNOOZE_S, *, now: float | None = None) -> str:
        at = self._now() if now is None else now
        held = max(1.0, min(float(seconds), MAX_SNOOZE_S))
        self._quiet_until = at + held
        return f"Quiet for {held:.0f} seconds."

    def excuse(self, *, now: float | None = None) -> str:
        """Forgive the excursion in progress: refund its seconds and stay quiet until they return.

        The refund matters. Without it a legitimate detour - looking something up for the work - still
        costs the owner their report card, and a report card that punishes the work is one they stop
        believing.
        """
        at = self._now() if now is None else now
        if not self._adrift:
            self._quiet_until = at + SNOOZE_S
            return "Noted."
        self._on_target_s += self._excursion_s
        self._excused_s += self._excursion_s
        self._excused += 1
        self._excursion_s = 0.0
        self._quiet_until = float("inf")              # until they are back on target
        return "Fair enough. I will not count that one."

    def nag_every(self, seconds: float, *, now: float | None = None) -> str:
        self._nag_every_s = max(5.0, min(float(seconds), 600.0))
        return f"I will say something every {self._nag_every_s:.0f} seconds."

    def pause(self, *, now: float | None = None) -> str:
        if self._phase not in (Phase.RUNNING, Phase.DEFERRED):
            return "There is no session running."
        at = self._now() if now is None else now
        self._last_tick = at
        self._paused_at = at
        self._phase = Phase.PAUSED
        return "Paused."

    def resume(self, *, now: float | None = None) -> str:
        if self._phase != Phase.PAUSED:
            return "There is no paused session."
        at = self._now() if now is None else now
        # The clock the deadline is measured against moves with the pause, so a paused session does
        # not quietly expire while it is paused.
        self._started_at += max(0.0, at - self._paused_at)
        self._paused_at = 0.0
        self._last_tick = at
        self._phase = Phase.DEFERRED if self._deferred else Phase.RUNNING
        return "Back on."

    def extend(self, minutes: int) -> str:
        if self._phase not in (Phase.RUNNING, Phase.DEFERRED, Phase.PAUSED):
            return "There is no session to extend."
        added = max(MIN_MINUTES, min(int(minutes), MAX_MINUTES))
        self._planned_s += added * 60.0
        return f"{added} more minutes."

    def relock(self, *, now: float | None = None) -> str:
        """"Lock on this tab": take where the owner is now, and forgive the drift in progress.

        From home base this re-arms the deferral rather than locking home base, which is the trap
        the Astra pack names: the button lives in the assistant's own window, so "lock here" said
        there can only mean "lock where I am about to be".
        """
        if self._phase not in (Phase.RUNNING, Phase.DEFERRED):
            return "There is no session running."
        surface = self._reader.read()
        if not surface.readable:
            return "I cannot tell which window you are in."
        if surface.is_home:
            self._deferred = True
            self._phase = Phase.DEFERRED
            self._candidate = ""
            self._settle = 0
            return "Go to it and I will lock on where you land."
        self._lock(surface)
        self._adrift = False
        self._excursion_s = 0.0
        self._last_callout = 0.0
        self._quiet_until = 0.0
        return "Locked on this tab." if self._target_tab else "Locked on."

    def end(self, *, now: float | None = None, completed: bool = False) -> ReportCard:
        """Finish the session and write one aggregate row. Returns the report card."""
        at = self._now() if now is None else now
        if self._phase in (Phase.ENDED, Phase.ABORTED):
            return self._last_card            # a session that ended itself can still be asked
        if self._phase == Phase.IDLE:
            return ReportCard()
        if self._adrift:
            self._end_excursion()
        planned = int(round(self._planned_s / 60.0))
        active = self._active_s / 60.0
        on_target = self._on_target_s / 60.0
        share = (self._on_target_s / self._planned_s) if self._planned_s else 0.0
        clean = bool(completed and share >= CLEAN_SHARE)
        card = ReportCard(planned_minutes=planned, active_minutes=round(active, 2),
                          on_target_minutes=round(on_target, 2), share=round(share, 3),
                          drifts=self._drifts, excused=self._excused, clean=clean,
                          streak=0, completed=bool(completed))
        self._ledger.record(card, started_at=self._started_wall)
        card = replace(card, streak=self._ledger.streak)
        self._last_card = card
        self._phase = Phase.ENDED if completed else Phase.ABORTED
        _log.info("FOCUS_ENDED completed=%s share=%.2f drifts=%d", completed, share, self._drifts)
        return card

    def abort(self, *, now: float | None = None) -> str:
        if self._phase in (Phase.IDLE, Phase.ENDED, Phase.ABORTED):
            return "There is no session running."
        self.end(now=now, completed=False)
        return "Session abandoned."

    # --- what callers may see -----------------------------------------

    @property
    def running(self) -> bool:
        return self._phase in (Phase.RUNNING, Phase.DEFERRED, Phase.PAUSED)

    def state(self, *, now: float | None = None) -> SessionState:
        at = self._now() if now is None else now
        # While paused, the countdown is read at the moment of the pause, for the same reason the
        # deadline is: a paused session is not running down.
        measured_at = self._paused_at if self._phase == Phase.PAUSED else at
        left = (max(0.0, self._planned_s - (measured_at - self._started_at))
                if self._planned_s else 0.0)
        return SessionState(
            phase=self._phase, planned_minutes=int(round(self._planned_s / 60.0)),
            active_s=round(self._active_s, 2), on_target_s=round(self._on_target_s, 2),
            seconds_left=round(left, 1), drifts=self._drifts, excused=self._excused,
            excused_s=round(self._excused_s, 2), adrift=self._adrift, deferred=self._deferred,
            settle_ticks=self._settle, app_locked=bool(self._target_app),
            tab_locked=bool(self._target_tab), quiet=at < self._quiet_until,
            has_intent=bool(self._intent), blind=self._blind, callouts=self._callouts)

    def diag(self, *, now: float | None = None) -> dict:
        """Booleans and statuses only, read from the LIVE engine.

        The Astra pack's rule and the reason it exists: a fresh process can pass every check while
        the long-lived one is frozen, so the diagnostic has to come from the object that is actually
        running. No identity appears here, and neither does a target handle.
        """
        payload = self.state(now=now).as_dict()
        payload.update({
            "naming": self.naming,
            "nag_every_s": self._nag_every_s,
            "reads": getattr(self._reader, "reads", 0),
            "read_failures": getattr(self._reader, "failures", 0),
            "tick_thread_alive": bool(self._thread and self._thread.is_alive()),
            "streak": self._ledger.streak,
        })
        return payload

    # --- production ticking -------------------------------------------

    def start_ticking(self, interval_s: float = TICK_S) -> None:
        """Tick once a second on a daemon thread, so a session outlives whoever started it.

        Kept to this: a loop and a sleep. Everything decidable lives in :meth:`tick`, which is why
        the behaviour is testable without a thread and why this wrapper has nothing to get wrong.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval_s):
                try:
                    self.tick()
                except Exception:                     # the thread must outlive one bad tick
                    _log.exception("FOCUS_TICK_FAILED")

        self._thread = threading.Thread(target=loop, name="void-focus", daemon=True)
        self._thread.start()

    def stop_ticking(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)


def from_assistant(assistant, *, speak=None) -> "FocusEngine":
    """Build an engine over an Assistant's existing layers, or None when focus is switched off."""
    config = assistant.config
    if not config.get("focus.enabled", True):
        return None
    ledger_path = config.state_dir() / "focus.jsonl" if config.get("focus.ledger", True) else None
    return FocusEngine(
        SurfaceReader.from_assistant(assistant),
        speak=speak,
        ledger=Ledger(ledger_path),
        naming=bool(config.get("focus.name_the_distraction", True)))
