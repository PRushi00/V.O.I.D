"""Minimal system-tray listening indicator for the normal, voice-only
runtime - NOT the Blackhole/Singularity developer UI.

No big window, no transparent overlay, no WebGL/WebEngine renderer, no
particles: just a small notification-area icon (the same
``QSystemTrayIcon`` mechanism already used by the developer UI's tray in
:mod:`void.ui.widget` / :mod:`void.ui.singularity_overlay`) that mirrors
VoiceController's REAL state, so the normal headless runtime has some
visible acknowledgment that it is listening - the gap this module closes.

Cross-thread safety: VoiceController's ``on_state`` callback fires
synchronously on a non-Qt thread (the voice monitor thread, the serial
worker, or the wake pump thread). This module reuses the SAME sanctioned
bridge the developer UI already relies on for this
(:class:`void.ui.voice_bridge.VoiceStateBridge`) rather than inventing a
second cross-thread mechanism; its ``stateChanged`` signal is queued onto
the Qt thread automatically, so :meth:`TrayIndicator.set_state` only ever
runs on the thread that owns the QApplication.

Optional and best-effort by design: constructing a QApplication/tray icon
can fail (PySide6 not installed, no desktop/system tray available under
some session types). ``create()`` returns ``None`` on any such failure -
callers must treat that as "no indicator this run", and voice must keep
working exactly as it does today. This module never touches Assistant,
RiskGate, KillSwitch, task state, or audio - it only paints a small icon
from the state string it is given.
"""
from __future__ import annotations

import logging
from typing import Callable

_log = logging.getLogger(__name__)

# State -> a short (color, label) pair. Matches void.voice.state.VoiceState's
# actual values exactly (see void/voice/state.py) - never invented states.
# IDLE here means "mic available, armed/waiting for the wake phrase" (the
# wake reconciler only arms the detector while the session sits IDLE);
# LISTENING is the active-command-capture state, not "waiting for wake".
_STATE_STYLE: dict[str, tuple[tuple[int, int, int], str]] = {
    "idle":         ((120, 170, 255), "V.O.I.D - listening for \"Hey V.O.I.D.\""),
    "listening":    ((255, 160, 40),  "V.O.I.D - capturing your command"),
    "captured":     ((255, 160, 40),  "V.O.I.D - command captured"),
    "transcribing": ((80, 170, 255),  "V.O.I.D - transcribing"),
    "dispatched":   ((150, 120, 255), "V.O.I.D - working"),
    "speaking":     ((80, 220, 120),  "V.O.I.D - speaking"),
    "error":        ((220, 70, 70),   "V.O.I.D - voice error (push-to-talk may still work)"),
    "stopped":      ((160, 160, 160), "V.O.I.D - stopped (kill switch engaged)"),
    "closed":       ((160, 160, 160), "V.O.I.D - voice stopped"),
    # Orthogonal to the session states above (see
    # void.voice.runtime.VoiceController's mic-health supervisor): the
    # microphone stream itself has gone silent independent of what the
    # session/wake-word state machine thinks is happening, so the tray must
    # be able to say that distinctly rather than keep showing "listening".
    "mic_unavailable": ((200, 30, 30),  "V.O.I.D - microphone unavailable (retrying...)"),
    "mic_recovering":  ((230, 150, 0),  "V.O.I.D - microphone recovering..."),
}
_DEFAULT_STYLE = ((120, 120, 120), "V.O.I.D")


def style_for_state(state: str) -> tuple[tuple[int, int, int], str]:
    """Pure mapping (no Qt): state string -> (RGB color, tooltip). Exposed
    separately so the mapping itself is testable without a display."""
    return _STATE_STYLE.get(str(state).strip().lower(), _DEFAULT_STYLE)


class TrayIndicator:
    """A tray-only listening indicator. Construct via :func:`create`, never
    directly - the factory is what applies the "never break voice" guarantee."""

    def __init__(self, app, tray, icons: dict, bridge=None, menu=None):
        self._app = app
        self._tray = tray
        self._icons = icons          # state name -> prebuilt QIcon
        self._bridge = bridge
        # QMenu has no QWidget parent available here (QSystemTrayIcon is not
        # a QWidget, so it cannot own one) - without an explicit Python
        # reference kept for as long as the indicator itself, the menu (and,
        # with it, the tray's context menu / Stop-Rearm-Exit actions) is only
        # reachable via create()'s local scope and becomes collectible the
        # moment create() returns. Held here so it lives exactly as long as
        # the tray icon it belongs to.
        self._menu = menu
        self._on_exit: Callable[[], None] | None = None
        if bridge is not None:
            bridge.stateChanged.connect(self.set_state)

    # --- state -----------------------------------------------------------
    def set_state(self, state: str) -> None:
        """Update the icon + tooltip. Safe to call from the Qt thread only
        (via the bridge signal, or directly if the caller is already on it)."""
        color, tooltip = style_for_state(state)
        icon = self._icons.get(str(state).strip().lower())
        if icon is not None:
            self._tray.setIcon(icon)
        self._tray.setToolTip(tooltip)
        _log.info("TRAY_STATE %s", state)

    # --- lifecycle ---------------------------------------------------------
    def set_exit_handler(self, on_exit: Callable[[], None]) -> None:
        self._on_exit = on_exit

    def run_until(self, is_done: Callable[[], bool], poll_ms: int = 200) -> None:
        """Block (running the Qt event loop) until ``is_done()`` is true or
        the tray menu's Exit action fires. Returns after the loop quits -
        callers proceed with their own shutdown exactly as without a tray."""
        from PySide6.QtCore import QTimer

        timer = QTimer()
        timer.setInterval(poll_ms)
        timer.timeout.connect(lambda: is_done() and self._app.quit())
        timer.start()
        try:
            self._app.exec()
        finally:
            timer.stop()

    def stop(self) -> None:
        try:
            self._tray.hide()
        except Exception:
            pass


def create(assistant, bridge, *, on_exit: Callable[[], None] | None = None
          ) -> TrayIndicator | None:
    """Best-effort factory. Returns None (never raises) if a tray indicator
    cannot be created here - PySide6 missing, no system tray available, or
    any other environment limitation. Callers MUST treat None as "run voice
    exactly as if this module did not exist"."""
    try:
        # Never pop up a REAL, visible tray icon from a test process - the
        # same contamination pattern already found and fixed for the
        # production log (see void.runtime.diagnostics) applies here too: an
        # unmocked call from a test would otherwise create a real desktop
        # side effect every run.
        from void.runtime.diagnostics import _running_under_pytest
        if _running_under_pytest():
            return None

        from PySide6.QtCore import Qt
        from PySide6.QtGui import QIcon, QPainter, QPixmap, QColor, QPen
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu

        app = QApplication.instance()
        owns_app = app is None
        if app is None:
            app = QApplication([])
        app.setQuitOnLastWindowClosed(False)

        tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        _log.info("TRAY_SYSTEM_AVAILABLE %s", tray_available)
        if not tray_available:
            _log.info("TRAY_INDICATOR_UNAVAILABLE no system tray on this session")
            return None

        def _make_icon(rgb: tuple[int, int, int]) -> "QIcon":
            # A small translucent ring is easy to miss at the ~16x16 size
            # Windows actually renders in the tray - a solid, fully opaque
            # disc with a bright white border reads clearly at that size
            # against both light and dark taskbars.
            pm = QPixmap(32, 32)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(QPen(QColor(255, 255, 255, 255), 3))
            p.setBrush(QColor(*rgb, 255))
            p.drawEllipse(3, 3, 26, 26)
            p.end()
            return QIcon(pm)

        icons = {name: _make_icon(color) for name, (color, _tip) in _STATE_STYLE.items()}
        default_icon = _make_icon(_DEFAULT_STYLE[0])

        tray = QSystemTrayIcon(default_icon)
        tray.setToolTip(_DEFAULT_STYLE[1])

        menu = QMenu()
        act_stop = menu.addAction("Stop", lambda: assistant.stop(reason="tray menu"))
        act_rearm = menu.addAction("Rearm", assistant.clear_stop)
        menu.addSeparator()

        def _sync_menu():
            engaged = bool(getattr(assistant.kill_switch, "engaged", False))
            act_stop.setVisible(not engaged)
            act_rearm.setVisible(engaged)

        menu.aboutToShow.connect(_sync_menu)

        indicator = TrayIndicator(app, tray, icons, bridge=bridge, menu=menu)

        def _exit():
            if on_exit is not None:
                on_exit()

        menu.addAction("Exit V.O.I.D", _exit)
        tray.setContextMenu(menu)
        tray.show()
        indicator._owns_app = owns_app     # diagnostics/tests only
        # isVisible() reflects Qt's own internal flag, not confirmation from
        # the shell - "no window handle to register against" and similar
        # failures can still leave this True, but a False here would be a
        # definite, unambiguous sign something is wrong on THIS side.
        _log.info("TRAY_INDICATOR_READY qt_is_visible=%s platform=%s",
                  tray.isVisible(), app.platformName())
        return indicator
    except Exception:
        _log.info("TRAY_INDICATOR_UNAVAILABLE", exc_info=True)
        return None
