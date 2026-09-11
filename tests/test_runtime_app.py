"""Deterministic tests for the persistent-app runtime coordinator
(void.runtime.app). No QApplication, no display, no real Qt event loop -
every Qt-shaped collaborator is a plain-Python fake exposing only the small
surface VoidRuntime actually calls (.show()/.closing.connect()/
.timeout.connect()/.start()/.stop()/.quit()). KillSwitch is used for real
(it is plain Python, no Qt) so "engaging the kill switch does not shut the
app down" is proven against the real object, not a stand-in for it.
"""
from __future__ import annotations

from void.core.kill_switch import KillSwitch
from void.runtime.app import VoidRuntime, build_runtime


# --- fakes ----------------------------------------------------------------

class FakeAssistant:
    def __init__(self, kill_switch=None):
        self.kill_switch = kill_switch or KillSwitch()


class FakeVoiceController:
    def __init__(self):
        self.start_calls = 0
        self.shutdown_calls = []

    def start(self):
        self.start_calls += 1

    def shutdown(self, reason="shutdown"):
        self.shutdown_calls.append(reason)


class FakeSignal:
    """Stands in for a Qt Signal: records connected callbacks, fired
    manually by the test. No Qt/QObject involved."""
    def __init__(self):
        self._subs = []

    def connect(self, cb):
        self._subs.append(cb)

    def fire(self, *a):
        for cb in list(self._subs):
            cb(*a)


class FakeWidget:
    def __init__(self):
        self.show_calls = 0
        self.closing = FakeSignal()
        self.killswitch_ticks = []

    def show(self):
        self.show_calls += 1

    def on_killswitch_state(self, engaged):
        self.killswitch_ticks.append(engaged)


class FakeTimer:
    def __init__(self):
        self.interval = None
        self.timeout = FakeSignal()
        self.started = 0
        self.stopped = 0

    def setInterval(self, ms):
        self.interval = ms

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class FakeQApplication:
    def __init__(self):
        self.quit_calls = 0

    def quit(self):
        self.quit_calls += 1


def _runtime(**kw):
    assistant = kw.pop("assistant", None) or FakeAssistant()
    vc = kw.pop("voice_controller", None) or FakeVoiceController()
    bridge = kw.pop("bridge", None) or object()
    widget = kw.pop("widget", None) or FakeWidget()
    timer_factory = kw.pop("timer_factory", FakeTimer)
    quit_fn = kw.pop("quit_fn", None)
    rt = VoidRuntime(assistant, vc, bridge, widget, quit_fn=quit_fn,
                     timer_factory=timer_factory, **kw)
    return rt, assistant, vc, bridge, widget


# --- start(): composition / arming -----------------------------------------

def test_start_starts_voice_shows_widget_and_arms_killswitch_watch():
    rt, asst, vc, bridge, widget = _runtime()
    rt.start()
    assert vc.start_calls == 1
    assert widget.show_calls == 1
    assert rt._timer is not None and rt._timer.started == 1


def test_start_without_widget_does_not_crash():
    vc = FakeVoiceController()
    rt = VoidRuntime(FakeAssistant(), vc, object(), widget=None,
                     timer_factory=FakeTimer)
    rt.start()
    assert vc.start_calls == 1


# --- shutdown ordering -------------------------------------------------

def test_shutdown_calls_voice_controller_shutdown_before_quit():
    order = []
    vc = FakeVoiceController()
    orig_shutdown = vc.shutdown

    def tracked_shutdown(reason="shutdown"):
        order.append("voice_shutdown")
        orig_shutdown(reason)

    vc.shutdown = tracked_shutdown

    def quit_fn():
        order.append("quit")

    rt, *_ = _runtime(voice_controller=vc, quit_fn=quit_fn)
    rt.start()
    rt.shutdown("test")
    assert order == ["voice_shutdown", "quit"]
    assert vc.shutdown_calls == ["test"]


def test_shutdown_stops_the_killswitch_watch_timer():
    rt, *_ = _runtime()
    rt.start()
    timer = rt._timer
    rt.shutdown()
    assert timer.stopped == 1


def test_shutdown_is_idempotent():
    quits = []
    rt, asst, vc, bridge, widget = _runtime(quit_fn=lambda: quits.append(1))
    rt.start()
    rt.shutdown("first")
    rt.shutdown("second")
    assert vc.shutdown_calls == ["first"]        # second call is a no-op
    assert quits == [1]


def test_shutdown_without_start_still_calls_voice_controller_shutdown():
    # A close before start() ever ran must still release the microphone.
    rt, asst, vc, bridge, widget = _runtime()
    rt.shutdown("closed before start")
    assert vc.shutdown_calls == ["closed before start"]


# --- KillSwitch must never trigger shutdown ---------------------------

def test_killswitch_engagement_does_not_trigger_shutdown():
    quits = []
    rt, asst, vc, bridge, widget = _runtime(quit_fn=lambda: quits.append(1))
    rt.start()
    asst.kill_switch.engage(reason="test stop")
    rt.poll_killswitch()
    assert vc.shutdown_calls == []
    assert quits == []
    # The widget IS notified (for a future visual layer), but that is the
    # only effect - never shutdown, never engage/reset/approve/deny.
    assert widget.killswitch_ticks == [True]


def test_killswitch_watch_tick_reflects_disengaged_state_too():
    rt, asst, vc, bridge, widget = _runtime()
    rt.poll_killswitch()
    assert widget.killswitch_ticks == [False]


def test_killswitch_watch_never_calls_engage_reset_approve_deny_on_assistant():
    # Structural guarantee: VoidRuntime never becomes a second authorization
    # path. Confirmed by construction (poll_killswitch reads .engaged only),
    # reconfirmed here by watching for any call to authority-shaped methods.
    class WatchedAssistant(FakeAssistant):
        def __init__(self):
            super().__init__()
            self.calls = []

        def approve(self, *a, **k):
            self.calls.append("approve")

        def deny(self, *a, **k):
            self.calls.append("deny")

        def clear_stop(self):
            self.calls.append("clear_stop")

    asst = WatchedAssistant()
    rt, *_ = _runtime(assistant=asst)
    rt.start()
    asst.kill_switch.engage(reason="test")
    rt.poll_killswitch()
    rt.shutdown()
    assert asst.calls == []


def test_repeated_engage_disengage_never_triggers_shutdown():
    quits = []
    rt, asst, vc, bridge, widget = _runtime(quit_fn=lambda: quits.append(1))
    rt.start()
    for _ in range(3):
        asst.kill_switch.engage(reason="cycle")
        rt.poll_killswitch()
        asst.kill_switch.reset()
        rt.poll_killswitch()
    assert vc.shutdown_calls == [] and quits == []
    assert widget.killswitch_ticks == [True, False, True, False, True, False]


# --- build_runtime(): exactly one Assistant / VoiceController --------------
#
# Every factory is overridden with a fake, so build_runtime here never
# imports PySide6/QApplication/VoidWidget at all - fully display-independent.

def _fake_build_kwargs():
    counts = {"assistant": 0, "bridge": 0, "voice_controller": 0,
              "widget": 0, "qapp": 0}
    made = {}

    def assistant_factory():
        counts["assistant"] += 1
        made["assistant"] = FakeAssistant()
        return made["assistant"]

    def bridge_factory():
        counts["bridge"] += 1
        made["bridge"] = object()
        return made["bridge"]

    def voice_controller_factory(assistant, bridge):
        counts["voice_controller"] += 1
        assert assistant is made["assistant"]      # same instance shared
        assert bridge is made["bridge"]
        made["voice_controller"] = FakeVoiceController()
        return made["voice_controller"]

    def widget_factory(assistant, bridge):
        counts["widget"] += 1
        assert assistant is made["assistant"]
        assert bridge is made["bridge"]
        made["widget"] = FakeWidget()
        return made["widget"]

    def qapplication_factory():
        counts["qapp"] += 1
        made["app"] = FakeQApplication()
        return made["app"]

    kwargs = dict(assistant_factory=assistant_factory,
                  bridge_factory=bridge_factory,
                  voice_controller_factory=voice_controller_factory,
                  widget_factory=widget_factory,
                  qapplication_factory=qapplication_factory)
    return kwargs, counts, made


def test_build_runtime_composes_exactly_one_of_each():
    kwargs, counts, made = _fake_build_kwargs()
    runtime, app = build_runtime(**kwargs)
    assert counts == {"assistant": 1, "bridge": 1, "voice_controller": 1,
                      "widget": 1, "qapp": 1}
    assert runtime.assistant is made["assistant"]
    assert runtime.voice_controller is made["voice_controller"]
    assert runtime.bridge is made["bridge"]
    assert runtime.widget is made["widget"]
    assert app is made["app"]


def test_build_runtime_wires_widget_closing_to_shutdown():
    kwargs, counts, made = _fake_build_kwargs()
    runtime, app = build_runtime(**kwargs)
    widget = made["widget"]
    voice_controller = made["voice_controller"]
    widget.closing.fire()
    assert voice_controller.shutdown_calls == ["ui closed"]
    assert app.quit_calls == 1
