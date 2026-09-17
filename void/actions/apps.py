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
from void.actions.computer import AppCatalog, ComputerBackendError
from void.actions.files import FileActions, PathNotAllowed
from void.security.risk import RiskLevel

# Engine-defined alias set (friendly name -> candidate executables resolved on
# PATH). This is fixed code, NOT model input: the model may only pick a key.
_APP_ALIASES: dict[str, list[str]] = {
    "cursor": ["cursor", "Cursor"],
    "vscode": ["code", "code.cmd"],
    "code": ["code", "code.cmd"],
    "notepad": ["notepad"],
    "explorer": ["explorer"],
    "chrome": ["chrome", "google-chrome"],
    "edge": ["msedge"],
    "calc": ["calc"],
}


class AppActions:
    def __init__(self, file_actions: FileActions, catalog: "AppCatalog | None" = None,
                 launcher=None):
        # Reuse the file layer's confinement for open_path on local files.
        self._files = file_actions
        # Engine-owned application catalog (from find_app discovery). launch_app
        # resolves an app_id against it; the LLM never supplies a path/command.
        self._catalog = catalog
        # Injectable launcher (entry) for testability; defaults to a no-shell
        # OS launch of a validated catalog target.
        self._launch = launcher or self._default_launch

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

    def _default_launch(self, kind: str, target: str) -> None:
        """No-shell OS launch of a validated target: a resolved .exe via Popen,
        or a Start-Menu .lnk via os.startfile. Never a shell/command string."""
        if kind == "lnk":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            subprocess.Popen([target])

    def launch_app(self, name: str) -> ToolResult:
        """Launch a VALIDATED application.

        Accepts an engine-owned ``app_id`` (from find_app) or a fixed alias key
        (cursor/vscode/notepad/...). It does NOT accept arbitrary commands, raw
        executable paths, or shell strings: unknown input is refused, never
        handed to a shell.
        """
        name = (name or "").strip()
        if not name:
            return ToolResult.failure("No application given.")

        # 1) Engine-owned app_id from the discovery catalog (preferred path).
        if self._catalog is not None:
            try:
                entry = self._catalog.resolve(name)
            except ComputerBackendError:
                entry = None
            if entry is not None:
                if not AppCatalog.revalidate(entry):
                    return ToolResult.failure(
                        f"'{entry.name}' is no longer available at its known "
                        f"location; re-run find_app.")
                try:
                    self._launch(entry.kind, entry.target)
                except OSError as exc:
                    return ToolResult.failure(
                        f"Could not launch {entry.name}: {exc}", error=str(exc))
                return ToolResult.success(f"Launched {entry.name}.")

        # 2) Fixed engine-defined alias -> resolve a real exe on PATH (no shell).
        if name.lower() in _APP_ALIASES:
            exe = next((shutil.which(c) for c in _APP_ALIASES[name.lower()]
                        if shutil.which(c)), None)
            if exe:
                try:
                    subprocess.Popen([exe])
                except OSError as exc:
                    return ToolResult.failure(
                        f"Could not launch {name}: {exc}", error=str(exc))
                return ToolResult.success(f"Launched {name} ({os.path.basename(exe)}).")
            return ToolResult.failure(
                f"'{name}' is a known alias but no executable for it was found "
                f"on PATH.")

        # 3) Refuse arbitrary input - no shell, no arbitrary path/command.
        return ToolResult.failure(
            f"Unknown application '{name}'. Use find_app to discover installed "
            f"applications, then launch_app with the returned app_id.")

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
                terminal_on_success=True,
            ),
            Tool(
                name="launch_app",
                description=(
                    "Launch a validated application by its app_id (from "
                    "find_app), or by a known alias like 'notepad', 'cursor', "
                    "'vscode', 'chrome'. Does NOT accept arbitrary commands or "
                    "executable paths - discover unknown apps with find_app "
                    "first."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": ("An app_id from find_app, or a "
                                                 "known alias.")},
                    },
                    "required": ["name"],
                },
                handler=self.launch_app,
                risk=RiskLevel.LOW,
                terminal_on_success=True,
            ),
        ]
