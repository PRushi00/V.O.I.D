"""Configuration loading for V.O.I.D.

Loads ``config/default_config.yaml`` and merges an optional, gitignored
``config/local_config.yaml`` on top. Never contains secrets - API keys and
PINs live in the OS secret store (see :mod:`void.security.secrets`).
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

# Repo root = parent of the ``void`` package directory.
_PKG_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PKG_DIR.parent
_CONFIG_DIR = _REPO_ROOT / "config"


def local_config_path() -> Path:
    """Path to the gitignored, machine-local config override file.

    Machine-local settings (e.g. security.allowed_roots) live here, never in
    the Git-tracked default config.
    """
    return _CONFIG_DIR / "local_config.yaml"


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """Dict-backed config with dotted-path access.

    Example::

        cfg = Config.load()
        cfg.get("llm.gemini.model")            # -> "gemini-1.5-flash"
        cfg.get("agent.max_steps", default=8)  # -> 12
    """

    def __init__(self, data: dict[str, Any]):
        self._data = data

    @classmethod
    def load(
        cls,
        default_path: Path | None = None,
        local_path: Path | None = None,
    ) -> "Config":
        default_path = default_path or (_CONFIG_DIR / "default_config.yaml")
        local_path = local_path or (_CONFIG_DIR / "local_config.yaml")

        with open(default_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

        if local_path.exists():
            with open(local_path, "r", encoding="utf-8") as fh:
                overrides = yaml.safe_load(fh) or {}
            data = _deep_merge(data, overrides)

        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    # --- derived paths -------------------------------------------------

    def state_dir(self) -> Path:
        """Absolute path to the runtime state directory (created on demand)."""
        rel = self.get("app.state_dir", ".void")
        path = Path(os.path.expanduser("~")) / rel
        path.mkdir(parents=True, exist_ok=True)
        return path

    def allowed_roots(self) -> list[Path]:
        roots = self.get("security.allowed_roots", ["~"]) or []
        return [Path(os.path.expanduser(r)).resolve() for r in roots]
