"""V.O.I.D's desktop presence (PySide6): a full-screen, centered Blackhole
overlay that materializes only while V.O.I.D is active.

"The desktop is the interface. V.O.I.D is a presence within it." At rest
there is NO UI at all. When the wake word fires (or PTT), a black hole
forms at the CENTER of the primary display over the user's existing
wallpaper: distortion -> particles fall inward -> compression -> singularity
-> a thick, dimensional event horizon. It stays formed through
LISTENING/PROCESSING/SPEAKING (the event horizon is never replaced by a thin
line or a waveform), then dissolves back to nothing when the interaction
ends. The wallpaper is never modified - this is a transparent overlay above
it.

VoidWidget takes its Assistant, and for the persistent app a VoiceStateBridge
and VoiceController, as constructor arguments - it never constructs any of
them itself. It is a pure OBSERVER of backend state: it authorizes nothing,
executes nothing, and only mirrors the authoritative VoiceState into pixels
via void.ui.orb's BlackholePresenter/BlackholeRenderer. RiskGate/KillSwitch
remain the sole authorities.

The overlay is click-through (transparent for mouse input) so it never
obstructs the desktop; user controls (Stop/Rearm/Close) live in a small
system-tray menu. PTT for development remains the VoiceController's hotkey,
unchanged.

NOTE: this module requires a display and PySide6, so it is validated by
running it on the laptop, not in headless CI (the visual logic in
void.ui.orb IS headless-testable).
"""
from __future__ import annotations

import sys
import threading
import time

try:
    from PySide6.QtCore import Qt, QObject, Signal, Slot, QPoint, QTimer
    from PySide6.QtGui import QPainter, QColor, QPen, QPixmap, QIcon
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QLabel, QMessageBox,
        QSystemTrayIcon, QMenu,
    )
except ImportError as exc:  # pragma: no cover - UI optional
    raise ImportError(
        "PySide6 is required for the UI. Install it with: pip install PySide6"
    ) from exc

from void.app import Assistant
from void.ui.orb import (
    BlackholePresenter, BlackholeRenderer, VisualMode, particle_field,
)
from void.voice.state import VoiceState

_ANIM_INTERVAL_MS = 33   # ~30 FPS - smooth but restrained
_BUBBLE_DURATION_MS = 3500
_BUBBLE_MAX_WIDTH = 320


class ConfirmBridge(QObject):
    """Bridges a worker-thread confirmation request to a UI-thread dialog
    (used only by the standalone `python -m void ui` command)."""
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        self._answer = False
        self._event = threading.Event()

    def confirm(self, description: str) -> bool:
        self._event.clear()
        self.requested.emit(description)
        self._event.wait()
        return self._answer

    @Slot(bool)
    def resolve(self, allowed: bool) -> None:
        self._answer = allowed
        self._event.set()


class _MessageBubble(QWidget):
    """A tiny, temporary text bubble near the top-center - never a chat log."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(self)
        self._label.setWordWrap(True)
        self._label.setMaximumWidth(_BUBBLE_MAX_WIDTH)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setStyleSheet(
            "color: #f0f0f5; background: rgba(14,14,22,205);"
            "border-radius: 10px; padding: 9px 13px; font-size: 13px;"
        )
        layout.addWidget(self._label)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_text(self, text: str, center_x: int, top_y: int,
                  duration_ms: int = _BUBBLE_DURATION_MS) -> None:
        if not text:
            return
        self._label.setText(text)
        self.adjustSize()
        self.move(center_x - self.width() // 2, top_y)
        self.show()
        self._hide_timer.start(duration_ms)


class VoidWidget(QWidget):
    # Emitted when V.O.I.D should shut down (tray "Close"). Carries no
    # authority - purely a lifecycle notification the runtime funnels.
    closing = Signal()

    def __init__(self, assistant: Assistant, confirm_bridge=None,
                 voice_bridge=None, voice_controller=None, *, preview: bool = False):
        super().__init__()
        self.assistant = assistant
        self.confirm_bridge = confirm_bridge
        if self.confirm_bridge is not None:
            self.confirm_bridge.requested.connect(self._on_confirm_requested)
        self.voice_bridge = voice_bridge
        self.voice_controller = voice_controller

        # Full-screen, frameless, always-on-top, click-through overlay.
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        # never obstruct the desktop (click-through); scoped enum for PySide6 6.x
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._apply_primary_geometry()

        self._renderer = BlackholeRenderer()
        self._presenter = BlackholePresenter()
        self._now = time.monotonic
        self._start_time = self._now()
        self._bubble = _MessageBubble()
        self._tray = None

        if self.voice_bridge is not None:
            self.voice_bridge.stateChanged.connect(self._on_voice_state_changed)
            self.voice_bridge.transcriptReceived.connect(self._on_voice_text)
            self.voice_bridge.messageReceived.connect(self._on_voice_text)

        if preview:
            # `python -m void ui` (no voice): show a formed blackhole so the
            # command remains a useful visual preview.
            self._presenter.observe_voice_state(VoiceState.LISTENING, self._now())

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(_ANIM_INTERVAL_MS)
        self._anim_timer.timeout.connect(self._on_tick)
        self._anim_timer.start()

    # --- geometry ---------------------------------------------------------
    def _apply_primary_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())

    # --- system tray (controls for a click-through overlay) ---------------
    def install_tray(self) -> None:
        """Create the tray icon + menu. Called by the persistent runtime
        after a QApplication exists. Safe/no-op if tray is unavailable."""
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

    # --- state observation (read-only mirror of backend truth) ------------
    @Slot(str)
    def _on_voice_state_changed(self, state: str) -> None:
        self._presenter.observe_voice_state(state, self._now())

    def on_killswitch_state(self, engaged: bool) -> None:
        """Called by the runtime coordinator's killswitch-watch timer. A
        read-only observer: only mirrors the flag into the presenter; never
        engages/resets/approves/denies - that authority stays with
        Assistant/KillSwitch."""
        self._presenter.observe_killswitch(engaged, self._now())

    # --- temporary message bubble -----------------------------------------
    def show_message(self, text: str) -> None:
        geo = self.geometry()
        self._bubble.show_text(text, geo.center().x(), geo.top() + max(60, geo.height() // 8))

    @Slot(str)
    def _on_voice_text(self, text: str) -> None:
        self.show_message(text)

    # --- animation + painting ---------------------------------------------
    def _on_tick(self) -> None:
        self._presenter.tick(self._now())
        self.update()

    def paintEvent(self, event):
        if not self._presenter.is_visible():
            return   # nothing painted -> fully transparent -> desktop normal
        painter = QPainter(self)
        try:
            now = self._now()
            elapsed = now - self._start_time
            params = self._presenter.params(now)
            particles = particle_field(params, elapsed)
            self._renderer.paint(painter, self.rect(), params, particles, elapsed)
        finally:
            painter.end()

    @Slot(str)
    def _on_confirm_requested(self, description: str) -> None:
        reply = QMessageBox.question(
            self, "V.O.I.D needs authorization",
            f"Allow this action?\n\n{description}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        self.confirm_bridge.resolve(reply == QMessageBox.Yes)

    # --- lifecycle -----------------------------------------------------
    def showEvent(self, event):
        # The persistent runtime calls widget.show() once at startup; install
        # the tray then (a QApplication exists by now). Idempotent.
        self.install_tray()
        super().showEvent(event)

    def closeEvent(self, event):
        self.closing.emit()
        if self._tray is not None:
            self._tray.hide()
        super().closeEvent(event)


def launch() -> int:
    """Standalone entry point for `python -m void ui` - one Assistant, a
    synchronous confirmation dialog, no voice. Shows a formed blackhole
    preview (there is no wake word here to trigger formation)."""
    app = QApplication.instance() or QApplication(sys.argv)
    confirm_bridge = ConfirmBridge()
    assistant = Assistant(confirm_fn=confirm_bridge.confirm)
    widget = VoidWidget(assistant, confirm_bridge=confirm_bridge, preview=True)
    widget.show()   # showEvent installs the tray
    return app.exec()


if __name__ == "__main__":
    sys.exit(launch())
