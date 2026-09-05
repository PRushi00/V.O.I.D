"""Minimal circular V.O.I.D widget (PySide6).

A small, always-on-top, draggable orb sits on the desktop. Click it to open a
compact panel: type a goal, watch V.O.I.D's steps stream in, and hit STOP at
any time. The normal Windows desktop stays fully visible - this is deliberately
not a big dashboard (that comes later).

The agent runs on a worker thread so the UI never freezes. High-risk actions
raise a confirmation dialog on the UI thread before proceeding.

NOTE: this module requires a display and PySide6, so it is validated by running
it on the laptop, not in headless CI.
"""
from __future__ import annotations

import sys
import threading

try:
    from PySide6.QtCore import Qt, QObject, Signal, Slot, QThread, QPoint
    from PySide6.QtGui import QColor, QPainter, QBrush, QFont
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit,
        QTextEdit, QPushButton, QLabel, QMessageBox,
    )
except ImportError as exc:  # pragma: no cover - UI optional
    raise ImportError(
        "PySide6 is required for the UI. Install it with: pip install PySide6"
    ) from exc

from void.app import Assistant


class ConfirmBridge(QObject):
    """Bridges a worker-thread confirmation request to a UI-thread dialog."""
    requested = Signal(str)

    def __init__(self):
        super().__init__()
        self._answer = False
        self._event = threading.Event()

    def confirm(self, description: str) -> bool:
        # Called on the worker thread; blocks until the UI answers.
        self._event.clear()
        self.requested.emit(description)
        self._event.wait()
        return self._answer

    @Slot(bool)
    def resolve(self, allowed: bool) -> None:
        self._answer = allowed
        self._event.set()


class Worker(QObject):
    event = Signal(str)
    finished = Signal(str, str)  # status, result-or-error

    def __init__(self, assistant: Assistant, goal: str):
        super().__init__()
        self.assistant = assistant
        self.goal = goal

    @Slot()
    def run(self):
        self.assistant.on_event = lambda m: self.event.emit(m)
        result = self.assistant.run(self.goal)
        self.finished.emit(result.status,
                           result.result or result.task.error or "")


class VoidWidget(QWidget):
    def __init__(self):
        super().__init__()
        self._drag_pos: QPoint | None = None
        self._thread: QThread | None = None
        self._worker: Worker | None = None

        self.confirm_bridge = ConfirmBridge()
        self.confirm_bridge.requested.connect(self._on_confirm_requested)
        self.assistant = Assistant(confirm_fn=self.confirm_bridge.confirm)

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(340, 300)
        self._build()

    # --- layout --------------------------------------------------------

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)

        self.orb = QLabel("V")
        self.orb.setAlignment(Qt.AlignCenter)
        self.orb.setFixedSize(56, 56)
        self.orb.setFont(QFont("Segoe UI", 20, QFont.Bold))
        self.orb.setStyleSheet(
            "color: white; background: rgba(30,30,40,220);"
            "border-radius: 28px;"
        )
        root.addWidget(self.orb, alignment=Qt.AlignHCenter)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Tell V.O.I.D what to do...")
        self.input.returnPressed.connect(self._on_submit)
        root.addWidget(self.input)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("background: rgba(20,20,28,235); color: #ddd;")
        root.addWidget(self.log)

        row = QHBoxLayout()
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setStyleSheet(
            "background:#a11; color:white; font-weight:bold; padding:6px;")
        self.stop_btn.clicked.connect(self._on_stop)
        row.addWidget(self.stop_btn)
        close_btn = QPushButton("Hide")
        close_btn.clicked.connect(self.hide)
        row.addWidget(close_btn)
        root.addLayout(row)

    # --- interactions --------------------------------------------------

    def _append(self, text: str):
        self.log.append(text)

    def _on_submit(self):
        goal = self.input.text().strip()
        if not goal or self._thread is not None:
            return
        if self.assistant.kill_switch.engaged:
            self.assistant.clear_stop()
        self.input.clear()
        self._append(f"> {goal}")

        self._thread = QThread()
        self._worker = Worker(self.assistant, goal)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.event.connect(self._append)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    @Slot(str, str)
    def _on_finished(self, status: str, message: str):
        self._append(f"[{status.upper()}] {message}")
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self._thread = None
        self._worker = None

    def _on_stop(self):
        self.assistant.stop(reason="UI STOP button")
        self._append("EMERGENCY STOP engaged.")

    @Slot(str)
    def _on_confirm_requested(self, description: str):
        reply = QMessageBox.question(
            self, "V.O.I.D needs authorization",
            f"Allow this action?\n\n{description}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        self.confirm_bridge.resolve(reply == QMessageBox.Yes)

    # --- dragging (frameless) -----------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - \
                self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)


def launch() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    widget = VoidWidget()
    widget.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(launch())
