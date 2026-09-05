"""File actions: search, read, write/update, and safe delete.

All paths are confined to the configured ``allowed_roots`` so V.O.I.D cannot
wander outside the owner's intended scope. Deletes go to the Recycle Bin
(recoverable) - never a hard unlink - in V1.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from void.actions.base import Tool, ToolResult
from void.security.risk import RiskLevel

# Directories we never descend into during search - noise and/or huge.
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "$Recycle.Bin", "AppData", ".cache",
}
_MAX_READ_BYTES = 200_000
_MAX_LIST_ENTRIES = 200  # cap on entries returned by list_directory


class PathNotAllowed(Exception):
    """Raised when a path resolves outside the allowed roots."""


class FileActions:
    def __init__(self, allowed_roots: list[Path], delete_to_recycle_bin: bool = True):
        # Resolve roots once; empty list means "no confinement" (discouraged).
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots]
        self.delete_to_recycle_bin = delete_to_recycle_bin

    # --- safety --------------------------------------------------------

    def _confine(self, path_str: str) -> Path:
        """Resolve a path and ensure it lives under an allowed root."""
        path = Path(os.path.expanduser(path_str)).resolve()
        if not self.allowed_roots:
            # V1 safety: no configured roots means DENY everything (never
            # "allow the whole machine"). Configure security.allowed_roots.
            raise PathNotAllowed(
                "No allowed roots are configured; all file access is denied. "
                "Set security.allowed_roots in config."
            )
        for root in self.allowed_roots:
            try:
                if path == root or path.is_relative_to(root):
                    return path
            except ValueError:
                continue
        raise PathNotAllowed(
            f"'{path}' is outside the allowed roots "
            f"({', '.join(str(r) for r in self.allowed_roots)})."
        )

    def _search_roots(self, root: str | None) -> list[Path]:
        if root:
            return [self._confine(root)]
        # Only the configured roots - never fall back to the whole home dir.
        return list(self.allowed_roots)

    # --- handlers ------------------------------------------------------

    def search(self, query: str, root: str | None = None,
               max_results: int = 50) -> ToolResult:
        """Find files whose name matches ``query`` (substring or glob)."""
        query = (query or "").strip()
        if not query:
            return ToolResult.failure("Search query was empty.")

        is_glob = any(ch in query for ch in "*?[")
        needle = query.lower()
        matches: list[str] = []

        try:
            roots = self._search_roots(root)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))

        for base in roots:
            if not base.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                               and not d.startswith(".")]
                for name in filenames:
                    hit = (fnmatch.fnmatch(name.lower(), needle) if is_glob
                           else needle in name.lower())
                    if hit:
                        matches.append(str(Path(dirpath) / name))
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return ToolResult.success(f"No files matched '{query}'.", data=[])
        listing = "\n".join(matches)
        return ToolResult.success(
            f"Found {len(matches)} file(s) matching '{query}':\n{listing}",
            data=matches,
        )

    def list_dir(self, path: str | None = None,
                 max_entries: int = _MAX_LIST_ENTRIES) -> ToolResult:
        """List the files and subdirectories directly inside a directory.

        Non-recursive (one level, like ``ls``). Confined to ``allowed_roots``.
        When ``path`` is omitted, lists the configured allowed root(s) so the
        agent can discover the workspace layout. Each entry is typed as
        ``file`` or ``directory`` and carries its full path for later calls.
        """
        try:
            max_entries = int(max_entries)
        except (TypeError, ValueError):
            max_entries = _MAX_LIST_ENTRIES
        if max_entries <= 0:
            max_entries = _MAX_LIST_ENTRIES

        # Resolve which directory/directories to list.
        if path:
            try:
                base = self._confine(path)
            except PathNotAllowed as exc:
                return ToolResult.failure(str(exc), error=str(exc))
            if not base.exists():
                return ToolResult.failure(f"Directory does not exist: {base}")
            if not base.is_dir():
                return ToolResult.failure(f"Not a directory: {base}")
            bases = [base]
        else:
            if not self.allowed_roots:
                # Deny-by-default: no roots configured means no access at all.
                return ToolResult.failure(
                    "No allowed roots are configured; all file access is denied. "
                    "Set security.allowed_roots in config."
                )
            bases = [r for r in self.allowed_roots if r.is_dir()]
            if not bases:
                return ToolResult.failure(
                    "No configured workspace directory exists yet "
                    f"({', '.join(str(r) for r in self.allowed_roots)})."
                )

        entries: list[dict] = []
        truncated = False
        for base in bases:
            try:
                children = sorted(
                    base.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except OSError as exc:
                return ToolResult.failure(
                    f"Could not list {base}: {exc}", error=str(exc))
            for child in children:
                is_dir = child.is_dir()
                # Respect the same noise filter search uses for directories.
                if is_dir and (child.name in _SKIP_DIRS
                               or child.name.startswith(".")):
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    break
                entries.append({
                    "name": child.name,
                    "type": "directory" if is_dir else "file",
                    "path": str(child),
                })
            if truncated:
                break

        where = str(bases[0]) if len(bases) == 1 else "the configured roots"
        if not entries:
            return ToolResult.success(
                f"{where} is empty (no listable entries).", data=[])
        listing = "\n".join(f"[{e['type']}] {e['path']}" for e in entries)
        note = f" (truncated to {max_entries})" if truncated else ""
        return ToolResult.success(
            f"Contents of {where} - {len(entries)} entr"
            f"{'y' if len(entries) == 1 else 'ies'}{note}:\n{listing}",
            data=entries,
        )

    def read(self, path: str, max_bytes: int = _MAX_READ_BYTES) -> ToolResult:
        try:
            p = self._confine(path)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not p.is_file():
            return ToolResult.failure(f"Not a file: {p}")
        try:
            data = p.read_bytes()[:max_bytes]
            text = data.decode("utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.failure(f"Could not read {p}: {exc}", error=str(exc))
        note = "" if len(data) < max_bytes else f" (truncated to {max_bytes} bytes)"
        return ToolResult.success(f"Contents of {p}{note}:\n{text}", data=text)

    def write(self, path: str, content: str, overwrite: bool = False) -> ToolResult:
        """Create or update a text file. Overwriting requires overwrite=True."""
        try:
            p = self._confine(path)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        existed = p.exists()
        if existed and not overwrite:
            return ToolResult.failure(
                f"{p} already exists. Set overwrite=true to replace it."
            )
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.failure(f"Could not write {p}: {exc}", error=str(exc))
        verb = "Updated" if existed else "Created"
        return ToolResult.success(f"{verb} {p} ({len(content)} chars).", data=str(p))

    def delete(self, path: str) -> ToolResult:
        """Delete a file by sending it to the Recycle Bin (recoverable)."""
        try:
            p = self._confine(path)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not p.exists():
            return ToolResult.failure(f"Nothing to delete at {p}.")

        if self.delete_to_recycle_bin:
            try:
                from send2trash import send2trash
            except ImportError:
                return ToolResult.failure(
                    "send2trash is not installed; refusing to hard-delete. "
                    "Install it with: pip install send2trash"
                )
            try:
                send2trash(str(p))
            except Exception as exc:  # send2trash raises OSError subclasses
                return ToolResult.failure(
                    f"Could not move {p} to Recycle Bin: {exc}", error=str(exc)
                )
            return ToolResult.success(f"Moved {p} to the Recycle Bin (recoverable).")

        # Hard delete is intentionally not enabled by default in V1.
        return ToolResult.failure(
            "Hard delete is disabled in V1. Enable "
            "security.delete_to_recycle_bin or delete manually."
        )

    # --- dynamic risk --------------------------------------------------

    def _write_risk(self, arguments: dict) -> RiskLevel:
        """Modifying/overwriting an EXISTING file is HIGH (needs confirmation);
        creating a new file is MEDIUM. Fail safe to HIGH on any doubt.
        """
        path = arguments.get("path")
        if not path:
            return RiskLevel.HIGH
        try:
            p = Path(os.path.expanduser(path)).resolve()
            return RiskLevel.HIGH if p.exists() else RiskLevel.MEDIUM
        except Exception:
            return RiskLevel.HIGH

    # --- registration --------------------------------------------------

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="search_files",
                description=(
                    "Search for files by name (substring, or a glob like "
                    "'*.md') under the allowed roots. Use this to locate files "
                    "such as notes or projects before opening them."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Name substring or glob to match."},
                        "root": {"type": "string",
                                 "description": "Optional directory to search under."},
                        "max_results": {"type": "integer",
                                        "description": "Max results (default 50)."},
                    },
                    "required": ["query"],
                },
                handler=self.search,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="list_directory",
                description=(
                    "List the files and subdirectories directly inside a "
                    "directory (one level, not recursive). Use this to discover "
                    "folders such as 'Projects' before creating or opening files "
                    "in them. Omit 'path' to list the configured workspace "
                    "root(s). Each entry is marked as a file or a directory and "
                    "includes its full path for use in later tool calls."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": ("Directory to list. Omit to list "
                                                 "the workspace root(s).")},
                        "max_entries": {"type": "integer",
                                        "description": "Max entries (default 200)."},
                    },
                    "required": [],
                },
                handler=self.list_dir,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="read_file",
                description="Read a text file's contents.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path."},
                    },
                    "required": ["path"],
                },
                handler=self.read,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="write_file",
                description=(
                    "Create a new text file, or update an existing one by "
                    "passing overwrite=true. Writes the full content given. "
                    "Modifying or overwriting a file that already exists "
                    "requires the owner's confirmation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File path."},
                        "content": {"type": "string",
                                    "description": "Full text to write."},
                        "overwrite": {"type": "boolean",
                                      "description": "Replace if it exists."},
                    },
                    "required": ["path", "content"],
                },
                handler=self.write,
                # New file = MEDIUM (autonomous); existing file = HIGH (confirm).
                risk=RiskLevel.MEDIUM,
                risk_fn=self._write_risk,
            ),
            Tool(
                name="delete_file",
                description=(
                    "Delete a file by moving it to the Recycle Bin. This is "
                    "recoverable but still requires owner confirmation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File to delete."},
                    },
                    "required": ["path"],
                },
                handler=self.delete,
                # High risk on purpose: deleting user data always asks first.
                risk=RiskLevel.HIGH,
            ),
        ]
