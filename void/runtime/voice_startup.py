"""Console-less, working-directory-independent V.O.I.D voice launcher.

Windows executes HKCU Run entries without a dependable project working
directory.  This tiny bootstrapper derives the repository from its own absolute
path, then delegates to the existing ``void voice`` command.  It owns no voice,
audio, wakeword, UI, or security policy.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable


def _ensure_repo_on_path() -> None:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


def _install_background_logging() -> None:
    """Write privacy-safe lifecycle diagnostics (void.log) for a ``pythonw``
    process. Shared with the other launchers - see void.runtime.diagnostics."""
    _ensure_repo_on_path()
    from void.runtime.diagnostics import install_background_logging
    install_background_logging()


def main(cli_main: Callable[[list[str]], int] | None = None) -> int:
    _ensure_repo_on_path()
    _install_background_logging()
    log = logging.getLogger("void.voice_startup")
    if cli_main is None:
        from void.cli import main as cli_main
    log.info("VOICE_AUTOSTART_LAUNCHED")
    try:
        result = cli_main(["voice"])
    except Exception:
        log.exception("VOICE_AUTOSTART_FATAL")
        return 1
    log.info("VOICE_AUTOSTART_EXIT code=%s", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
