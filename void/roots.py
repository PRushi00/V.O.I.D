"""Owner-managed trusted filesystem roots (machine-local authorization).

Allowed roots are the filesystem confinement boundary for V.O.I.D's file
tools. Adding one is an AUTHORIZATION change, so it is owner-only: these
functions are called from the CLI, never exposed to the agent/LLM as a tool.
The agent cannot widen its own filesystem access.

Roots live ONLY in the gitignored ``config/local_config.yaml`` under
``security.allowed_roots`` (machine-local config). They are never stored in
source, Git-tracked config, the keyring, task state, or environment variables.

This module never scans the machine or guesses paths: the owner supplies an
exact directory path.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from void.config import Config, local_config_path


class RootError(ValueError):
    """Raised when a root cannot be added/removed (validation or safety)."""


def _normalize(path_str: str) -> Path:
    """Expand ``~``, resolve to a canonical absolute path (follows links)."""
    if not isinstance(path_str, str) or not path_str.strip():
        raise RootError("A non-empty directory path is required.")
    return Path(os.path.expanduser(path_str.strip())).resolve(strict=False)


def _key(p: Path) -> str:
    """Case-normalized comparison key (Windows paths are case-insensitive)."""
    return os.path.normcase(str(p))


def current_roots(local_path: Path) -> list[Path]:
    """Effective allowed roots (default config + local override), resolved."""
    return Config.load(local_path=local_path).allowed_roots()


def _load_local(local_path: Path) -> dict:
    if local_path.exists():
        with open(local_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _persist_roots(local_path: Path, roots: list[Path]) -> None:
    """Write the roots into local config, preserving any other local keys."""
    data = _load_local(local_path)
    security = data.get("security")
    if not isinstance(security, dict):
        security = {}
        data["security"] = security
    security["allowed_roots"] = [str(r) for r in roots]
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)


def list_roots(local_path: Path | None = None) -> list[Path]:
    lp = local_path or local_config_path()
    return current_roots(lp)


def add_root(path_str: str, local_path: Path | None = None) -> Path:
    """Authorize an additional trusted root (exact owner-supplied directory).

    Only adds the exact directory - never its parents. Rejects files,
    nonexistent paths, and normalized duplicates.
    """
    lp = local_path or local_config_path()
    new = _normalize(path_str)
    if not new.exists():
        raise RootError(f"Path does not exist: {new}")
    if not new.is_dir():
        raise RootError(f"Not a directory: {new}")
    roots = current_roots(lp)
    if _key(new) in {_key(r) for r in roots}:
        raise RootError(f"Already an allowed root: {new}")
    _persist_roots(lp, list(roots) + [new])
    return new


def remove_root(path_str: str, local_path: Path | None = None) -> Path:
    """De-authorize a trusted root.

    Refuses to remove the final remaining root: an empty root set denies all
    file access, leaving V.O.I.D unable to operate on files. The owner must
    keep at least one workspace root.
    """
    lp = local_path or local_config_path()
    target = _normalize(path_str)
    roots = current_roots(lp)
    tkey = _key(target)
    if tkey not in {_key(r) for r in roots}:
        raise RootError(f"Not an allowed root: {target}")
    remaining = [r for r in roots if _key(r) != tkey]
    if not remaining:
        raise RootError(
            "Refusing to remove the last remaining allowed root - V.O.I.D "
            "would have no filesystem access. Add another root first."
        )
    _persist_roots(lp, remaining)
    return target
