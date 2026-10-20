"""Persistent V.O.I.D desktop application: the runtime coordinator.

The composition root for the always-on desktop presence. Constructs exactly
ONE Assistant, ONE VoiceController (the sole physical-microphone owner, via
its AudioCaptureBroker), ONE VoiceStateBridge, and the orb widget, then owns
the Qt event loop and the single shutdown funnel.

VoidRuntime itself never imports PySide6 at module scope and never touches
RiskGate/ToolRegistry/KillSwitch internals or the Agent execution funnel - it
only calls existing public Assistant/VoiceController methods (run/approve/
deny/stop/clear_stop/start/shutdown). This keeps it testable with plain
fakes, with no QApplication or display required; build_runtime()/main() below
are the only places real PySide6 objects are constructed.

KillSwitch semantics for the persistent process (deliberately different from
a one-shot CLI run): engaging the kill switch must NEVER shut down this
application. It halts execution through the existing, unmodified mechanisms
(Agent checks raise_if_engaged() at every checkpoint; VoiceSession self-
coerces to STOPPED on its own next poll() tick) and is reflected in the UI.
Only closing the window, or an explicit call to shutdown(), ends the process.
KillSwitch remains authoritative for EXECUTION; it does not control process
lifetime here, and clearing/rearming it is never automatic.
"""
from __future__ import annotations

import logging
from typing import Callable

from void.app import Assistant

_log = logging.getLogger(__name__)

# Killswitch-watch cadence: a cheap boolean property read, same order of
# magnitude as VoiceController's own monitor loop (100ms default).
_DEFAULT_KILLSWITCH_POLL_MS = 200

# Focus-card cadence. The countdown is read in minutes and seconds, so twice a second is already
# finer than the thing it displays; anything faster would just be redraws nobody can perceive. The
# card never DRIVES the engine - FocusEngine ticks itself on its own daemon thread so a session
# outlives whoever started it - this only reads state() and paints.
_DEFAULT_FOCUS_CARD_POLL_MS = 500

# Screen-watch cadence. Five seconds: the shortest stillness the watch reacts to is ten minutes, so
# this is already two orders of magnitude finer than the decision it feeds, and each tick is one
# downscale to 576 numbers. Off entirely unless BOTH screen.enabled and screen.watch_enabled are
# true, so on a default installation this timer is never armed.
_DEFAULT_SCREEN_WATCH_POLL_MS = 5000


class VoidRuntime:
    """Owns the persistent app's composition and lifecycle.

    Takes already-built collaborators (build_runtime() below builds the real
    ones). Everything Qt-specific is optional and duck-typed - this class has
    no hard PySide6 dependency of its own except inside the default (fully
    overridable) killswitch-watch timer.
    """

    def __init__(self, assistant, voice_controller, bridge, widget=None, *,
                 quit_fn: Callable[[], None] | None = None,
                 timer_factory: Callable[[], object] | None = None,
                 killswitch_poll_ms: int = _DEFAULT_KILLSWITCH_POLL_MS,
                 focus_card=None,
                 focus_card_poll_ms: int = _DEFAULT_FOCUS_CARD_POLL_MS,
                 screen_watch=None,
                 screen_watch_poll_ms: int = _DEFAULT_SCREEN_WATCH_POLL_MS):
        self.assistant = assistant
        self.voice_controller = voice_controller
        self.bridge = bridge
        self.widget = widget
        self._quit_fn = quit_fn or (lambda: None)
        self._timer_factory = timer_factory
        self.killswitch_poll_ms = killswitch_poll_ms
        self.focus_card = focus_card
        self.focus_card_poll_ms = focus_card_poll_ms
        self.screen_watch = screen_watch
        self.screen_watch_poll_ms = screen_watch_poll_ms
        self._timer = None
        self._focus_timer = None
        self._watch_timer = None
        self._shutting_down = False

    # --- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start voice (the sole mic owner), show the widget (if any), and
        begin observing KillSwitch. Does not run the Qt event loop itself -
        the caller (main(), below) does that with app.exec()."""
        self.voice_controller.start()
        if self.widget is not None:
            self.widget.show()
        self._start_killswitch_watch()
        self._start_focus_card()
        self._start_screen_watch()

    def _start_killswitch_watch(self) -> None:
        """A polling timer, NOT a new KillSwitch API. KillSwitch itself is
        not modified or subscribed to - this only reads the existing
        `.engaged` property on a cadence, exactly as VoiceController's own
        monitor thread already does for voice. It never calls engage()/
        reset()/approve()/deny() - it cannot become a second authorization
        path, only a read-only observer."""
        factory = self._timer_factory
        if factory is None:
            try:
                from PySide6.QtCore import QTimer
            except ImportError:
                return
            factory = QTimer
        self._timer = factory()
        self._timer.setInterval(self.killswitch_poll_ms)
        self._timer.timeout.connect(self.poll_killswitch)
        self._timer.start()

    def _start_focus_card(self) -> None:
        """Poll the focus engine and paint the card. Built on demand, and optional.

        Deliberately a read-only observer of ``FocusEngine.state()``, like the killswitch watch
        above: it never starts, ends, pauses or ticks a session. A card that could change the
        session it displays would be a second control path over the owner's attention, and there is
        no reason for one to exist.
        """
        if self.focus_card is None:
            try:
                from void.ui.focus_card import FocusCardView
                self.focus_card = FocusCardView.create()
            except Exception:                   # noqa: BLE001 - no Qt, no display: not an error
                self.focus_card = None
        if self.focus_card is None:
            return
        factory = self._timer_factory
        if factory is None:
            try:
                from PySide6.QtCore import QTimer
            except ImportError:
                return
            factory = QTimer
        self._focus_timer = factory()
        self._focus_timer.setInterval(self.focus_card_poll_ms)
        self._focus_timer.timeout.connect(self.poll_focus_card)
        self._focus_timer.start()

    def poll_focus_card(self) -> None:
        """One card tick: read the session's published state, paint it, and nothing else.

        Returns early when focus is switched off or no engine exists, so a machine with
        ``focus.enabled: false`` never builds one just to draw a card. Swallows its own failures for
        the same reason the card itself does: the session is the feature and the card is a courtesy,
        so a panel that cannot paint must not be able to take the assistant down with it.
        """
        card = self.focus_card
        if card is None:
            return
        try:
            engine = getattr(self.assistant, "focus", None)
            if engine is None:
                card.hide()
                return
            card.render(engine.state())
        except Exception:                       # noqa: BLE001
            _log.debug("FOCUS_CARD_TICK_FAILED", exc_info=True)

    def _start_screen_watch(self) -> None:
        """Arm the stillness watch, if and only if the owner switched it on.

        ``from_config`` returns None unless BOTH screen.enabled and screen.watch_enabled are true,
        so on a default installation there is nothing to drive and no timer is created - a watch of
        the owner's screen is not something to arrange by accident.
        """
        if self.screen_watch is None:
            try:
                from void.perception.stillness import from_config as watch_from_config
                self.screen_watch = watch_from_config(
                    self.assistant.config,
                    on_nudge=getattr(self.assistant, "on_event", None))
            except Exception:                   # noqa: BLE001 - absent numpy, no display, no Qt
                self.screen_watch = None
        if self.screen_watch is None:
            return
        factory = self._timer_factory
        if factory is None:
            try:
                from PySide6.QtCore import QTimer
            except ImportError:
                return
            factory = QTimer
        self._watch_timer = factory()
        self._watch_timer.setInterval(self.screen_watch_poll_ms)
        self._watch_timer.timeout.connect(self.poll_screen_watch)
        self._watch_timer.start()

    def poll_screen_watch(self) -> None:
        """One watch tick. ``StillnessWatch.tick`` already swallows its own failures; this is the
        second belt, for the case where the watch object itself is wrong."""
        watch = self.screen_watch
        if watch is None:
            return
        try:
            watch.tick()
        except Exception:                       # noqa: BLE001
            _log.debug("SCREEN_WATCH_TICK_FAILED", exc_info=True)

    def poll_killswitch(self) -> None:
        """One watch tick. Deliberately does nothing beyond an optional,
        purely-visual hook: it must NEVER call shutdown(), engage(), reset(),
        approve(), or deny(). Execution already stops through the existing
        Agent/VoiceSession checks with no help needed here; this exists only
        so a future visual layer can reflect STOPPED without polling
        KillSwitch itself."""
        engaged = bool(getattr(self.assistant.kill_switch, "engaged", False))
        on_tick = getattr(self.widget, "on_killswitch_state", None)
        if callable(on_tick):
            on_tick(engaged)

    def shutdown(self, reason: str = "shutdown") -> None:
        """The ONE shutdown funnel - triggered by the widget closing, or by
        an explicit caller. Idempotent: safe to call more than once. Never
        triggered by KillSwitch engagement alone (see poll_killswitch).

        Order: stop observing KillSwitch -> VoiceController.shutdown()
        (releases the microphone; existing, unmodified internal ordering -
        activation stop -> wake teardown -> monitor join -> session.close()
        -> broker.close() -> worker stop) -> quit Qt. Threads are never
        force-killed; VoiceController.shutdown() already joins them with
        bounded timeouts.
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        for attr in ("_timer", "_focus_timer", "_watch_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
                setattr(self, attr, None)
        # The card is hidden rather than left painted over whatever the owner does next: a
        # countdown still sitting top-right after the assistant has gone would be a window with
        # nothing behind it, and no obvious way to dismiss.
        if self.focus_card is not None:
            try:
                self.focus_card.hide()
            except Exception:
                pass
        self.voice_controller.shutdown(reason)
        self._quit_fn()


def build_runtime(
    *, confirm_fn=None,
    assistant_factory: Callable[[], object] | None = None,
    bridge_factory: Callable[[], object] | None = None,
    voice_controller_factory: Callable[[object, object], object] | None = None,
    widget_factory: Callable[[object, object, object], object] | None = None,
    qapplication_factory: Callable[[], object] | None = None,
):
    """Compose exactly ONE Assistant, ONE VoiceController (the sole
    microphone owner), ONE VoiceStateBridge, and ONE VoidWidget, wrapped in a
    VoidRuntime. Every factory defaults to the real class; tests override any
    or all of them with fakes, so this function never requires a display -
    or even PySide6 - to be exercised in isolation.

    confirm_fn defaults to None (deferred confirmation): the persistent app's
    Assistant is built the SAME way cmd_voice() already builds one, so a
    HIGH-risk action reached via voice still lands on durable
    AWAITING_CONFIRMATION rather than any synchronous approval - "voice
    cannot authorize HIGH-risk actions" is unchanged by this step.
    """
    if assistant_factory is None:
        assistant_factory = lambda: Assistant(confirm_fn=confirm_fn)
    if bridge_factory is None:
        from void.ui.voice_bridge import VoiceStateBridge
        bridge_factory = VoiceStateBridge
    if voice_controller_factory is None:
        from void.voice.runtime import VoiceController

        def voice_controller_factory(assistant, bridge):
            return VoiceController.from_assistant(
                assistant,
                on_state=bridge.stateChanged.emit,
                on_transcript=bridge.transcriptReceived.emit,
                on_message=bridge.messageReceived.emit,
            )
    if widget_factory is None:
        from void.ui.widget import VoidWidget

        def widget_factory(assistant, bridge, voice_controller):
            return VoidWidget(assistant, voice_bridge=bridge,
                              voice_controller=voice_controller)
    if qapplication_factory is None:
        import sys as _sys
        from PySide6.QtWidgets import QApplication

        def qapplication_factory():
            app = QApplication.instance()
            return app if app is not None else QApplication(_sys.argv)

    app = qapplication_factory()
    assistant = assistant_factory()
    bridge = bridge_factory()
    voice_controller = voice_controller_factory(assistant, bridge)
    widget = widget_factory(assistant, bridge, voice_controller)

    runtime = VoidRuntime(assistant, voice_controller, bridge, widget,
                          quit_fn=getattr(app, "quit", lambda: None))
    closing = getattr(widget, "closing", None)
    if closing is not None:
        closing.connect(lambda: runtime.shutdown("ui closed"))
    return runtime, app


def main() -> int:
    """Entry point for `python -m void app`."""
    runtime, app = build_runtime()
    runtime.start()
    return app.exec()


if __name__ == "__main__":
    import sys
    sys.exit(main())
