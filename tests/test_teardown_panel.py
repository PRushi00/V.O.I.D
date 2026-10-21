"""The teardown panel and the reader - the two halves of REA's eleventh prompt.

Both are built here rather than declined, because the part that could be wrong in a way that matters
turned out to be decidable without a screen: the step ordering, the statuses, and which note counts
as "the last teardown" are all pure functions over data the engine already publishes. What is left
for the Qt view is setText and setStyleSheet, and it returns None rather than raising where there is
no display.

The property defended hardest is that neither half can act: a panel that could start or stop a
capture, or a reader that could write, would be a second control path over something that spends a
real browser and a model call.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from void.teardown import NOTE_DIR
from void.teardown.panel import (DONE, FAILED, LABELS, NONE_YET, NO_VAULT, PENDING, RUNNING,
                                 SKIPPED, STATUSES, Panel, Progress, Row, Step,
                                 TeardownPanelView, asked_for_the_last, emitter, last_teardown,
                                 panel_for)
from void.vault import Vault

ROOT = Path(__file__).resolve().parent.parent


# --- the step vocabulary --------------------------------------------------------------------------

def test_there_are_exactly_the_eight_steps_the_pack_describes():
    assert len(Step.ORDER) == 8
    assert set(Step.ORDER) == Step.ALL


def test_every_step_has_a_label():
    """A view must never have to invent wording for a step, or fall back to a bare identifier."""
    assert set(LABELS) == Step.ALL
    assert all(LABELS[s] and not LABELS[s].islower() for s in Step.ORDER)


def test_the_order_is_the_captures_own():
    """The sequence is the pack's and it is not arbitrary: REA has to be WATCHING while V.O.I.D
    scrolls, so observation must follow attach and precede the bundle read. A panel that implied a
    different order would misdescribe how the evidence was obtained."""
    order = list(Step.ORDER)
    assert order.index(Step.OPEN) < order.index(Step.READ_PAGE) < order.index(Step.ATTACH)
    assert order.index(Step.ATTACH) < order.index(Step.OBSERVE) < order.index(Step.BUNDLE)
    assert order.index(Step.SNAPSHOT) < order.index(Step.ASSETS)
    assert order[-1] == Step.FINGERPRINT


# --- the panel ------------------------------------------------------------------------------------

def test_the_whole_shape_of_the_job_shows_from_the_first_event():
    """Someone watching a teardown wants to know what is still to come, which is most of the reason
    to show it at all - so every step has a row immediately, not one appearing at a time."""
    p = panel_for(Progress())
    assert len(p.rows) == 8
    assert all(r.status == PENDING for r in p.rows)
    assert p.done == 0 and p.finished is False


def test_a_marked_step_is_reflected_with_its_detail():
    pr = Progress().mark(Step.OPEN, DONE, "1.4s to load")
    row = next(r for r in panel_for(pr).rows if r.step == Step.OPEN)
    assert row.status == DONE and row.detail == "1.4s to load"


def test_the_running_step_is_named_once():
    pr = Progress().mark(Step.OPEN, DONE).mark(Step.READ_PAGE, RUNNING)
    p = panel_for(pr)
    assert p.running == Step.READ_PAGE and p.done == 1


def test_only_the_first_running_step_is_reported_as_the_running_one():
    """Two at once should not happen, and if it ever did the panel must still name one thing rather
    than flicker between them."""
    pr = Progress().mark(Step.BUNDLE, RUNNING).mark(Step.ASSETS, RUNNING)
    assert panel_for(pr).running == Step.BUNDLE


def test_a_skipped_step_is_not_a_failed_one():
    """A teardown whose snapshot the kit declined is still a teardown. Painting both the same would
    make a partial result look like a broken one."""
    pr = Progress().mark(Step.SNAPSHOT, SKIPPED, "no image returned")
    p = panel_for(pr)
    assert p.skipped == 1 and p.failed == 0


def test_finished_means_every_step_reached_a_terminal_status():
    pr = Progress()
    for step in Step.ORDER[:-1]:
        pr.mark(step, DONE)
    assert panel_for(pr).finished is False
    pr.mark(Step.FINGERPRINT, SKIPPED)
    assert panel_for(pr).finished is True


def test_finished_is_false_while_a_step_is_still_running():
    """The panel may not claim completion with work outstanding."""
    pr = Progress()
    for step in Step.ORDER[:-1]:
        pr.mark(step, DONE)
    pr.mark(Step.FINGERPRINT, RUNNING)
    assert panel_for(pr).finished is False


def test_a_failure_still_counts_toward_being_finished():
    """A run that broke is over. A panel that stayed at 7/8 forever would look like a hang."""
    pr = Progress()
    for step in Step.ORDER[:-1]:
        pr.mark(step, DONE)
    pr.mark(Step.FINGERPRINT, FAILED, "the kit exited 1")
    p = panel_for(pr)
    assert p.finished is True and p.failed == 1 and p.progress == 1.0


def test_progress_runs_from_nothing_to_whole_and_is_never_out_of_range():
    assert panel_for(Progress()).progress == 0.0
    pr = Progress()
    for step in Step.ORDER:
        pr.mark(step, DONE)
    assert panel_for(pr).progress == 1.0


def test_an_unknown_step_or_status_is_ignored_rather_than_shown():
    """The emitter is inside a long-running capture. A typo in a step name must not add a mystery
    row, and must certainly not raise."""
    pr = Progress().mark("teleport", DONE).mark(Step.OPEN, "sideways")
    p = panel_for(pr)
    assert len(p.rows) == 8
    assert all(r.step in Step.ALL for r in p.rows)
    assert next(r for r in p.rows if r.step == Step.OPEN).status == PENDING


def test_every_status_a_row_can_carry_is_in_the_closed_set():
    pr = Progress()
    for step, status in zip(Step.ORDER, (PENDING, RUNNING, DONE, FAILED, SKIPPED, DONE, DONE, DONE)):
        pr.mark(step, status)
    assert all(r.status in STATUSES for r in panel_for(pr).rows)


def test_the_panel_and_its_rows_are_readings_not_handles():
    """Frozen: a panel is one moment. A view that wants another asks for another."""
    pr = Progress().mark(Step.OPEN, DONE)
    p = panel_for(pr)
    with pytest.raises(Exception):
        p.rows[0].status = FAILED       # type: ignore[misc]
    with pytest.raises(Exception):
        p.done = 99                     # type: ignore[misc]
    assert isinstance(p, Panel) and isinstance(p.rows[0], Row)


# --- the capture hook -----------------------------------------------------------------------------

def test_capture_takes_an_on_step_callback():
    import inspect

    from void.teardown import Teardown

    assert "on_step" in inspect.signature(Teardown.capture).parameters


def test_the_callback_is_keyword_only_so_it_cannot_be_passed_by_accident():
    import inspect

    from void.teardown import Teardown

    param = inspect.signature(Teardown.capture).parameters["on_step"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY and param.default is None


def test_every_step_is_emitted_by_the_capture_source():
    """Checked against the source rather than by running a browser: a real capture needs Chrome,
    Node and REA. What this pins is that no step was given a row in the panel and then never
    reported - a step stuck at PENDING for the whole of every run.
    """
    import inspect

    from void.teardown import Teardown

    src = inspect.getsource(Teardown.capture)
    for step in Step.ORDER:
        assert f"_panel.Step.{step.upper()}" in src, f"{step} is never reported by capture()"


def test_the_emitter_passes_events_through():
    seen = []
    emitter(lambda *a: seen.append(a))("open", RUNNING, "1.4s")
    assert seen == [("open", RUNNING, "1.4s")]


def test_a_raising_callback_cannot_break_a_capture():
    """A progress display must never lose a run that has already spent a browser, seconds of
    observation and a model call - the panel is the least important thing in that list.

    The guard lives in ``emitter`` rather than inside capture's body precisely so this is a real
    unit test and not an assertion about source text.
    """
    def explode(*_a):
        raise RuntimeError("the panel fell over")

    emitter(explode)("open", RUNNING, "")        # must not raise
    emitter(explode)("fingerprint", FAILED, "x")


def test_no_callback_is_a_no_op_so_a_caller_needs_no_branch():
    emitter(None)("open", RUNNING, "")           # must not raise, and must do nothing


def test_capture_routes_its_progress_through_the_guarded_emitter():
    """Pins the wiring: an emission path that bypassed the guard would reintroduce exactly the
    failure mode the guard exists to prevent."""
    import inspect

    from void.teardown import Teardown

    src = inspect.getsource(Teardown.capture)
    assert "_panel.emitter(on_step)" in src


# --- the reader -----------------------------------------------------------------------------------

def _vault_with(tmp_path, notes: dict) -> Vault:
    root = tmp_path / "notes"
    for rel, body in notes.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    v = Vault(root)
    v.reload()
    return v


def test_no_vault_is_reported_as_no_vault():
    out = last_teardown(None)
    assert out.found is False and out.reason == NO_VAULT and out.excerpt == ""


def test_an_empty_vault_says_so_without_an_excerpt(tmp_path):
    """Absence is the answer, with nothing to improvise from - the same rule the notes vault
    follows for a question its notes do not cover."""
    out = last_teardown(_vault_with(tmp_path, {"Admin/Notes.md": "# Notes\nNothing.\n"}))
    assert out.found is False and out.reason == NONE_YET and out.excerpt == ""


def test_the_newest_teardown_note_is_the_one_returned(tmp_path):
    import os
    import time

    vault = _vault_with(tmp_path, {
        f"{NOTE_DIR}/teardowns stripe.com 2026-01-01.md": "# Stripe\nOne.\n",
        f"{NOTE_DIR}/teardowns figma.com 2026-02-02.md": "# Figma\nTwo.\n",
    })
    # Make the ordering unambiguous rather than relying on filesystem timestamp resolution.
    newer = next(n for n in vault.notes() if "figma" in n.path.lower())
    os.utime(newer.path, (time.time() + 50, time.time() + 50))
    vault.reload()
    out = last_teardown(vault)
    assert out.found is True and "figma" in out.path.lower()
    assert out.note_id is not None and out.excerpt


def test_a_note_that_is_not_a_teardown_is_never_returned(tmp_path):
    """A note that merely mentions a teardown is not one. Only where write_note files them, or a
    title it gave them, counts."""
    vault = _vault_with(tmp_path, {
        "Projects/Thoughts on teardowns.md": "# Thoughts on teardowns\nI should do more of these.\n",
        "Admin/Invoices.md": "# Invoices\nNothing to see.\n",
    })
    assert last_teardown(vault).found is False


def test_a_teardown_note_is_found_by_its_group(tmp_path):
    vault = _vault_with(tmp_path, {
        f"{NOTE_DIR}/anything at all.md": "# Anything\nBody.\n",
    })
    assert last_teardown(vault).found is True


def test_a_vault_that_cannot_be_read_is_reported_rather_than_raised():
    """A reader must never raise at its caller: the route that calls it is answering the owner."""
    class Angry:
        def notes(self):
            raise OSError("the notes folder went away")

    out = last_teardown(Angry())
    assert out.found is False and "could not be read" in out.reason


# --- the recogniser -------------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "show me the last teardown",
    "Show me the last teardown.",
    "what was the latest teardown",
    "read me my last teardown",
    "open the most recent teardown",
    "the teardown from last time",
    "last teardown",
    "can you show me my previous teardown?",
])
def test_a_request_to_read_the_last_one_is_recognised(said):
    assert asked_for_the_last(said) is True


@pytest.mark.parametrize("said", [
    "",
    "take apart stripe.com",
    "tear down the old shed",
    "teardown stripe.com",
    "take apart the last site I looked at",
    "what is the weather",
    "show me my notes",
    "open my downloads folder",
    "the last thing I asked you",
    "most recent email",
])
def test_everything_else_reaches_nothing(said):
    """A sentence that merely sounds related must claim nothing: "teardown" alone is the request to
    take a site apart, which a different route owns."""
    assert asked_for_the_last(said) is False


def test_a_sentence_that_asks_for_a_new_capture_is_never_read_as_a_request_to_read():
    """Both words present, and still a capture request. The new-action check has to win."""
    assert asked_for_the_last("tear apart the last site I visited") is False
    assert asked_for_the_last("take apart my most recent teardown target") is False


# --- the route ------------------------------------------------------------------------------------

def test_the_route_answers_without_a_model_and_without_the_kit():
    """Answered from the vault, so it does not care whether Chrome, Node or REA are installed - and
    it must cost no model call, like every other deterministic route."""
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    result = assistant.run("show me the last teardown")
    assert result.status == "completed" and result.steps == 0
    assert result.result


def test_the_route_reports_absence_honestly_rather_than_inventing_one():
    from void.app import Assistant

    assistant = Assistant()
    assistant.clear_stop()
    reply = assistant.run("show me the last teardown").result or ""
    assert reply in (NO_VAULT, NONE_YET) or "could not be read" in reply


# --- the command line ----------------------------------------------------------------------------

def test_the_last_command_exits_two_when_there_is_nothing_to_show(capsys):
    """A script has to be able to tell "none yet" from "here it is", so absence is an exit code and
    not just a sentence."""
    from void.cli import cmd_teardown_last

    assert cmd_teardown_last(as_json=False) == 2
    assert capsys.readouterr().out.strip() in (NO_VAULT, NONE_YET)


def test_the_last_command_reports_absence_as_json_too(capsys):
    import json

    from void.cli import cmd_teardown_last

    assert cmd_teardown_last(as_json=True) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is False and payload["reason"] and payload["excerpt"] == ""


def test_the_last_command_needs_no_browser_node_or_rea():
    """It answers from the vault. Requiring the kit would make reading a note you already have
    depend on three things being installed."""
    import inspect

    from void.cli import cmd_teardown_last

    src = inspect.getsource(cmd_teardown_last)
    for kit_thing in ("CleanBrowser", "kit.run", "chrome", "npm"):
        assert kit_thing not in src


def test_the_teardown_subcommands_include_last():
    from void.cli import build_parser

    args = build_parser().parse_args(["teardown", "last"])
    assert args.command == "teardown" and args.teardown_action == "last"


def test_a_run_reports_its_progress_through_the_guarded_emitter():
    """The text progress runs everywhere, display or not: a capture is tens of seconds of silence
    otherwise, and a run that looks hung is a run people interrupt."""
    import inspect

    from void.cli import cmd_teardown_run

    src = inspect.getsource(cmd_teardown_run)
    assert "on_step=on_step" in src, "the CLI does not pass a progress callback"
    assert "TeardownPanelView.create()" in src, "the CLI never offers the on-screen panel"


# --- the view -------------------------------------------------------------------------------------

def test_importing_the_module_does_not_import_qt():
    """The CLI, the route and these tests all import this module. Needing PySide6 for that would
    make a panel a dependency of taking a website apart, which is backwards.

    Checked in a fresh interpreter: this process has very likely imported Qt already.
    """
    import subprocess
    import sys

    probe = ("import sys; import void.teardown.panel; "
             "sys.exit(1 if any(m == 'PySide6' or m.startswith('PySide6.') for m in sys.modules) else 0)")
    done = subprocess.run([sys.executable, "-c", probe], cwd=str(ROOT), capture_output=True, text=True)
    assert done.returncode == 0, f"importing void.teardown.panel pulled in Qt:\n{done.stderr}"


def test_the_view_never_constructs_a_widget_without_a_qapplication(monkeypatch):
    """The guard that matters, exercised on the dangerous path rather than inferred.

    Qt ABORTS the process - "QWidget: Must construct a QApplication before a QWidget" is a fatal
    abort, not a Python exception, so no try/except can catch it. On Linux with no display the
    import raises and is caught; on Windows PySide6 imports cleanly and the construction kills the
    interpreter. That is how this crashed CI a third of the way through the suite, mid-run, with no
    failure summary and no traceback at all.

    A FAKE PySide6.QtWidgets is injected rather than patching the real one, so this runs identically
    on a machine whose Qt cannot even be imported (this container has no libEGL). The pytest guard
    is switched off deliberately, so the QApplication check is the thing under test, and a bomb
    stands in for the widget: if the factory reaches it, this test fails loudly instead of the whole
    run dying silently.
    """
    import sys
    import types

    from void.runtime import diagnostics

    monkeypatch.setattr(diagnostics, "_running_under_pytest", lambda: False)

    reached = []

    class FakeApplication:
        @staticmethod
        def instance():
            return None

    def bomb(*_a, **_k):
        reached.append(True)
        raise AssertionError("a QWidget was constructed with no QApplication - Qt would abort here")

    widgets = types.ModuleType("PySide6.QtWidgets")
    widgets.QApplication = FakeApplication
    widgets.QWidget = bomb
    widgets.QLabel = bomb
    widgets.QVBoxLayout = bomb
    core = types.ModuleType("PySide6.QtCore")
    core.Qt = types.SimpleNamespace(FramelessWindowHint=1, WindowStaysOnTopHint=2, Tool=4,
                                    WA_ShowWithoutActivating=8, NoFocus=0)
    package = types.ModuleType("PySide6")
    package.QtWidgets = widgets
    package.QtCore = core
    monkeypatch.setitem(sys.modules, "PySide6", package)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", widgets)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", core)

    assert TeardownPanelView.create() is None
    assert not reached, "the factory reached the widget constructor"


def test_the_view_makes_no_real_window_from_a_test_process():
    """The second guard, and the reason it is first: a factory that built a real always-on-top
    window would put one on the developer's desktop on every run of the suite. Same rule as
    void/ui/tray_indicator.py, which found this class of problem first.
    """
    assert TeardownPanelView.create() is None


def test_the_factory_never_starts_a_qapplication_of_its_own():
    """A panel that quietly created one would be taking over the process's event loop to draw a
    courtesy. The runtime owns the application object; this asks whether one exists."""
    import ast
    import inspect
    import textwrap

    src = inspect.getsource(TeardownPanelView.create)
    for node in ast.walk(ast.parse(textwrap.dedent(src))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "QApplication", "the factory constructs a QApplication"
    assert "QApplication.instance()" in src


class FakeLabel:
    def __init__(self):
        self.text = ""
        self.style = ""

    def setText(self, value):
        self.text = value

    def setStyleSheet(self, value):
        self.style = value


class FakeWidget(FakeLabel):
    def __init__(self):
        super().__init__()
        self.shown = 0
        self.hidden = 0
        self.raised = 0

    def show(self):
        self.shown += 1

    def hide(self):
        self.hidden += 1

    def raise_(self):
        self.raised += 1


def _view():
    """Wired to fakes, so the paint logic is covered on every platform rather than skipped."""
    widget, heading = FakeWidget(), FakeLabel()
    rows = [FakeLabel() for _ in Step.ORDER]
    return TeardownPanelView(widget, rows, heading), widget, rows, heading


def test_the_view_paints_through_the_presenter_and_reports_what_it_drew():
    view, widget, rows, heading = _view()
    panel = view.render(Progress(host="example.com").mark(Step.OPEN, DONE, "1.4s to load"))
    assert panel.done == 1 and view.last is panel
    assert "1/8" in heading.text and "example.com" in heading.text
    assert LABELS[Step.OPEN] in rows[0].text and "1.4s to load" in rows[0].text
    assert widget.shown == 1


def test_every_step_gets_a_row_even_before_anything_has_happened():
    view, _widget, rows, _heading = _view()
    view.render(Progress())
    assert all(label.text for label in rows)
    assert [LABELS[s] for s in Step.ORDER] == [label.text for label in rows]


def test_each_rows_colour_follows_its_status():
    view, _widget, rows, _heading = _view()
    view.render(Progress().mark(Step.OPEN, DONE).mark(Step.READ_PAGE, FAILED))
    r, g, b = TeardownPanelView.TINTS[DONE]
    assert f"rgb({r},{g},{b})" in rows[0].style
    r, g, b = TeardownPanelView.TINTS[FAILED]
    assert f"rgb({r},{g},{b})" in rows[1].style
    r, g, b = TeardownPanelView.TINTS[PENDING]
    assert f"rgb({r},{g},{b})" in rows[-1].style


def test_a_widget_that_raises_mid_paint_still_returns_the_panel():
    """A capture has already spent a browser, seconds of observation and a model call. A panel may
    not be the thing that loses it."""
    class Hostile(FakeWidget):
        def show(self):
            raise RuntimeError("the compositor went away")

    view = TeardownPanelView(Hostile(), [FakeLabel() for _ in Step.ORDER], FakeLabel())
    panel = view.render(Progress().mark(Step.OPEN, DONE))
    assert panel.done == 1


def test_the_view_has_a_tint_for_every_status():
    """A status with no tint would paint as whatever the dictionary default happened to be."""
    assert set(TeardownPanelView.TINTS) == STATUSES
