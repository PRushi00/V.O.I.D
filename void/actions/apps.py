"""Application actions: launch programs and open files/URLs.

V1 favours native OS launching over screen automation for reliability.
On Windows, files/folders open with ``os.startfile`` and apps launch via the
resolved executable or the shell. A small alias map covers the apps the owner
named (Cursor, VS Code, etc.); anything else is resolved from PATH.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from void.actions.base import Tool, ToolResult
from void.actions.files import FileActions, PathNotAllowed
from void.security.risk import RiskLevel

# Friendly name -> candidate executables (first that resolves wins).
_APP_ALIASES: dict[str, list[str]] = {
    "cursor": ["cursor", "Cursor"],
    "vscode": ["code", "code.cmd"],
    "code": ["code", "code.cmd"],
    "notepad": ["notepad"],
    "explorer": ["explorer"],
    "chrome": ["chrome", "google-chrome"],
    "edge": ["msedge"],
    "terminal": ["wt", "cmd"],
    "calc": ["calc"],
}


class AppActions:
    def __init__(self, file_actions: FileActions):
        # Reuse the file layer's confinement for open_path on local files.
        self._files = file_actions

    def _is_windows(self) -> bool:
        return sys.platform.startswith("win")

    def open_path(self, target: str) -> ToolResult:
        """Open a file, folder, or URL with its default handler."""
        target = (target or "").strip()
        if not target:
            return ToolResult.failure("No target given to open.")

        # URLs go straight to the browser.
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)
            return ToolResult.success(f"Opened URL {target}.")

        # Local paths are confined to allowed roots.
        try:
            p = self._files._confine(target)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not p.exists():
            return ToolResult.failure(f"Path does not exist: {p}")

        try:
            if self._is_windows():
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as exc:
            return ToolResult.failure(f"Could not open {p}: {exc}", error=str(exc))
        return ToolResult.success(f"Opened {p}.")

    def launch_app(self, name: str) -> ToolResult:
        """Launch an application by friendly name or executable."""
        name = (name or "").strip()
        if not name:
            return ToolResult.failure("No application name given.")

        candidates = _APP_ALIASES.get(name.lower(), [name])
        exe = next((c for c in candidates if shutil.which(c)), None)

        try:
            if exe:
                subprocess.Popen([exe])
                return ToolResult.success(f"Launched {name} ({exe}).")
            # Last resort on Windows: let the shell resolve it (Start menu apps).
            if self._is_windows():
                subprocess.Popen(["cmd", "/c", "start", "", name], shell=False)
                return ToolResult.success(
                    f"Asked Windows to start '{name}'. If nothing opened, "
                    f"the app may not be installed or on PATH."
                )
        except OSError as exc:
            return ToolResult.failure(f"Could not launch {name}: {exc}", error=str(exc))
        return ToolResult.failure(
            f"Could not find an executable for '{name}'. "
            f"Try the full program name or add it to PATH."
        )

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="open_path",
                description=(
                    "Open a file, folder, or URL with its default application "
                    "(e.g. open a document, reveal a folder, open a webpage)."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string",
                                   "description": "File path, folder, or URL."},
                    },
                    "required": ["target"],
                },
                handler=self.open_path,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="launch_app",
                description=(
                    "Launch an application by name, e.g. 'cursor', 'vscode', "
                    "'notepad', 'chrome'."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Application name or executable."},
                    },
                    "required": ["name"],
                },
                handler=self.launch_app,
                risk=RiskLevel.LOW,
            ),
        ]
