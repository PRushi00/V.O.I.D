"""WebGL blackhole overlay: an alternative desktop presence surface that hosts
the standalone `void-singularity-renderer` (Three.js/WebGL) inside a transparent,
click-through QWebEngineView instead of painting with QPainter.

It is a drop-in peer of void.ui.widget.VoidWidget with the SAME constructor
signature and the SAME runtime contract (show(), on_killswitch_state(engaged),
`closing` signal, showEvent-installed tray), so void.runtime.app.build_runtime
can build it via its injectable widget_factory with no other change. The proven
QPainter VoidWidget remains the default; this overlay is opt-in
(`python -m void singularity`) so the WebGL path can be validated live before it
ever becomes the default (the QPainter overlay is the rollback).

Ownership / security (identical guarantees to VoidWidget):
  * Pure OBSERVER of backend truth. It constructs no Assistant, opens no
    microphone / second audio owner, and never calls RiskGate/KillSwitch
    authorization (engage/reset/approve/deny/run). It only mirrors the
    authoritative VoiceState into the renderer, and exposes the same public
    Stop/Rearm/Close controls the CLI already has (assistant.stop /
    clear_stop), via a system-tray menu (the overlay is click-through).
  * The renderer is presentation-only. The Python side drives the page ONE WAY
    via page().runJavaScript(...); there is no QWebChannel and no Python object
    exposed to JavaScript, so the renderer can never call back into V.O.I.D
    (no tools, no filesystem, no AI, no network, no authority).
  * Lifecycle truth comes from void.ui.orb.BlackholePresenter (the same pure
    phase machine the QPainter overlay uses) - this module invents NO second
    state machine; void.ui.singularity_state.renderer_state_for only translates
    the presenter's phase/mode into the renderer's state vocabulary.

NOTE: like VoidWidget this needs a display, PySide6, AND QtWebEngine, so it is
validated by running it on the laptop, not in headless CI. The pure pieces
(singularity_state, host.html structure, offline/security invariants) ARE
covered by headless tests.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QUrl, QTimer, Signal
    from PySide6.QtGui import QPainter, QColor, QPen, QPixmap, QIcon
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QMessageBox, QSystemTrayIcon, QMenu,
    )
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError as exc:  # pragma: no cover - UI optional
    raise ImportError(
        "PySide6 + QtWebEngine are required for the WebGL overlay. Install with: "
        "pip install PySide6"
    ) from exc

from void.app import Assistant
from void.ui.orb import BlackholePresenter
from void.ui.singularity_state import renderer_state_for
from void.ui.voice_bridge import VoiceStateBridge  # noqa: F401  (type reference)
from void.voice.state import VoiceState

# Same cadence as the QPainter overlay - drives presenter.tick() so the
# time-based FORMING->ACTIVE and DISSOLVING->HIDDEN transitions advance and the
# mapped renderer state is pushed only when it actually changes.
_TICK_INTERVAL_MS = 33

# The vendored, offline renderer host page lives inside the renderer package.
_HOST_HTML = Path(__file__).resolve().parent / "void-singularity-renderer" / "host.html"


def host_html_path() -> Path:
    """Absolute path to the production host page. Exposed for tests."""
    return _HOST_HTML


def enable_webengine_gl() -> None:
    """Must be called BEFORE the QApplication is constructed. QtWebEngine needs
    shared OpenGL contexts to composite its WebGL canvas correctly. Safe/no-op
    if an application already exists."""
    if QApplication.instance() is not None:
        return
    try:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    except Exception:
        pass


class SingularityOverlay(QWidget):
    # Emitted when V.O.I.D should shut down (tray "Close"). Carries no
    # authority - purely a lifecycle notification the runtime funnels, exactly
    # like VoidWidget.closing.
    closing = Signal()

    def __init__(self, assistant: Assistant, confirm_bridge=None,
                 voice_bridge=None, voice_controller=None, *, preview: bool = False):
        super().__init__()
        self.assistant = assistant
        self.confirm_bridge = confirm_bridge
        self.voice_bridge = voice_bridge
        self.voice_controller = voice_controller

        # Full-screen, frameless, always-on-top, click-through overlay - the
        # SAME window contract as VoidWidget so the desktop is never obstructed.
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply_primary_geometry()

        # --- the WebGL surface -------------------------------------------
        self._view = QWebEngineView(self)
        self._view.setAttribute(Qt.WA_TransparentForMouseEvents)
        # Transparent page so the real desktop shows through where the shader's
        # alpha is 0 (everywhere the black hole is not).
        try:
            self._view.page().setBackgroundColor(Qt.transparent)
        except Exception:
            pass
        self._view.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._presenter = BlackholePresenter()
        self._now = time.monotonic
        self._tray = None
        self._page_ready = False
        self._last_pushed = None          # last renderer state string sent to JS
        self._pending_state = "HIDDEN"    # buffered until the page finishes loading

        self._view.loadFinished.connect(self._on_load_finished)
        self._view.load(QUrl.fromLocalFile(str(_HOST_HTML)))

        if self.voice_bridge is not None:
            self.voice_bridge.stateChanged.connect(self._on_voice_state_changed)

        if preview:
            # `python -m void singularity` with no live voice attached: show a
            # formed blackhole so the command is a useful visual preview.
            self._presenter.observe_voice_state(VoiceState.LISTENING, self._now())

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(_TICK_INTERVAL_MS)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

    # --- geometry ---------------------------------------------------------
    def _apply_primary_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())

    # --- state observation (read-only mirror of backend truth) ------------
    def _on_voice_state_changed(self, state: str) -> None:
        self._presenter.observe_voice_state(state, self._now())
        self._sync_renderer()

    def on_killswitch_state(self, engaged: bool) -> None:
        """Runtime coordinator's killswitch-watch hook. Read-only observer:
        mirrors the flag into the presenter (-> renderer STOPPED); never
        engages/resets/approves/denies."""
        self._presenter.observe_killswitch(engaged, self._now())
        self._sync_renderer()

    # --- animation: advance the presenter, push state only on change ------
    def _on_tick(self) -> None:
        self._presenter.tick(self._now())
        self._sync_renderer()

    def _sync_renderer(self) -> None:
        params = self._presenter.params(self._now())
        state = renderer_state_for(params.phase, params.mode)
        if state == self._last_pushed:
            return
        self._last_pushed = state
        self._push_state(state)

    def _push_state(self, state: str) -> None:
        if not self._page_ready:
            self._pending_state = state    # flush once the page has loaded
            return
        # One-way call into the page's minimal command surface. json.dumps
        # safely quotes the (known, constant) state string.
        self._view.page().runJavaScript(
            f"window.__void && window.__void.setState({json.dumps(state)});"
        )

    def _on_load_finished(self, ok: bool) -> None:
        self._page_ready = bool(ok)
        if ok:
            self._push_state(self._pending_state)

    # --- system tray (controls for a click-through overlay) ---------------
    def install_tray(self) -> None:
        if self._tray is not None or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(self._make_tray_icon(), self)
        self._tray.setToolTip("V.O.I.D")
        menu = QMenu()
        self._act_stop = menu.addAction("Stop", self._menu_stop)
        self._act_rearm = menu.addAction("Rearm", self._menu_rearm)
        menu.addSeparator()
        menu.addAction("Close V.O.I.D", self._menu_close)
        menu.aboutToShow.connect(self._sync_tray_menu)
        self._tray.setContextMenu(menu)
        self._tray.show()

    def _sync_tray_menu(self) -> None:
        engaged = bool(getattr(self.assistant.kill_switch, "engaged", False))
        self._act_stop.setVisible(not engaged)
        self._act_rearm.setVisible(engaged)

    def _make_tray_icon(self) -> QIcon:
        pm = QPixmap(32, 32)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor(255, 160, 80, 230), 3))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(6, 6, 20, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 255))
        p.drawEllipse(11, 11, 10, 10)
        p.end()
        return QIcon(pm)

    def _menu_stop(self) -> None:
        self.assistant.stop(reason="tray menu")

    def _menu_rearm(self) -> None:
        self.assistant.clear_stop()

    def _menu_close(self) -> None:
        self.closing.emit()

    # --- lifecycle -----------------------------------------------------
    def showEvent(self, event):
        self.install_tray()
        super().showEvent(event)

    def closeEvent(self, event):
        # Release the WebGL/Chromium resources deterministically: dispose the
        # renderer inside the page, then tear down the view so no orphan
        # QtWebEngineProcess lingers.
        try:
            if self._page_ready:
                self._view.page().runJavaScript("window.__void && window.__void.dispose();")
        except Exception:
            pass
        try:
            self._tick_timer.stop()
        except Exception:
            pass
        try:
            self._view.setParent(None)
            self._view.deleteLater()
        except Exception:
            pass
        self.closing.emit()
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)


def launch() -> int:
    """Standalone entry point for `python -m void singularity` - a persistent
    desktop app identical to `python -m void app` except the desktop presence
    is the WebGL overlay instead of the QPainter orb. Reuses the runtime's
    injectable widget_factory so exactly ONE Assistant / VoiceController /
    VoiceStateBridge is composed (the sole microphone owner), unchanged."""
    enable_webengine_gl()   # before any QApplication is created
    from void.runtime.app import build_runtime

    def widget_factory(assistant, bridge, voice_controller):
        return SingularityOverlay(assistant, voice_bridge=bridge,
                                  voice_controller=voice_controller)

    runtime, app = build_runtime(widget_factory=widget_factory)
    runtime.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(launch())
