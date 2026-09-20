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


def test_an_hour_of_typing_starts_no_sessions_and_every_chord_starts_exactly_one():
    """Seeded simulation of ~1 h of typing at ~5 keystrokes/s (18 000 events): words with
    spaces, held spaces with OS auto-repeat, ctrl-shortcuts. V1 started a session on every one
    of those spaces (723 of 809 STT starts in the real log were 0-sample). Now only real
    Ctrl+Space chords count."""
    import random

    rng = random.Random(20260921)
    held = set()
    ptt, presses, releases = _ptt(held)
    chords = 0
    for _ in range(18_000):
        roll = rng.random()
        if roll < 0.70:                                   # ordinary letter: not the hotkey, never reaches it
            continue
        if roll < 0.93:                                   # a word-separating space, maybe held with auto-repeat
            held.add("space")
            for _rep in range(rng.choice([1, 1, 1, 3, 8])):
                ptt._handle_key_down()
            held.discard("space")
            ptt._handle_key_up()
        elif roll < 0.98:                                 # other Ctrl shortcuts (ctrl+c etc.): no space involved
            held.add("ctrl")
            held.discard("ctrl")
        else:                                             # a deliberate Ctrl+Space press-and-release
            held.update({"ctrl", "space"})
            for _rep in range(rng.choice([1, 1, 4])):
                ptt._handle_key_down()
            held.discard("space")
            ptt._handle_key_up()
            held.discard("ctrl")
            chords += 1
    assert chords > 100                                   # the simulation really did exercise chords
    assert len(presses) == chords, f"{len(presses)} sessions from {chords} deliberate chords"
    assert len(releases) == chords

