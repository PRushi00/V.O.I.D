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


def current_protected(local_path: Path) -> list[Path]:
    """Effective protected (excluded) roots, resolved."""
    return Config.load(local_path=local_path).protected_roots()


def _covers(root: Path, other: Path) -> bool:
    """True if ``other`` equals ``root`` or is a descendant of it."""
    try:
        return other == root or other.is_relative_to(root)
    except ValueError:
        return False


def _persist(local_path: Path, key: str, roots: list[Path]) -> None:
    """Write a roots list under ``security.<key>``, preserving other keys."""
    data = _load_local(local_path)
    security = data.get("security")
    if not isinstance(security, dict):
        security = {}
        data["security"] = security
    security[key] = [str(r) for r in roots]
    local_path.parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)


def _add_deduped(existing: list[Path], new: Path, label: str) -> list[Path]:
    """Add ``new`` deterministically: reject if already covered, collapse the
    narrower roots that ``new`` now covers (redundancy handling)."""
    for r in existing:
        if _covers(r, new):
            raise RootError(f"Already covered by an existing {label} root: {r}")
    kept = [r for r in existing if not _covers(new, r)]
    return kept + [new]


def _validate_dir(path_str: str) -> Path:
    new = _normalize(path_str)
    if not new.exists():
        raise RootError(f"Path does not exist: {new}")
    if not new.is_dir():
        raise RootError(f"Not a directory: {new}")
    return new


# --- allowed (trusted) roots ---------------------------------------------

def list_roots(local_path: Path | None = None) -> list[Path]:
    lp = local_path or local_config_path()
    return current_roots(lp)


def add_root(path_str: str, local_path: Path | None = None) -> Path:
    """Authorize a trusted root (exact owner-supplied directory).

    Adds only the exact directory - never its parents. Rejects files,
    nonexistent paths, and roots already covered by an existing root; a broader
    new root collapses the narrower roots it now covers.
    """
    lp = local_path or local_config_path()
    new = _validate_dir(path_str)
    _persist(lp, "allowed_roots", _add_deduped(current_roots(lp), new, "allowed"))
    return new


def remove_root(path_str: str, local_path: Path | None = None) -> Path:
    """De-authorize a trusted root.

    Refuses to remove the final remaining root: an empty root set denies all
    file access, leaving V.O.I.D unable to operate on files.
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
    _persist(lp, "allowed_roots", remaining)
    return target


# --- protected (excluded) roots ------------------------------------------

def list_protected(local_path: Path | None = None) -> list[Path]:
    lp = local_path or local_config_path()
    return current_protected(lp)


def add_protected(path_str: str, local_path: Path | None = None) -> Path:
    """Exclude a directory subtree (overrides allowed_roots).

    Adds only the exact directory. Rejects files/nonexistent paths and roots
    already covered by an existing protected root; a broader new protected root
    collapses the narrower protected roots it now covers.
    """
    lp = local_path or local_config_path()
    new = _validate_dir(path_str)
    _persist(lp, "protected_roots",
             _add_deduped(current_protected(lp), new, "protected"))
    return new


def remove_protected(path_str: str, local_path: Path | None = None) -> Path:
    """Remove an exclusion. Removing all exclusions is allowed."""
    lp = local_path or local_config_path()
    target = _normalize(path_str)
    prot = current_protected(lp)
    tkey = _key(target)
    if tkey not in {_key(r) for r in prot}:
        raise RootError(f"Not a protected root: {target}")
    _persist(lp, "protected_roots", [r for r in prot if _key(r) != tkey])
    return target
