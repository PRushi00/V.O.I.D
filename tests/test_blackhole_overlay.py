"""Structural safety tests for the Blackhole overlay window (void.ui.widget).

These parse the SOURCE with ast (no PySide6 import / no display needed), so
they run in headless CI and pin down the milestone's ownership guarantees:
the overlay is a pure observer - it never constructs a second Assistant,
never opens a microphone / second audio owner, and never calls RiskGate /
KillSwitch authorization. It DOES call the existing public stop()/clear_stop()
controls (the same ones the CLI exposes), which is allowed.
"""
from __future__ import annotations

import ast
from pathlib import Path

_WIDGET_SRC = Path(__file__).resolve().parent.parent / "void" / "ui" / "widget.py"


def _tree():
    return ast.parse(_WIDGET_SRC.read_text(encoding="utf-8"))


def _class_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _calls_in(node):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _call_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def test_voidwidget_does_not_construct_a_second_assistant():
    # Only the standalone launch() entry point may build the single Assistant;
    # the overlay class itself must never construct one (it receives it).
    tree = _tree()
    cls = _class_node(tree, "VoidWidget")
    assert "Assistant" not in {_call_name(c) for c in _calls_in(cls)}


def test_widget_module_creates_no_second_audio_owner():
    src = _WIDGET_SRC.read_text(encoding="utf-8")
    for banned in ("AudioCaptureBroker", "create_audio_broker",
                   "sounddevice", "InputStream", "feed_audio(", "VoiceController("):
        assert banned not in src, f"overlay must not reference {banned}"


def test_widget_module_makes_no_riskgate_or_killswitch_authorization_calls():
    tree = _tree()
    banned = {"engage", "reset", "approve", "deny", "raise_if_engaged", "run"}
    called = {_call_name(c) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    assert not (called & banned), called & banned


def test_overlay_observes_the_presenter():
    # The overlay must delegate visual state to the pure presenter, not
    # reinvent a state machine.
    src = _WIDGET_SRC.read_text(encoding="utf-8")
    assert "BlackholePresenter" in src
    assert "observe_voice_state" in src
    assert "observe_killswitch" in src


def test_overlay_is_click_through_and_fullscreen():
    src = _WIDGET_SRC.read_text(encoding="utf-8")
    assert "WA_TransparentForMouseEvents" in src   # never obstructs the desktop
    assert "primaryScreen" in src                  # centered on the primary display
    assert "WA_TranslucentBackground" in src       # wallpaper visible underneath


def test_overlay_paints_nothing_when_not_visible():
    # paintEvent must early-return when the presenter says hidden, so a
    # mapped-but-idle overlay shows nothing (desktop stays normal).
    tree = _tree()
    cls = _class_node(tree, "VoidWidget")
    paint = next((n for n in cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "paintEvent"), None)
    assert paint is not None
    assert any(isinstance(n, ast.Return) for n in ast.walk(paint))
