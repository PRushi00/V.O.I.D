"""Tests for void.ui.tray_indicator: the minimal, notification-area-only
listening indicator for the normal (voice-only) runtime.

Deliberately does NOT construct a real QApplication/QSystemTrayIcon here -
create() refuses to under pytest by design (see the contamination lesson in
void.runtime.diagnostics: an unmocked real entry point popping up a REAL,
visible tray icon on every test run would be a desktop-visible regression of
exactly that kind). These tests cover the pure state->style mapping and the
test-safety guard; the actual Qt wiring is validated by running it live."""
from __future__ import annotations

from void.ui import tray_indicator
from void.voice.state import VoiceState


def test_every_real_voicestate_value_has_an_explicit_style():
    # Every state VoiceController can actually emit must map to something
    # deliberate, not silently fall through to the generic default.
    real_states = [
        VoiceState.IDLE, VoiceState.LISTENING, VoiceState.CAPTURED,
        VoiceState.TRANSCRIBING, VoiceState.DISPATCHED, VoiceState.SPEAKING,
        VoiceState.ERROR, VoiceState.STOPPED, VoiceState.CLOSED,
    ]
    for state in real_states:
        color, tooltip = tray_indicator.style_for_state(state)
        assert color != tray_indicator._DEFAULT_STYLE[0], state
        assert "V.O.I.D" in tooltip


def test_idle_and_listening_are_visually_distinct():
    # IDLE = armed/waiting for the wake phrase; LISTENING = actively
    # capturing a command. Conflating them would misrepresent whether the
    # microphone is capturing a command right now.
    idle_color, idle_tip = tray_indicator.style_for_state(VoiceState.IDLE)
    cap_color, cap_tip = tray_indicator.style_for_state(VoiceState.LISTENING)
    assert idle_color != cap_color
    assert idle_tip != cap_tip


def test_error_and_stopped_are_distinguishable_from_normal_operation():
    normal_colors = {tray_indicator.style_for_state(s)[0] for s in
                     (VoiceState.IDLE, VoiceState.LISTENING, VoiceState.SPEAKING)}
    assert tray_indicator.style_for_state(VoiceState.ERROR)[0] not in normal_colors
    assert tray_indicator.style_for_state(VoiceState.STOPPED)[0] not in normal_colors


def test_unknown_state_falls_back_to_default_without_raising():
    color, tooltip = tray_indicator.style_for_state("some_future_state")
    assert color == tray_indicator._DEFAULT_STYLE[0]
    assert "V.O.I.D" in tooltip


def test_mic_health_states_are_distinct_from_every_normal_and_error_state():
    # These are NOT VoiceState values - they come from VoiceController's own
    # mic-health supervisor (void.voice.runtime), an orthogonal signal about
    # the physical microphone stream itself, not the session lifecycle. The
    # bug this closes ("tray says listening while the mic is actually dead")
    # requires these to be visually unmistakable from ordinary operation.
    other_colors = {tray_indicator.style_for_state(s)[0] for s in (
        VoiceState.IDLE, VoiceState.LISTENING, VoiceState.CAPTURED,
        VoiceState.TRANSCRIBING, VoiceState.DISPATCHED, VoiceState.SPEAKING,
        VoiceState.ERROR, VoiceState.STOPPED, VoiceState.CLOSED,
    )}
    unavailable_color, unavailable_tip = tray_indicator.style_for_state("mic_unavailable")
    recovering_color, recovering_tip = tray_indicator.style_for_state("mic_recovering")
    assert unavailable_color not in other_colors
    assert recovering_color not in other_colors
    assert unavailable_color != recovering_color
    assert "microphone" in unavailable_tip.lower()
    assert "microphone" in recovering_tip.lower()


def test_style_mapping_is_case_and_whitespace_tolerant():
    assert tray_indicator.style_for_state(" IDLE ") == tray_indicator.style_for_state("idle")


def test_create_never_constructs_a_real_tray_under_pytest():
    # The actual regression this guards: create() must not pop up a real,
    # visible system tray icon just because a test called cmd_voice() (or
    # this factory) without mocking it out - see void.runtime.diagnostics'
    # _running_under_pytest for the identical, already-fixed problem with
    # the production log.
    class _FakeKillSwitch:
        engaged = False

    class _FakeAssistant:
        kill_switch = _FakeKillSwitch()

        def stop(self, reason=None):
            raise AssertionError("must never be called by this test")

        def clear_stop(self):
            raise AssertionError("must never be called by this test")

    indicator = tray_indicator.create(_FakeAssistant(), bridge=None)
    assert indicator is None


def test_indicator_holds_a_persistent_reference_to_its_context_menu():
    # Regression for the actual root cause found by comparing V.O.I.D
    # against a working standalone PySide6 script (which kept every Qt
    # object as a module-level global, so nothing was ever collectible):
    # QMenu has no QWidget parent available in create() (QSystemTrayIcon is
    # not a QWidget, so it cannot own one). Without an explicit Python
    # reference kept for as long as the indicator itself, the menu built in
    # create() was only reachable via that function's local scope and
    # became collectible the instant create() returned - taking the tray's
    # Stop/Rearm/Exit actions, and very plausibly the icon's own shell
    # registration, down with it once garbage collection actually ran.
    import gc

    class _FakeApp:
        pass

    class _FakeTray:
        def setIcon(self, icon):
            pass

        def setToolTip(self, tooltip):
            pass

    class _FakeMenu:
        pass

    menu = _FakeMenu()
    menu_id = id(menu)
    indicator = tray_indicator.TrayIndicator(
        _FakeApp(), _FakeTray(), {}, bridge=None, menu=menu)
    del menu
    gc.collect()
    assert indicator._menu is not None
    assert id(indicator._menu) == menu_id   # still the SAME object, not collected


def test_indicator_never_imports_riskgate_task_or_secrets_modules():
    # Static guard (imports only, not prose - the module's own docstring
    # legitimately NAMES these to disclaim touching them): this module must
    # hold no reference to RiskGate, the task store, or credentials/secrets -
    # only the same two owner-facing controls the existing dev-UI tray uses
    # (assistant.stop / assistant.clear_stop).
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(tray_indicator))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif isinstance(node, ast.Import):
            imported.update(n.name for n in node.names)
    for banned in ("void.security.risk", "void.core.task",
                   "void.security.secrets", "void.security.credentials",
                   "void.core.kill_switch"):
        assert banned not in imported, imported
