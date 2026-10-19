"""Make V.O.I.D's own files readable by their owner and nobody else.

The gap this closes was found by ``void preflight`` on its first real run, which is the argument for
having a harness that executes rather than inspects: every unit test passed while
``~/.void/tasks.sqlite`` was being created mode 0644. That database holds task goals, the owner's own
sentences, model answers and tool output in plain text, so on a shared POSIX machine any other local
account could read the lot. ``void/device/cert.py`` was already careful with its TLS private key -
the rest of the state directory was not, and the asymmetry was an oversight rather than a decision.

The Jarvis prompt packs name exactly this rule ("every token or state file is readable by my user
only, at boot and after every write", ``docs/PROMPT_PACK_SOURCES.md`` §3), and it is cheap to keep.

Three properties, all deliberate:

**Best effort, never fatal.** A filesystem that cannot express POSIX modes (FAT32 on a USB stick, a
network share, Windows) must not stop V.O.I.D from working. :func:`restrict` reports what happened and
raises nothing; the caller carries on either way, and ``void preflight`` is what tells the owner when
the result is not what they would want.

**Windows is not pretended about.** POSIX mode bits are meaningless on NTFS, where access is decided
by the ACL that the state directory inherits from the user profile. ``os.chmod`` there only toggles
the read-only attribute, so this module does not claim to have hardened anything it has not; it
reports :data:`NOT_APPLICABLE` and the preflight says so in those words.

**Directories get 0700, files 0600.** A directory anyone can list tells them which tasks exist and
when, which is information the file modes are there to withhold.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

_log = logging.getLogger(__name__)

#: Modes V.O.I.D's own state is created with.
FILE_MODE = 0o600
DIR_MODE = 0o700

APPLIED = "applied"
#: The platform does not express access this way; the state directory's inherited ACL is what governs.
NOT_APPLICABLE = "not_applicable"
FAILED = "failed"
MISSING = "missing"


def restrict(path: str | Path, *, directory: bool | None = None) -> str:
    """Restrict ``path`` to its owner. Returns one of the module's status constants.

    ``directory`` is inferred when not given. Never raises: a permission change that cannot be made
    is a thing to report, not a reason to fail the operation the caller was in the middle of.
    """
    target = Path(path)
    if os.name == "nt":
        return NOT_APPLICABLE
    try:
        is_dir = target.is_dir() if directory is None else bool(directory)
        if not target.exists():
            return MISSING
        os.chmod(target, DIR_MODE if is_dir else FILE_MODE)
    except OSError as exc:
        # Content-free: the path can be user-specific, the errno cannot.
        _log.warning("OWNERSHIP_RESTRICT_FAILED errno=%s", getattr(exc, "errno", "?"))
        return FAILED
    return APPLIED


def restrict_tree(directory: str | Path) -> dict[str, int]:
    """Restrict a directory and every file directly in it. Returns a count per status."""
    counts: dict[str, int] = {}
    root = Path(directory)
    results = [restrict(root, directory=True)]
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_file():
                results.append(restrict(child, directory=False))
    for status in results:
        counts[status] = counts.get(status, 0) + 1
    return counts


def too_open(path: str | Path) -> str | None:
    """Why ``path`` is readable by someone other than its owner, or None when it is not.

    The question ``void preflight`` asks. On Windows it always answers None, because the mode bits it
    would read do not describe who can open the file.
    """
    target = Path(path)
    if os.name == "nt" or not target.exists():
        return None
    try:
        mode = target.stat().st_mode & 0o777
    except OSError as exc:
        return f"could not be read: {exc}"
    if target.is_dir():
        return None if not mode & 0o077 else f"directory mode {oct(mode)}"
    return None if not mode & 0o077 else f"mode {oct(mode)}"


def owner_only_flags() -> int:
    """``os.open`` flags for creating an owner-only file, where a caller opens one itself."""
    return stat.S_IRUSR | stat.S_IWUSR
