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

from typing import Callable

from void.app import Assistant

# Killswitch-watch cadence: a cheap boolean property read, same order of
# magnitude as VoiceController's own monitor loop (100ms default).
_DEFAULT_KILLSWITCH_POLL_MS = 200


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
                 killswitch_poll_ms: int = _DEFAULT_KILLSWITCH_POLL_MS):
        self.assistant = assistant
        self.voice_controller = voice_controller
        self.bridge = bridge
        self.widget = widget
        self._quit_fn = quit_fn or (lambda: None)
        self._timer_factory = timer_factory
        self.killswitch_poll_ms = killswitch_poll_ms
        self._timer = None
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
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
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
