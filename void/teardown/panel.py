"""The teardown panel: live progress through the eight steps, and the reader for the last one.

REA's eleventh prompt asks for "the live panel, the reader, 'show me the last teardown'". Both halves
are here, and both are decidable without a screen:

* :func:`panel_for` maps a run's step events onto rows a view can draw. It is a pure function over a
  closed step vocabulary, so the ordering, the statuses, and what happens when a step fails are all
  asserted headlessly.
* :func:`last_teardown` answers "show me the last teardown" from the vault. The note is already on
  disk - ``write_note`` puts it there through the confined file layer - so the reader is a query, not
  a second store.

The pack's panel is a progress display, and that is all this is. It reports; it cannot start, stop or
alter a capture, and :class:`Step` is a closed set so a view can switch on it safely.

Nothing here imports Qt. :class:`TeardownPanelView` imports PySide6 inside its own methods, the same
arrangement ``void.ui.orb`` and ``void.ui.focus_card`` use, so the CLI and these tests can import
this module on a machine with no display.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

#: Where ``write_note`` files a teardown, relative to the vault's captures folder. Imported rather
#: than restated so the reader below and the writer can never disagree about where notes live.
from void.teardown import NOTE_DIR

_log = logging.getLogger(__name__)

#: The capture's own order, named. These mirror the numbered comments in ``Teardown.capture`` - the
#: sequence is the pack's and the reason it matters is that REA must be WATCHING while V.O.I.D
#: scrolls, so a panel that implied a different order would misdescribe the evidence.
class Step:
    OPEN = "open"                 #: a clean browser, and the page loaded
    READ_PAGE = "read_page"       #: the page's own description of itself, before anything scrolls it
    ATTACH = "attach"             #: REA attached to the page target
    OBSERVE = "observe"           #: REA watching while V.O.I.D scrolls into its window
    BUNDLE = "bundle"             #: the script bundle, read statically
    SNAPSHOT = "snapshot"         #: one viewport picture, from the top
    ASSETS = "assets"             #: the browser's own log of every file the page loaded
    FINGERPRINT = "fingerprint"   #: licence banners, and the three-way attribution

    ORDER = (OPEN, READ_PAGE, ATTACH, OBSERVE, BUNDLE, SNAPSHOT, ASSETS, FINGERPRINT)
    ALL = frozenset(ORDER)


LABELS = {
    Step.OPEN: "Opening a clean browser",
    Step.READ_PAGE: "Reading the page as loaded",
    Step.ATTACH: "Attaching the reader",
    Step.OBSERVE: "Watching while it scrolls",
    Step.BUNDLE: "Reading the script bundle",
    Step.SNAPSHOT: "Taking one snapshot",
    Step.ASSETS: "Listing every file it loaded",
    Step.FINGERPRINT: "Fingerprinting what built it",
}

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
#: A step that could not run but did not break the capture - a snapshot the kit declined, say. The
#: distinction from FAILED is the point: a teardown with a missing snapshot is still a teardown, and
#: a panel that painted both the same would make a partial result look like a broken one.
SKIPPED = "skipped"

STATUSES = frozenset({PENDING, RUNNING, DONE, FAILED, SKIPPED})


@dataclass(frozen=True)
class Row:
    """One step, as a view draws it."""

    step: str
    label: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class Panel:
    """A whole run, as a view draws it.

    ``done``/``total`` are counted rather than left to the view, and ``finished`` is true only when
    every step has reached a terminal status - so a panel cannot claim completion while a step is
    still running.
    """

    rows: tuple[Row, ...]
    done: int
    total: int
    failed: int
    skipped: int
    running: str
    finished: bool
    host: str = ""
    seconds: float = 0.0

    @property
    def progress(self) -> float:
        return 0.0 if not self.total else (self.done + self.failed + self.skipped) / self.total


@dataclass
class Progress:
    """The accumulating record of one run. A view holds one of these and re-renders on each event.

    Deliberately append-only per step and tolerant of an unknown step name: a capture is a long job
    with a browser in it, and a panel is the last thing that should be able to raise inside one.
    """

    statuses: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    host: str = ""
    seconds: float = 0.0

    def mark(self, step: str, status: str, detail: str = "") -> "Progress":
        if step in Step.ALL and status in STATUSES:
            self.statuses[step] = status
            if detail:
                self.details[step] = detail
        return self


def emitter(on_step):
    """Wrap a progress callback so its own failures can never reach the capture.

    Extracted from ``Teardown.capture`` so this guarantee is a unit test rather than a comment. A
    capture spends a real browser, seconds of observation and a model call; a panel that raised
    mid-run would lose all of it, and the panel is the least important thing in that list.

    Returns a callable that is safe to invoke unconditionally - ``None`` in means a no-op, so the
    caller needs no branch of its own.
    """
    def step(name: str, status: str, detail: str = "") -> None:
        if on_step is None:
            return
        try:
            on_step(name, status, detail)
        except Exception:                       # noqa: BLE001 - a panel may not break a run
            _log.debug("TEARDOWN_STEP_CALLBACK_FAILED", exc_info=True)

    return step


def panel_for(progress: Progress) -> Panel:
    """Render a panel from what has happened so far. Total: every step always has a row.

    A step nothing has been said about is PENDING, so the panel shows the whole shape of the job
    from the first event onwards rather than growing a row at a time. Someone watching a teardown
    wants to know what is still to come, which is most of the value of showing it at all.
    """
    rows = []
    done = failed = skipped = 0
    running = ""
    for step in Step.ORDER:
        status = progress.statuses.get(step, PENDING)
        rows.append(Row(step=step, label=LABELS[step], status=status,
                        detail=progress.details.get(step, "")))
        if status == DONE:
            done += 1
        elif status == FAILED:
            failed += 1
        elif status == SKIPPED:
            skipped += 1
        elif status == RUNNING and not running:
            running = step
    finished = (done + failed + skipped) == len(Step.ORDER)
    return Panel(rows=tuple(rows), done=done, total=len(Step.ORDER), failed=failed,
                 skipped=skipped, running=running, finished=finished,
                 host=progress.host, seconds=progress.seconds)


# --- the reader -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class LastTeardown:
    """The most recent teardown note, or an honest account of there being none."""

    found: bool
    title: str = ""
    path: str = ""
    note_id: int | None = None
    excerpt: str = ""
    reason: str = ""


#: Said when there is no teardown to show. A constant, like every other engine-authored line.
NONE_YET = "I have not taken a website apart yet."
NO_VAULT = "No notes folder is configured, so there is nowhere for a teardown note to be."


#: Words that make a sentence about the MOST RECENT teardown rather than a new one. Both halves are
#: required: "teardown" alone is the request to take a site apart, which a different route owns, and
#: a recency word alone belongs to nothing here.
_RECENCY = r"(?:last|latest|previous|most\s+recent|recent)"
_ASKED_FOR_THE_LAST = re.compile(
    rf"\b{_RECENCY}\b[^.?!]{{0,40}}?\bteardowns?\b|\bteardowns?\b[^.?!]{{0,20}}?\b{_RECENCY}\b",
    re.IGNORECASE)
#: A sentence that also names a NEW action wins by the other route: "take apart the last site I
#: looked at" is a capture request, not a request to read a note.
_NEW_CAPTURE = re.compile(r"\b(?:tear|take|rip|pull)\b[^.?!]{0,16}\bapart\b|\btear\s+down\b",
                          re.IGNORECASE)


def asked_for_the_last(utterance: object) -> bool:
    """True for "show me the last teardown" and its close relatives, false for everything else.

    A closed-set recogniser rather than a model, for the same reason every other route here is one:
    the answer is a file on disk, and a sentence that merely sounds related must reach nothing.
    """
    text = " ".join(str(utterance or "").split())
    if not text or _NEW_CAPTURE.search(text):
        return False
    return bool(_ASKED_FOR_THE_LAST.search(text))


def last_teardown(vault) -> LastTeardown:
    """The newest teardown note in the vault, by the note's own modified time.

    Reads the vault rather than keeping a pointer of its own: the note is the record, and a second
    store that could disagree with it would be a way to be wrong. Absence is reported as absence -
    no excerpt, no guess - for the same reason the notes vault answers that way.
    """
    if vault is None:
        return LastTeardown(found=False, reason=NO_VAULT)
    try:
        notes = list(vault.notes())
    except Exception as exc:                    # noqa: BLE001 - a reader must never raise at a caller
        return LastTeardown(found=False, reason=f"The notes folder could not be read: {exc}")
    wanted = [n for n in notes if _is_teardown(n)]
    if not wanted:
        return LastTeardown(found=False, reason=NONE_YET)
    newest = max(wanted, key=lambda n: getattr(n, "modified", 0.0))
    return LastTeardown(found=True, title=newest.title, path=newest.path,
                        note_id=newest.id, excerpt=newest.excerpt)


def _is_teardown(note) -> bool:
    """A note written by a teardown, identified by where it lives and what it is called.

    Both, because either alone is wrong: the group is how ``write_note`` files them, and the title
    prefix catches one the owner has since moved. A note that merely mentions a teardown is not one.
    """
    group = (getattr(note, "group", "") or "").lower()
    title = (getattr(note, "title", "") or "").lower()
    return NOTE_DIR in group.split("/") or title.startswith(NOTE_DIR)


class TeardownPanelView:
    """The Qt half: a frameless panel listing the eight steps as they happen.

    Like ``FocusCardView``, it returns None instead of raising where there is no display, and it
    swallows its own paint failures: a capture takes a browser, seconds of observation and a model
    call, and a progress display must never be the thing that loses all of it.
    """

    SIZE = (330, 268)
    MARGIN = 18
    TINTS = {PENDING: (118, 122, 130), RUNNING: (64, 150, 160), DONE: (96, 164, 118),
             FAILED: (198, 92, 84), SKIPPED: (150, 138, 96)}

    def __init__(self, widget, rows, heading):
        self._widget = widget
        self._rows = rows
        self._heading = heading
        self._last: Panel | None = None

    @classmethod
    def create(cls, parent=None) -> "TeardownPanelView | None":
        try:
            from PySide6.QtCore import Qt
            from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
        except Exception:                       # noqa: BLE001 - no Qt, or no display
            return None
        try:
            widget = QWidget(parent)
            widget.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
            widget.setAttribute(Qt.WA_ShowWithoutActivating)
            widget.setFocusPolicy(Qt.NoFocus)
            widget.resize(*cls.SIZE)
            layout = QVBoxLayout(widget)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(3)
            heading = QLabel("", widget)
            heading.setFocusPolicy(Qt.NoFocus)
            layout.addWidget(heading)
            rows = []
            for _ in Step.ORDER:
                label = QLabel("", widget)
                label.setFocusPolicy(Qt.NoFocus)
                layout.addWidget(label)
                rows.append(label)
            return cls(widget, rows, heading)
        except Exception:                       # noqa: BLE001
            return None

    def render(self, progress: Progress) -> Panel:
        panel = panel_for(progress)
        self._last = panel
        try:
            where = f" · {panel.host}" if panel.host else ""
            self._heading.setText(f"Teardown {panel.done + panel.failed + panel.skipped}"
                                  f"/{panel.total}{where}")
            self._heading.setStyleSheet("color: rgb(230,232,236); font-size: 13px; font-weight: 600;")
            for label, row in zip(self._rows, panel.rows):
                r, g, b = self.TINTS.get(row.status, self.TINTS[PENDING])
                text = row.label + (f" — {row.detail}" if row.detail else "")
                label.setText(text)
                label.setStyleSheet(f"color: rgb({r},{g},{b}); font-size: 11px;")
            self._widget.setStyleSheet(
                "QWidget { background-color: rgba(18,20,24,228); border-radius: 12px; "
                "border: 1px solid rgba(120,126,136,120); }")
            self._widget.show()
            self._widget.raise_()
        except Exception:                       # noqa: BLE001 - never lose a capture over a panel
            pass
        return panel

    def hide(self) -> None:
        try:
            self._widget.hide()
        except Exception:                       # noqa: BLE001
            pass

    @property
    def last(self) -> "Panel | None":
        return self._last
