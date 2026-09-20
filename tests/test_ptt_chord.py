"""D-02 (T0.4): ``ctrl+space`` must mean Ctrl+Space; bare Space must not activate voice.

The handlers are driven directly with injected key-state predicates (the adapter's
existing testing seam) - no real global hook, no ``keyboard`` import."""
import sys
import types

from void.voice.adapters import PTTActivation


def _ptt(held, hotkey="ctrl+space", **kw):
    """``held`` is a mutable set of key names currently 'down'."""
    presses, releases = [], []
    ptt = PTTActivation(
        lambda: presses.append(1), lambda: releases.append(1), hotkey=hotkey,
        key_is_down=lambda: "space" in held or "f9" in held,
        modifier_is_down=lambda name: name in held, **kw)
    return ptt, presses, releases


def test_bare_space_is_ignored():
    held = {"space"}
    ptt, presses, releases = _ptt(held)
    ptt._handle_key_down()
    assert presses == []
    assert ptt._held is False


def test_ctrl_space_starts_exactly_one_session():
    held = {"ctrl", "space"}
    ptt, presses, releases = _ptt(held)
    ptt._handle_key_down()
    assert presses == [1]


def test_release_ends_the_session_when_the_main_key_goes_up():
    held = {"ctrl", "space"}
    ptt, presses, releases = _ptt(held)
    ptt._handle_key_down()
    held.discard("space")
    ptt._handle_key_up()
    assert (presses, releases) == ([1], [1])


def test_ctrl_released_before_space_still_ends_cleanly_and_repeats_do_not_restart():
    held = {"ctrl", "space"}
    ptt, presses, releases = _ptt(held)
    ptt._handle_key_down()                 # chord pressed
    held.discard("ctrl")                   # Ctrl let go first, Space still held
    ptt._handle_key_down()                 # OS auto-repeat of Space
    ptt._handle_key_down()
    assert presses == [1], "auto-repeat after Ctrl release must not start a new session"
    assert releases == []                  # capture continues until the real release
    held.discard("space")
    ptt._handle_key_up()
    assert releases == [1]


def test_auto_repeat_with_ctrl_held_is_still_one_activation():
    held = {"ctrl", "space"}
    ptt, presses, releases = _ptt(held)
    for _ in range(5):
        ptt._handle_key_down()
    assert presses == [1]


def test_space_pressed_before_ctrl_activates_once_the_chord_is_complete():
    held = {"space"}
    ptt, presses, releases = _ptt(held)
    ptt._handle_key_down()                 # Ctrl not yet down -> ignored
    assert presses == []
    held.add("ctrl")
    ptt._handle_key_down()                 # next auto-repeat, chord now complete
    assert presses == [1]


def test_modifier_free_hotkey_is_unaffected():
    held = {"f9"}
    ptt, presses, releases = _ptt(held, hotkey="f9")
    ptt._handle_key_down()
    assert presses == [1]


def test_every_modifier_of_a_multi_modifier_chord_is_required():
    held = {"ctrl", "space"}
    ptt, presses, _ = _ptt(held, hotkey="ctrl+shift+space")
    ptt._handle_key_down()
    assert presses == []                   # Shift missing
    held.add("shift")
    ptt._handle_key_down()
    assert presses == [1]


def test_modifier_aliases_are_normalised():
    held = {"windows", "space"}
    ptt, presses, _ = _ptt(held, hotkey="win+space")
    ptt._handle_key_down()
    assert presses == [1]


def test_unreadable_modifier_state_fails_closed():
    presses = []
    def boom(name):
        raise RuntimeError("cannot read key state")
    ptt = PTTActivation(lambda: presses.append(1), lambda: None, hotkey="ctrl+space",
                        modifier_is_down=boom)
    ptt._handle_key_down()
    assert presses == []


def test_strict_chord_false_restores_v1_behaviour():
    held = {"space"}
    ptt, presses, _ = _ptt(held, strict_chord=False)
    ptt._handle_key_down()
    assert presses == [1]                  # rollback switch: last key only, like V1


def test_handlers_driven_directly_without_a_predicate_are_not_enforced():
    # Existing tests (test_voice.py) call the handlers on an un-started adapter.
    presses = []
    ptt = PTTActivation(lambda: presses.append(1), lambda: None)     # default ctrl+space
    ptt._handle_key_down()
    assert presses == [1]


def test_start_binds_the_real_key_state_for_modifiers(monkeypatch):
    held = set()
    kb = types.SimpleNamespace(callbacks={})
    kb.on_press_key = lambda key, cb: kb.callbacks.__setitem__(("down", key), cb)
    kb.on_release_key = lambda key, cb: kb.callbacks.__setitem__(("up", key), cb)
    kb.is_pressed = lambda name: name in held
    kb.unhook_all = lambda: None
    monkeypatch.setitem(sys.modules, "keyboard", kb)
    presses = []
    ptt = PTTActivation(lambda: presses.append(1), lambda: None, hotkey="ctrl+space")
    ptt.start()
    kb.callbacks[("down", "space")](None)
    assert presses == []                   # bare Space through the real wiring
    held.update({"ctrl", "space"})
    kb.callbacks[("down", "space")](None)
    assert presses == [1]
    ptt.stop()


def test_controller_reads_the_strict_chord_switch_from_config():
    from void.config import Config
    from void.voice.runtime import VoiceController
    cfg = Config({"voice": {"ptt_hotkey": "ctrl+space", "ptt_strict_chord": False,
                            "wake_provider": "null"}, "app": {}})
    fake_assistant = types.SimpleNamespace(config=cfg, kill_switch=types.SimpleNamespace(engaged=False))
    ctrl = VoiceController.from_assistant(fake_assistant, cfg)
    assert ctrl._activation._strict_chord is False
