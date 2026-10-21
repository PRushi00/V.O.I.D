"""The screen-watch organ: it must notice stillness and must not be able to see the screen.

This was declined in an earlier pass because its payoff is a model call about a picture of whatever
the owner has open. It is built now with that objection answered in the design, so these tests are
mostly about the objection rather than about the feature:

* nothing here calls a model, or can;
* nothing is kept between ticks but 576 block means;
* the published state is a closed set of numbers;
* the whole thing is off unless BOTH switches are on.

Every test runs on synthetic arrays, so none of it needs a screen.
"""
from __future__ import annotations

from dataclasses import fields

import pytest

numpy = pytest.importorskip("numpy", reason="the watch reduces images with numpy")

from void.perception.stillness import (BLANK_CONTRAST, GRID, NUDGE_EVERY_S, STATE_KEYS,
                                       STILL_FOR_S, STILL_THRESHOLD, StillnessWatch, WatchPolicy,
                                       WatchState, difference, from_config, nudge_line, thumbnail)
from void.config import Config

CANARY = "zygalquintaped.example"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Screen:
    """A fake screen. ``array`` is whatever the test wants the next capture to be."""

    def __init__(self, array):
        self.array = array
        self.captures = 0

    def __call__(self):
        self.captures += 1
        return self


def flat(value: int = 128, size=(180, 320)):
    return numpy.full(size, value, dtype=numpy.uint8)


def noisy(seed: int = 1, size=(180, 320)):
    rng = numpy.random.default_rng(seed)
    return rng.integers(0, 255, size=size, dtype=numpy.uint8)


def watch(screen, clock, **over) -> StillnessWatch:
    policy = WatchPolicy(enabled=True, **over)
    return StillnessWatch(policy, capture=screen, now=clock)


# --- the thumbnail --------------------------------------------------------------------------------

def test_a_thumbnail_is_the_documented_grid():
    thumb = thumbnail(noisy())
    assert thumb.shape == (GRID[1], GRID[0])


def test_a_thumbnail_is_block_means_not_sampled_pixels():
    """A sampled grid misses a change between its sample points. Here one cell's worth of the image
    is altered and the corresponding cell must move - which only happens if it is an average."""
    base = flat(0)
    changed = base.copy()
    changed[0:10, 0:10] = 255
    before, after = thumbnail(base), thumbnail(changed)
    assert after[0][0] > before[0][0]


def test_a_colour_image_is_reduced_to_one_channel():
    assert thumbnail(numpy.zeros((180, 320, 3), dtype=numpy.uint8)).shape == (GRID[1], GRID[0])


def test_a_screen_whose_size_is_not_a_multiple_of_the_grid_still_reduces():
    """Which is every real screen."""
    for size in ((1080, 1921), (37, 41), (19, 33), (1, 1)):
        thumb = thumbnail(numpy.zeros(size, dtype=numpy.uint8))
        assert thumb.ndim == 2 and thumb.size > 0


def test_the_thumbnail_destroys_what_was_on_screen():
    """The property the whole privacy argument rests on: 576 cells cannot hold a line of text.

    Two images with the same block means but wildly different content reduce identically, which is
    the formal way of saying the thumbnail is not a picture of anything.
    """
    left = numpy.zeros((180, 320), dtype=numpy.uint8)
    right = numpy.zeros((180, 320), dtype=numpy.uint8)
    # Same mean per block, opposite arrangement inside it.
    left[0::2, :] = 200
    right[1::2, :] = 200
    assert numpy.allclose(thumbnail(left), thumbnail(right), atol=1.0)


def test_a_thing_that_is_not_an_image_is_rejected():
    for bad in (numpy.array([]), numpy.zeros((2, 2, 2, 2))):
        with pytest.raises(Exception):
            thumbnail(bad)


# --- the difference -------------------------------------------------------------------------------

def test_an_identical_screen_differs_by_nothing():
    thumb = thumbnail(noisy())
    assert difference(thumb, thumb) == 0.0


def test_a_changed_screen_differs_by_more_than_the_threshold():
    assert difference(thumbnail(flat(0)), thumbnail(flat(255))) > STILL_THRESHOLD


def test_a_missing_side_is_no_difference_rather_than_a_crash():
    assert difference(None, thumbnail(flat())) == 0.0
    assert difference(thumbnail(flat()), None) == 0.0


def test_an_ordinary_resolution_change_is_compared_on_its_merits():
    """The grid NORMALISES shape, which is what makes this work at all: a window captured at one
    size and then another still reduces to 32x18, so the two are comparable and the change shows up
    as content rather than as an incomparable shape. An earlier revision of this test asserted a
    shape mismatch here and was simply wrong about the design.
    """
    same = difference(thumbnail(flat(100, size=(180, 320))), thumbnail(flat(100, size=(90, 160))))
    assert same == pytest.approx(0.0), "the same picture at two sizes should compare equal"
    moved = difference(thumbnail(flat(0, size=(180, 320))), thumbnail(flat(255, size=(90, 160))))
    assert moved > STILL_THRESHOLD


def test_shapes_that_cannot_be_compared_are_reported_as_changed_rather_than_as_stillness():
    """A screen smaller than the grid reduces to a smaller grid. Calling that "unchanged" would
    start the stillness clock on a comparison that never happened."""
    tiny = thumbnail(numpy.zeros((4, 4), dtype=numpy.uint8))
    full = thumbnail(flat())
    assert tiny.shape != full.shape
    assert difference(full, tiny) == float("inf")


# --- stillness ------------------------------------------------------------------------------------

def test_the_first_tick_only_establishes_a_baseline():
    """Treating it as "unchanged" would start the clock from a comparison that never happened."""
    clock = Clock()
    w = watch(Screen(noisy()), clock)
    state = w.tick()
    assert state.ticks == 1 and state.still_s == 0.0 and state.changes == 0 and state.change == 0.0


def test_an_unchanged_screen_accumulates_stillness():
    clock = Clock()
    screen = Screen(flat())
    w = watch(screen, clock)
    w.tick()
    clock.advance(60)
    assert w.tick().still_s == pytest.approx(60.0)
    clock.advance(60)
    assert w.tick().still_s == pytest.approx(120.0)


def test_a_change_resets_the_clock_and_is_counted():
    clock = Clock()
    screen = Screen(flat(0))
    w = watch(screen, clock)
    w.tick()
    clock.advance(300)
    screen.array = flat(255)
    state = w.tick()
    assert state.still_s == 0.0 and state.changes == 1


def test_a_change_under_the_threshold_is_not_a_change():
    """A blinking cursor and video compression noise must not read as the owner working."""
    clock = Clock()
    base = flat(100)
    screen = Screen(base)
    w = watch(screen, clock)
    w.tick()
    nudged = base.copy()
    nudged[0, 0] = 101            # one pixel, one level
    screen.array = nudged
    clock.advance(60)
    state = w.tick()
    assert state.changes == 0 and state.still_s == pytest.approx(60.0)


def test_reset_establishes_a_fresh_baseline_without_losing_the_counters():
    clock = Clock()
    screen = Screen(flat())
    w = watch(screen, clock)
    w.tick()
    clock.advance(120)
    w.tick()
    before = w.state()
    w.reset()
    clock.advance(10)
    after = w.tick()
    assert after.still_s == 0.0 and after.ticks == before.ticks + 1


# --- the nudge ------------------------------------------------------------------------------------

def test_no_nudge_before_the_screen_has_been_still_long_enough():
    clock = Clock()
    said = []
    # A BUSY but unchanging screen. A flat one would be blank, which never nudges by design, so a
    # flat screen here would make every nudge test pass for the wrong reason.
    w = StillnessWatch(WatchPolicy(enabled=True), capture=Screen(noisy()), now=clock,
                       on_nudge=said.append)
    w.tick()
    clock.advance(STILL_FOR_S - 1)
    assert w.tick().nudges == 0 and said == []


def test_a_nudge_once_the_screen_has_been_still_too_long():
    clock = Clock()
    said = []
    w = StillnessWatch(WatchPolicy(enabled=True), capture=Screen(noisy()), now=clock,
                       on_nudge=said.append)
    w.tick()
    clock.advance(STILL_FOR_S + 1)
    assert w.tick().nudges == 1 and len(said) == 1


def test_a_nudge_is_never_repeated_faster_than_the_cadence():
    """A nudge every ten minutes at a screen the owner walked away from is an alarm."""
    clock = Clock()
    said = []
    w = StillnessWatch(WatchPolicy(enabled=True), capture=Screen(noisy()), now=clock,
                       on_nudge=said.append)
    w.tick()
    clock.advance(STILL_FOR_S + 1)
    w.tick()
    for _ in range(20):
        clock.advance(60)
        w.tick()
    assert len(said) == 1, said
    clock.advance(NUDGE_EVERY_S)
    w.tick()
    assert len(said) == 2


def test_a_blank_screen_never_nudges():
    """Asleep, locked and blanked are all still, and none of them is the owner staring at a window.
    Nudging someone about their own screensaver is the noise that gets a feature switched off."""
    clock = Clock()
    said = []
    w = StillnessWatch(WatchPolicy(enabled=True), capture=Screen(flat(0)), now=clock,
                       on_nudge=said.append)
    w.tick()
    clock.advance(STILL_FOR_S * 5)
    state = w.tick()
    assert state.blank is True and state.nudges == 0 and said == []


def test_blankness_is_measured_by_contrast_not_by_colour():
    """A flat white lock screen is as blank as a black one."""
    clock = Clock()
    for value in (0, 128, 255):
        w = watch(Screen(flat(value)), clock)
        w.tick()
        assert w.tick().blank is True
    busy = watch(Screen(noisy()), Clock())
    busy.tick()
    assert busy.tick().blank is False
    assert BLANK_CONTRAST > 0


def test_the_nudge_sentence_names_a_duration_and_nothing_else():
    line = nudge_line(732.0)
    assert "12 minutes" in line
    for word in ("http", "window", "chrome", "title", "pixel", ".com"):
        assert word not in line.lower()


def test_a_listener_that_raises_cannot_break_the_watch():
    clock = Clock()

    def explode(_line):
        raise RuntimeError("the listener fell over")

    w = StillnessWatch(WatchPolicy(enabled=True), capture=Screen(noisy()), now=clock,
                       on_nudge=explode)
    w.tick()
    clock.advance(STILL_FOR_S + 1)
    assert w.tick().nudges == 1          # counted, and the exception never reached here


# --- it is off unless asked for -------------------------------------------------------------------

def test_a_disabled_watch_takes_no_captures_at_all():
    screen = Screen(noisy())
    w = StillnessWatch(WatchPolicy(enabled=False), capture=screen, now=Clock())
    for _ in range(5):
        assert w.tick().watching is False
    assert screen.captures == 0, "a switched-off watch captured the screen"


def test_the_default_policy_is_off():
    assert WatchPolicy().enabled is False


def test_the_shipped_configuration_leaves_it_off():
    assert WatchPolicy.from_config(Config.load()).enabled is False


def test_both_switches_are_required():
    """screen.enabled is "may you capture at all"; watch_enabled is "may you do it on a loop". One
    mis-set key must not be able to start a continuous watch."""
    assert WatchPolicy.from_config(Config({"screen": {"enabled": True}})).enabled is False
    assert WatchPolicy.from_config(Config({"screen": {"watch_enabled": True}})).enabled is False
    both = Config({"screen": {"enabled": True, "watch_enabled": True}})
    assert WatchPolicy.from_config(both).enabled is True


def test_from_config_returns_nothing_to_drive_when_it_is_off():
    """None rather than a disabled object, so a caller cannot accidentally drive a watch the owner
    never asked for."""
    assert from_config(Config.load()) is None
    assert from_config(Config({"screen": {"enabled": True, "watch_enabled": True}}),
                       capture=Screen(flat())) is not None


def test_nonsense_timings_fall_back_to_the_defaults_rather_than_nudging_constantly():
    cfg = Config({"screen": {"enabled": True, "watch_enabled": True,
                             "watch_still_for_s": 0, "watch_nudge_every_s": -5,
                             "watch_threshold": "soon"}})
    policy = WatchPolicy.from_config(cfg)
    assert policy.still_for_s == STILL_FOR_S
    assert policy.nudge_every_s == NUDGE_EVERY_S
    assert policy.threshold == STILL_THRESHOLD


# --- what it cannot do ----------------------------------------------------------------------------

def test_the_state_cannot_carry_an_identity():
    """The structural guarantee. Every field is a number or a boolean, so there is nowhere to put a
    window title - and this fails loudly if a field that could hold one is ever added."""
    allowed = {"bool", "float", "int"}
    for field in fields(WatchState):
        assert field.type in allowed, f"WatchState.{field.name} is a {field.type}; the watch can now leak"
    assert set(STATE_KEYS) == {f.name for f in fields(WatchState)}


def test_the_state_renders_without_anything_recognisable():
    clock = Clock()
    w = watch(Screen(noisy()), clock)
    w.tick()
    clock.advance(30)
    rendered = repr(w.tick())
    assert CANARY not in rendered
    for word in ("http", "://", ".com", "window", "title", "array"):
        assert word not in rendered.lower()


def _identifiers() -> tuple[set[str], set[str]]:
    """Every name, attribute and imported module the watch module actually USES.

    By syntax tree rather than by searching the text, for the reason the teardown kit's own
    structural test gives: this file is full of prose about providers and files precisely because it
    explains why it has none, and a grep cannot tell an explanation from a call. An earlier revision
    of these two tests failed on their own docstrings.
    """
    import ast
    import inspect

    from void.perception import stillness

    tree = ast.parse(inspect.getsource(stillness))
    used: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
            imported.update(alias.name for alias in node.names)
    return used, imported


def test_the_module_cannot_call_a_model():
    """The declined objection, answered structurally: this module decides WHEN, never WHAT.

    Acting on a nudge - taking a real capture, sending it anywhere - stays behind
    ScreenPolicy.allow_cloud_analysis, the risk gate and the existing vision path. There is no
    provider here to call and no encoder to hand a picture to.
    """
    used, imported = _identifiers()
    for forbidden in ("generate", "complete", "encode_jpeg", "b64encode", "urlopen", "post"):
        assert forbidden not in used, f"the watch USES {forbidden!r}"
    for forbidden in ("requests", "httpx", "urllib", "base64", "socket", "http"):
        assert forbidden not in imported, f"the watch IMPORTS {forbidden!r}"
    assert not any("provider" in name.lower() for name in used | imported)


def test_the_module_never_writes_anything():
    """Nothing is kept: no image, no file, no path. 576 floats between ticks, and that is all."""
    used, imported = _identifiers()
    for forbidden in ("open", "write_text", "write_bytes", "save", "mkdir", "unlink",
                      "NamedTemporaryFile", "dump", "dumps"):
        assert forbidden not in used, f"the watch USES {forbidden!r}"
    for forbidden in ("pathlib", "tempfile", "shutil", "pickle", "sqlite3", "csv"):
        assert forbidden not in imported, f"the watch IMPORTS {forbidden!r}"


def test_nothing_but_the_previous_thumbnail_is_kept_between_ticks():
    """576 floats, and no second copy of anything. A watch that accumulated frames would be a
    recording."""
    clock = Clock()
    w = watch(Screen(noisy()), clock)
    for _ in range(8):
        clock.advance(10)
        w.tick()
    held = [v for k, v in vars(w).items() if k.startswith("_previous")]
    assert len(held) == 1
    assert held[0].shape == (GRID[1], GRID[0])


# --- it survives a bad frame ----------------------------------------------------------------------

def test_a_capture_that_fails_is_reported_as_unavailable_and_not_as_stillness():
    """A screen that cannot be read is not a still screen, and must never accumulate toward a
    nudge: that would nudge the owner because V.O.I.D went blind."""
    clock = Clock()

    def explode():
        raise RuntimeError("no display")

    w = StillnessWatch(WatchPolicy(enabled=True), capture=explode, now=clock)
    state = w.tick()
    assert state.unavailable is True and state.still_s == 0.0 and state.nudges == 0


def test_a_missing_capture_source_is_survived():
    w = StillnessWatch(WatchPolicy(enabled=True), capture=None, now=Clock())
    assert w.tick().unavailable is True


def test_an_unreadable_frame_does_not_lose_the_counters():
    clock = Clock()
    screen = Screen(flat())
    w = watch(screen, clock)
    w.tick()
    clock.advance(60)
    w.tick()
    before = w.state()

    w._capture = lambda: (_ for _ in ()).throw(RuntimeError("one bad frame"))
    broken = w.tick()
    assert broken.unavailable is True and broken.ticks == before.ticks + 1
    assert broken.changes == before.changes and broken.nudges == before.nudges
