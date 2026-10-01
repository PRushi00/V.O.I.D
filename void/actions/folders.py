"""Deterministic resolution of a spoken FOLDER name to one directory on this machine.

Why this exists at all. The file layer already has a directory search (``FileActions.find_dir``), and it is the
right tool for the model: it walks the whole allowed tree, up to a 20 000-directory budget. Measured on this
machine, with ``security.allowed_roots = ["C:\\"]``, that walk takes **13.7-24.6 seconds** and returns
*ambiguous* for every ordinary name tried ("Projects", "workspace", "Downloads"). It therefore cannot sit on a path
whose whole purpose is to answer in milliseconds without a model.

What this does instead is deliberately smaller in every dimension:

* **Bounded breadth.** Only the first ``depth`` levels below the owner's home and the configured allowed roots, with
  a visit budget, skipping the same noise/system directories the file layer skips. Measured: 7 ms for 282
  directories at depth 2 on this machine - and that is enough to see "Projects", "workspace" and "Downloads".
* **Identity matching only.** A query either normalises to the same thing as a directory's name, or is the same
  name spaced differently. There is no prefix tier, no vendor tier, no phonetic tier and nothing fuzzy: a folder is
  the owner's own content, and opening the wrong one is not a small mistake. Two matches is ``ambiguous``, never a
  tie-break.
* **No policy of its own.** Every candidate is passed through the ``confine`` callable the file layer supplies, so
  allowed roots, protected roots and the engine's own protected locations decide what may be seen - this module
  never reads that configuration and never bypasses it.

The name rules come from ``void.actions.app_names``, unchanged, so there is one place where "the same name spaced
differently" is defined.
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from void.actions.app_names import normalise, squash

_log = logging.getLogger("void.folders")

#: Levels below each root. 3 is what it takes on a real Windows machine: OneDrive redirects Desktop and Documents,
#: so the owner's "Projects" sits at "~/OneDrive/Attachments/Projects". Measured here: 196 directories in 217 ms at
#: depth 3, against 85 in 92 ms at depth 2 (which could not see it) and 364 in 421 ms at depth 4 (which added
#: nothing that resolved).
DEFAULT_DEPTH = 3
#: Directories visited before the scan stops. A bound, not a target: the measured scan sees 282.
DEFAULT_BUDGET = 1500
#: How long a scan is trusted. Same value as the application catalog, for the same reason.
DEFAULT_TTL_S = 600.0
#: At most this many candidates are reported for an ambiguous name.
_MAX_CANDIDATES = 4

#: Never descended into. The first group is the file layer's own skip list; the second is what only appears when a
#: scan starts at a drive root. Kept here as data rather than imported privately, and pinned by a test.
SKIP_DIRS = frozenset({
    "node_modules", ".git", "__pycache__", ".venv", "venv", "$Recycle.Bin", "AppData", ".cache",
    "Windows", "WinSxS", "System Volume Information", "Recovery", "ProgramData",
    "Program Files", "Program Files (x86)", "PerfLogs", "Config.Msi", "OneDriveTemp", "Intel", "MSOCache",
    # The account templates Windows copies for a NEW user. They contain a full set of "Downloads", "Documents",
    # "Desktop" ... which made every one of those names ambiguous against the owner's own.
    "Default", "Default User", "All Users", "Public",
})


@dataclass(frozen=True)
class FolderEntry:
    """One directory the owner could name. ``path`` is absolute and has already passed ``confine``."""
    name: str
    path: str


@dataclass
class FolderMatch:
    entry: FolderEntry | None = None
    candidates: tuple[FolderEntry, ...] = ()
    tier: str = ""                       # "exact" | "spacing"
    reason: str = ""                     # "" | "unknown" | "ambiguous"


@dataclass
class FolderCatalog:
    """A shallow, cached index of folder names, resolved by identity only.

    ``confine`` is the file layer's confinement check: given a path it returns the canonical ``Path`` when the owner
    has allowed it and it is not protected, and ``None`` otherwise. Passing it in keeps every security decision in
    one place; a catalog built without one indexes nothing, because deny-by-default is the only safe reading of
    "no policy available".
    """
    roots: tuple[str, ...] = ()
    confine: object = None
    depth: int = DEFAULT_DEPTH
    budget: int = DEFAULT_BUDGET
    ttl_s: float = DEFAULT_TTL_S
    clock: object = field(default=time.monotonic)
    scandir: object = field(default=os.scandir)

    def __post_init__(self) -> None:
        self._entries: tuple[FolderEntry, ...] | None = None
        self._by_norm: dict[str, list[FolderEntry]] = {}
        self._by_squash: dict[str, list[FolderEntry]] = {}
        self._built_at = 0.0
        self.scans = 0
        # Scans are serialised the same way application discovery is: a second caller waits for the one in flight
        # instead of starting its own. Reads are never locked - the index is published in one assignment.
        self._lock = threading.RLock()

    # --- the scan -----------------------------------------------------------------------------
    def _walk(self) -> list[FolderEntry]:
        """Directories within ``depth`` of the roots, each one confined. Never raises.

        BREADTH-first, which is not a detail: with overlapping roots (the home directory and a drive root) the same
        directory is reachable at different levels, and de-duplication must never be what decides whether its
        children are seen. Visiting shallowest-first means a directory is always reached by its shortest path, and
        the visit budget gives up the deepest level rather than an arbitrary subtree.
        """
        found: list[FolderEntry] = []
        seen_paths: set[str] = set()
        visited = 0
        queue = collections.deque((str(r), 0) for r in self.roots)
        while queue:
            base, level = queue.popleft()
            if level >= self.depth:
                continue
            try:
                it = self.scandir(base)
            except OSError:
                continue
            try:
                with it:
                    for e in it:
                        try:
                            if not e.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if e.name in SKIP_DIRS or e.name.startswith("."):
                            continue
                        visited += 1
                        if visited > self.budget:
                            _log.info("FOLDER_SCAN_BUDGET_REACHED budget=%d", self.budget)
                            return found
                        if e.path in seen_paths:
                            continue
                        seen_paths.add(e.path)
                        allowed = self._confined(e.path)
                        if allowed is not None:
                            found.append(FolderEntry(name=e.name, path=allowed))
                        # Descend even into a directory the owner may not OPEN: a child of it may still be allowed,
                        # and refusing to look would depend on policy this module deliberately does not read.
                        queue.append((e.path, level + 1))
            except OSError:
                continue
        return found

    def _confined(self, path: str) -> str | None:
        if self.confine is None:
            return None                  # no policy available means no access, never "allow everything"
        try:
            ok = self.confine(path)
        except Exception:                # noqa: BLE001 - a refusal is a refusal, however it is signalled
            return None
        return None if ok is None else str(ok)

    def _build(self) -> None:
        entries = self._walk()
        by_norm: dict[str, list[FolderEntry]] = {}
        by_squash: dict[str, list[FolderEntry]] = {}
        for e in entries:
            n = normalise(e.name)
            if not n:
                continue
            by_norm.setdefault(n, []).append(e)
            by_squash.setdefault(squash(e.name), []).append(e)
        # Published in one step, indexes first: a non-None _entries means the indexes behind it are ready.
        self._by_norm, self._by_squash = by_norm, by_squash
        self._entries = tuple(entries)
        self._built_at = self.clock()
        self.scans += 1

    def _ensure(self) -> None:
        if self._entries is not None and (self.ttl_s <= 0 or (self.clock() - self._built_at) < self.ttl_s):
            return
        with self._lock:
            if self._entries is not None and (self.ttl_s <= 0 or (self.clock() - self._built_at) < self.ttl_s):
                return                   # a concurrent caller already rebuilt it
            self._build()

    # --- the public surface -------------------------------------------------------------------
    def entries(self) -> tuple[FolderEntry, ...]:
        self._ensure()
        return self._entries or ()

    def invalidate(self) -> None:
        with self._lock:
            self._entries = None

    def resolve_name(self, query: object) -> FolderMatch:
        """Resolve a spoken folder name by IDENTITY: the same name, or the same name spaced differently.

        Never a prefix, never phonetic, never fuzzy - and never a path: a query containing a separator does not
        normalise to a directory's bare name, so it cannot address a location. More than one match is ambiguous.
        """
        n = normalise(query)
        if not n:
            return FolderMatch(reason="unknown")
        self._ensure()
        for tier, hits in (("exact", self._by_norm.get(n)), ("spacing", self._by_squash.get(squash(query)))):
            if not hits:
                continue
            if len(hits) == 1:
                return FolderMatch(entry=hits[0], tier=tier)
            return FolderMatch(candidates=tuple(hits[:_MAX_CANDIDATES]), tier=tier, reason="ambiguous")
        return FolderMatch(reason="unknown")


def scan_roots(home: object, allowed_roots) -> tuple[str, ...]:
    """The roots worth scanning: the owner's home first, then each configured allowed root.

    Home comes first because that is where the owner's own folders are, and a nested root adds nothing a shallower
    one did not already cover. Order only affects which duplicate is seen first; identity matching makes that
    irrelevant to the outcome.
    """
    out: list[str] = []
    for r in (home, *allowed_roots):
        if not r:
            continue
        try:
            p = str(Path(str(r)).expanduser())
        except (OSError, ValueError):
            continue
        if p not in out:
            out.append(p)
    return tuple(out)
