"""Console-less, working-directory-independent launcher for the weekly pass.

Windows Task Scheduler runs an action without a dependable project working directory, exactly as the
HKCU Run key does, so ``pythonw.exe -m void maintenance run`` is not safe to register: it depends on
the repository happening to be the current directory. This bootstrapper derives the repository from
its own absolute path and then delegates to the existing CLI - the same shape, and for the same
reason, as :mod:`void.runtime.voice_startup`.

It owns no maintenance logic whatsoever. Whether a run is due, whether the week is already claimed
and whether another pass is in progress are all decided by the database inside
``void maintenance run``; this file only gets the interpreter into the right place to ask. That is
what makes a scheduler firing harmless however often it happens.

No elevation, no credential, no new permission: it runs as the interactive user and calls one
existing command.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

#: Argument vector handed to the existing CLI. ``run`` does nothing unless the database says the pass
#: is due, which is why the scheduler never needs to know what day it is.
COMMAND = ("maintenance", "run")


def _ensure_repo_on_path() -> None:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


def main(cli_main: Callable[[list[str]], int] | None = None) -> int:
    """Run the weekly pass if the database says it is due. Returns the CLI's exit code.

    A failure here is logged and reported as a non-zero code rather than raised: a scheduled task
    that raises produces an unreadable Windows "last result", and the whole point of this launcher is
    that a Sunday firing is observable.
    """
    _ensure_repo_on_path()
    from void.runtime.diagnostics import install_background_logging
    install_background_logging()
    log = logging.getLogger("void.maintenance_launcher")
    if cli_main is None:
        from void.cli import main as cli_main
    log.info("MAINTENANCE_TRIGGERED")
    try:
        result = int(cli_main(list(COMMAND)))
    except Exception:
        log.exception("MAINTENANCE_LAUNCH_FAILED")
        return 1
    log.info("MAINTENANCE_EXIT code=%s", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
