"""File actions: search, inspect, read, write/update, copy, move, and safe delete.

All paths are confined to the configured ``allowed_roots`` so V.O.I.D cannot
wander outside the owner's intended scope. Deletes go to the Recycle Bin
(recoverable) - never a hard unlink - in V1.

Operations with two paths (copy, move) confine BOTH ends independently, and
confine the destination for writing, so a transfer can never be used to reach a
location that ``write_file`` would itself refuse. Folders are deliberately not
transferable: one wrong argument on a recursive move is unrecoverable, and
nothing in V2 needs it.
"""
from __future__ import annotations

import datetime
import fnmatch
import os
import shutil
from pathlib import Path

from void.actions.base import Tool, ToolResult
from void.security.protected import EngineProtected
from void.security.risk import RiskLevel

# Directories we never descend into during search - noise and/or huge.
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "$Recycle.Bin", "AppData", ".cache",
}
# Extra system/noise directories skipped when scanning from a drive root.
# Kept small and deliberate - not a configurable subsystem.
_SYSTEM_NOISE_DIRS = {
    "Windows", "WinSxS", "$Recycle.Bin", "System Volume Information",
    "Recovery", "ProgramData",
}
_MAX_READ_BYTES = 200_000

# Binary reads (reopening a generated document to inspect it) need a far larger cap than text
# reads: a real deck or workbook easily exceeds 200 KB, and a truncated read would make a
# perfectly good file look corrupt. Still bounded - these bytes are counted, never shown to the
# model, so the cap only has to be larger than a plausible document.
_MAX_BINARY_READ_BYTES = 20_000_000
_MAX_LIST_ENTRIES = 200  # cap on entries returned by list_directory
# find_directory bounds (fixed for this phase; not configurable).
_FIND_DEFAULT_RESULTS = 25
_FIND_MAX_RESULTS = 50        # hard ceiling
_FIND_MAX_VISITED = 20_000    # directory-visit budget before giving up
# Filler words in a natural-language location hint that describe the RELATIONSHIP
# ("the Projects folder ON MY Desktop"), not the location itself. Dropped so the
# context matcher narrows by the real location word(s). Deliberately small.
_CONTEXT_FILLER = frozenset({
    "the", "a", "an", "my", "our", "your", "in", "on", "of", "at", "to",
    "into", "inside", "within", "under", "below", "from", "folder",
    "directory", "dir", "named", "called", "please",
})


def _risk_path(path_str: object) -> Path | None:
    """A path a risk decision may be based on, or None when no decision can safely be based on it.

    Needed because ``Path(...).exists()`` fails *open*: on Windows a path containing a NUL byte resolves
    without raising and then reports that it does not exist, so a nonsense destination was graded MEDIUM
    (no confirmation) rather than HIGH. Anything that is not a plain, non-empty, NUL-free string is
    refused here, and the caller fails safe.
    """
    if not isinstance(path_str, str) or not path_str.strip() or "\x00" in path_str:
        return None
    try:
        return Path(os.path.expanduser(path_str)).resolve()
    except (OSError, ValueError):
        return None


def _when(timestamp: float) -> str | None:
    """A filesystem timestamp as a plain local date and time, or None if it is unusable."""
    try:
        return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return None


def _size(num_bytes: int) -> str:
    """A byte count a person can hear: "4.2 MB", not "4404019 bytes"."""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class PathNotAllowed(Exception):
    """Raised when a path resolves outside the allowed roots."""


class FileActions:
    def __init__(self, allowed_roots: list[Path], delete_to_recycle_bin: bool = True,
                 protected_roots: list[Path] | None = None,
                 engine_protected: EngineProtected | None = None):
        # Resolve roots once; empty list means "no confinement" (discouraged).
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots]
        # Protected roots are EXCLUSIONS that override allowed_roots. Anything
        # inside a protected root is denied even when it also sits inside an
        # allowed root (e.g. OneDrive Personal under an authorized C:\).
        self.protected_roots = [Path(r).resolve() for r in (protected_roots or [])]
        # ENGINE-protected paths (V.O.I.D's own state/secrets, credential stores, ...): fixed by the engine, not by
        # config or the model. ``protected_roots`` above can only ADD to them; nothing here removes one.
        self._engine = engine_protected if engine_protected is not None else EngineProtected.default()
        self.delete_to_recycle_bin = delete_to_recycle_bin

    # --- safety --------------------------------------------------------

    def _is_protected(self, path: Path) -> bool:
        """True if ``path`` is a protected root or a descendant of one.

        Uses Path-based containment (never string prefixes), so a protected
        root ``.../Projects`` does not accidentally match ``.../ProjectsBackup``.
        """
        if self._engine.covers(path):
            return True
        for pr in self.protected_roots:
            try:
                if path == pr or path.is_relative_to(pr):
                    return True
            except ValueError:
                continue
        return False

    def _is_reparse_point(self, path: Path) -> bool:
        """True for symlinks/junctions (reparse points). Conservative: we never
        traverse or match these, so a link/junction cannot lead a name-based
        walk out of the allowed area or into a protected subtree."""
        try:
            return path.is_symlink()
        except OSError:
            return True  # if we cannot tell, treat as a link and skip it

    def _confine(self, path_str: str, write: bool = False) -> Path:
        """Resolve a path and ensure it is allowed and not protected.

        Precedence: ENGINE protection (raw form + identity) -> canonicalize -> PROTECTED check (deny) -> allowed
        check. Protected roots always win, so an excluded subtree is denied regardless of what the caller (or the
        LLM) requests. ``write`` additionally refuses the engine's write-deny roots (system dirs, V.O.I.D's code).
        """
        reason = self._engine.denies(path_str, write=write)
        if reason is not None:
            raise PathNotAllowed(
                f"That location is protected by V.O.I.D ({reason}) and cannot be accessed by file tools.")
        path = Path(os.path.expanduser(path_str)).resolve()
        # Protected roots override everything, evaluated after canonicalization
        # (so traversal and symlinks cannot slip past the exclusion).
        if self._is_protected(path):
            raise PathNotAllowed(
                f"'{path}' is inside a protected (excluded) directory and "
                f"cannot be accessed."
            )
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
            if not base.exists() or self._is_protected(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                # Prune noise AND never descend into protected subtrees.
                dirnames[:] = [
                    d for d in dirnames
                    if d not in _SKIP_DIRS and not d.startswith(".")
                    and not self._is_protected(Path(dirpath) / d)
                ]
                for name in filenames:
                    full = Path(dirpath) / name
                    if self._is_protected(full):
                        continue  # protected files never surface in results
                    hit = (fnmatch.fnmatch(name.lower(), needle) if is_glob
                           else needle in name.lower())
                    if hit:
                        matches.append(str(full))
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

    def find_dir(self, query: str, root: str | None = None,
                 max_results: int = _FIND_DEFAULT_RESULTS,
                 context: str | None = None) -> ToolResult:
        """Locate DIRECTORIES by name under the authorized roots.

        Exact case-insensitive name match (glob if the query has * ? [ ]); never
        substring, so 'Project' does not match 'Projects'. Bounded, pruned walk
        that never descends into protected subtrees, symlinks/junctions, noise,
        or system directories. Every emitted match is re-confined (allowed and
        NOT protected). The tool never picks a winner among multiple matches.

        ``context`` is an optional parent-location hint from the owner's request
        (e.g. "OneDrive Desktop" or "StudioVerse"). When given, candidates are
        DETERMINISTICALLY filtered to those whose real path contains every
        context token (case-insensitive) - used only to NARROW, never to rank or
        pick. Cardinality of the filtered set decides the outcome, so a context
        that leaves several candidates is still reported as ambiguous.
        """
        query = (query or "").strip()
        if not query:
            return ToolResult.failure("Search query was empty.")
        try:
            max_results = int(max_results)
        except (TypeError, ValueError):
            max_results = _FIND_DEFAULT_RESULTS
        if max_results <= 0:
            max_results = _FIND_DEFAULT_RESULTS
        max_results = min(max_results, _FIND_MAX_RESULTS)

        is_glob = any(ch in query for ch in "*?[")
        needle = query.lower()

        # Resolve search roots. An explicit root is confined by _search_roots;
        # a protected or out-of-bounds root fails here with the standard message.
        try:
            roots = self._search_roots(root)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if root:
            base0 = roots[0]
            if not base0.exists():
                return ToolResult.failure(f"Directory does not exist: {base0}")
            if not base0.is_dir():
                return ToolResult.failure(f"Not a directory: {base0}")

        skip = _SKIP_DIRS | _SYSTEM_NOISE_DIRS
        matches: list[dict] = []
        visited = 0
        visited_truncated = False
        capped = False

        for base in roots:
            if not base.exists() or not base.is_dir() or self._is_protected(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
                visited += 1
                if visited > _FIND_MAX_VISITED:
                    visited_truncated = True
                    break

                # Match the current directory's own name.
                name = Path(dirpath).name
                hit = (fnmatch.fnmatch(name.lower(), needle) if is_glob
                       else name.lower() == needle)
                if hit:
                    try:
                        confined = self._confine(dirpath)  # canonical + policy
                    except PathNotAllowed:
                        confined = None
                    if confined is not None and not self._is_protected(confined):
                        matches.append({"name": name, "path": str(confined)})
                        if len(matches) >= max_results:
                            capped = True
                            break

                # Prune before descending: noise/system, dotted, protected
                # subtrees, and reparse points (symlinks/junctions).
                dirnames[:] = [
                    d for d in dirnames
                    if d not in skip and not d.startswith(".")
                    and not self._is_protected(Path(dirpath) / d)
                    and not self._is_reparse_point(Path(dirpath) / d)
                ]
            if capped or visited_truncated:
                break

        incomplete = capped or visited_truncated

        # Deterministic, structure-aware contextual narrowing (never ranking or
        # traversal/depth order). See _narrow_by_context: the owner's location
        # hint is matched against each candidate's real ANCESTOR segments, most
        # specific tier first. It only ever FILTERS; cardinality below still
        # decides RESOLVED / AMBIGUOUS / NOT_FOUND.
        ctx = (context or "").strip()
        pre_filter_count = len(matches)
        if ctx:
            matches = self._narrow_by_context(matches, ctx, needle)

        note = " (results may be incomplete)" if incomplete else ""
        if not matches:
            if ctx and pre_filter_count:
                # Candidates existed by name but none matched the context.
                return ToolResult.success(
                    f"No '{query}' directory matched the context '{ctx}'. "
                    f"Ask the owner for the exact location.", data=[])
            if visited_truncated:
                return ToolResult.success(
                    f"Search was INCOMPLETE (reached the directory-visit limit) "
                    f"before a match for '{query}' could be confirmed; the folder "
                    f"may still exist. Narrow the search with a 'root'.", data=[])
            return ToolResult.success(f"No directories matched '{query}'.", data=[])
        if len(matches) == 1:
            ctx_note = f" (context '{ctx}')" if ctx else ""
            return ToolResult.success(
                f"Found 1 directory matching '{query}'{ctx_note}{note}: "
                f"{matches[0]['path']}", data=matches)
        listing = "\n".join(m["path"] for m in matches)
        ctx_note = f" for context '{ctx}'" if ctx else ""
        return ToolResult.success(
            f"Found {len(matches)} directories matching '{query}'{ctx_note} "
            f"(ambiguous - do NOT pick one; ask the owner which){note}:\n{listing}",
            data=matches,
        )

    def _narrow_by_context(self, matches: list[dict], context: str,
                           needle: str) -> list[dict]:
        """Filter directory candidates by a natural-language location hint,
        using real filesystem STRUCTURE - never ranking, order, or depth.

        The hint is tokenized; filler words and any token equal to the directory
        name itself are dropped (so "OneDrive Projects" hints the *parent*
        OneDrive, not the target). Candidates are then filtered by the most
        specific tier that yields any match:

          Tier 1 - immediate-parent anchor: the most specific (last) meaningful
                   token equals the candidate's immediate parent directory, and
                   every earlier meaningful token is one of its ancestor
                   segments. ("Desktop" -> .../Desktop/Projects only.)
          Tier 2 - exact ancestor-segment: every meaningful token matches an
                   exact ancestor segment. Handles a hint that names a higher
                   ancestor ("StudioVerse" -> .../StudioVerse/code/Projects).
          Tier 3 - substring-anywhere over the full path: the original
                   (pre-9A) behavior, kept only as a backward-compatible
                   fallback when the structural tiers find nothing.

        Returns the filtered subset. The caller's cardinality check still
        decides the outcome, so a hint that leaves >1 candidate stays AMBIGUOUS
        and a hint that matches none is NOT_FOUND - this never picks a winner.
        """
        raw = [t for t in context.lower().replace("/", " ").replace("\\", " ").split()
               if t]
        tokens = [t for t in raw if t not in _CONTEXT_FILLER and t != needle]

        def ancestor_segments(path: str) -> list[str]:
            # All path components ABOVE the matched directory, lowercased.
            return [p.lower() for p in Path(path).parts[:-1]]

        if not tokens:
            # Only filler / the name itself: no usable location. Fall back to a
            # substring pass so an odd hint can still help, else leave as-is.
            if raw:
                sub = [m for m in matches
                       if all(t in m["path"].lower() for t in raw)]
                return sub if sub else matches
            return matches

        # Tier 1: immediate-parent anchor (+ earlier tokens as ancestors).
        anchor, earlier = tokens[-1], tokens[:-1]
        tier1 = []
        for m in matches:
            ancestors = ancestor_segments(m["path"])
            if not ancestors:
                continue
            if ancestors[-1] == anchor and all(e in ancestors for e in earlier):
                tier1.append(m)
        if tier1:
            return tier1

        # Tier 2: every meaningful token is an exact ancestor segment.
        tier2 = [m for m in matches
                 if all(t in ancestor_segments(m["path"]) for t in tokens)]
        if tier2:
            return tier2

        # Tier 3: backward-compatible substring-anywhere over the full path.
        return [m for m in matches
                if all(t in m["path"].lower() for t in raw)]

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
            bases = [r for r in self.allowed_roots
                     if r.is_dir() and not self._is_protected(r)]
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
                # Never expose protected-root entries or their contents.
                if self._is_protected(child):
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
            p = self._confine(path, write=True)
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

    def write_bytes(self, path: str, payload: bytes, overwrite: bool = True) -> ToolResult:
        """Create or replace a file from raw bytes, under exactly the same confinement as ``write``.

        Exists because a generated document (docx/pptx/xlsx/pdf) is a ZIP or a byte stream, and routing it
        through the text ``write`` would corrupt it - Windows translates ``\\n`` to ``\\r\\n`` in text mode,
        which breaks a ZIP container. The alternative would be for the artifact layer to open files itself,
        which would put a second, unconfined write path in V.O.I.D. One confinement implementation is worth
        more than avoiding this method.

        **Not exposed as a tool.** There is no ``write_bytes`` in :meth:`tools`, so the model cannot ask for
        an arbitrary byte write anywhere; only V.O.I.D's own artifact generator reaches this.

        ``overwrite`` defaults to True because the caller has already passed the RiskGate, which grades an
        existing path HIGH and so has already asked the owner. It is a parameter rather than a constant so a
        caller that has not been gated can keep the create-only behaviour.
        """
        try:
            p = self._confine(path, write=True)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not isinstance(payload, (bytes, bytearray)):
            return ToolResult.failure("Internal error: byte write called with non-bytes.")
        existed = p.exists()
        if existed and not overwrite:
            return ToolResult.failure(f"{p} already exists. Set overwrite=true to replace it.")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(bytes(payload))
        except OSError as exc:
            return ToolResult.failure(f"Could not write {p}: {exc}", error=str(exc))
        verb = "Updated" if existed else "Created"
        return ToolResult.success(f"{verb} {p} ({len(payload)} bytes).", data=str(p))

    def read_bytes(self, path: str, max_bytes: int = _MAX_BINARY_READ_BYTES) -> ToolResult:
        """Read raw bytes under the same confinement as ``read``, for reopening a generated document.

        Also not exposed as a tool: it backs ``inspect_document``, which reports structure rather than
        handing bytes to the model.
        """
        try:
            p = self._confine(path)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not p.is_file():
            return ToolResult.failure(f"Not a file: {p}")
        try:
            data = p.read_bytes()[:max_bytes]
        except OSError as exc:
            return ToolResult.failure(f"Could not read {p}: {exc}", error=str(exc))
        return ToolResult.success(f"Read {len(data)} bytes from {p}.", data=data)

    def delete(self, path: str) -> ToolResult:
        """Delete a file by sending it to the Recycle Bin (recoverable)."""
        try:
            p = self._confine(path, write=True)
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

    def stat(self, path: str) -> ToolResult:
        """Describe a file or folder without reading its contents: size, dates, kind, read-only flag.

        Read-only and content-free, which is the point: "when did I last touch this?" and "how big is
        it?" are answerable without a single byte of the file entering V.O.I.D or a model's context.
        """
        try:
            p = self._confine(path)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not p.exists():
            return ToolResult.failure(f"Nothing exists at {p}.")
        try:
            info = p.stat()
        except OSError as exc:
            return ToolResult.failure(f"Could not read information about {p}: {exc}", error=str(exc))
        is_dir = p.is_dir()
        data = {"path": str(p), "name": p.name,
                "kind": "folder" if is_dir else "file",
                "size_bytes": None if is_dir else info.st_size,
                "modified": _when(info.st_mtime),
                "created": _when(info.st_ctime),
                "accessed": _when(info.st_atime),
                "read_only": not os.access(p, os.W_OK),
                "is_link": self._is_reparse_point(p),
                "suffix": p.suffix.lower() or None}
        if is_dir:
            # A count, not a listing: "how many things are in here" without enumerating names.
            try:
                data["entries"] = sum(1 for _ in p.iterdir())
            except OSError:
                data["entries"] = None
            summary = (f"{p.name} is a folder, last modified {data['modified']}"
                       + (f", containing {data['entries']} item(s)."
                          if data["entries"] is not None else "."))
        else:
            summary = (f"{p.name} is a {_size(info.st_size)} file, last modified "
                       f"{data['modified']}.")
        if data["read_only"]:
            summary += " It is read-only."
        return ToolResult.success(summary, data=data)

    def copy(self, source: str, destination: str, overwrite: bool = False) -> ToolResult:
        """Copy a file. Both ends are confined separately, so neither can reach outside the allowed roots.

        Confining the destination with ``write=True`` is what stops a copy being used as a write
        primitive against a location that ``write_file`` itself would refuse.
        """
        return self._transfer(source, destination, overwrite, move=False)

    def move(self, source: str, destination: str, overwrite: bool = False) -> ToolResult:
        """Move or rename a file. Both ends are confined; the source is confined for writing too,
        because a move removes it."""
        return self._transfer(source, destination, overwrite, move=True)

    def _transfer(self, source: str, destination: str, overwrite: bool, *, move: bool) -> ToolResult:
        verb = "move" if move else "copy"
        try:
            # A move deletes the source, so the source needs write authority for a move and only read
            # authority for a copy. The destination always needs write authority.
            src = self._confine(source, write=move)
            dst = self._confine(destination, write=True)
        except PathNotAllowed as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not src.exists():
            return ToolResult.failure(f"Nothing to {verb} at {src}.")
        if src.is_dir():
            # Recursive directory transfer multiplies the consequences of one mistaken argument, and
            # nothing in V2 needs it. Deliberately out of scope rather than quietly supported.
            return ToolResult.failure(
                f"I can only {verb} files, not folders. Name a file instead.")
        if self._is_reparse_point(src):
            return ToolResult.failure(
                f"{src} is a link, and I do not {verb} links - the result would be ambiguous.")
        # A destination naming a directory means "into that directory", which is what a person means by
        # "move this into Archive"; resolving it here keeps the confinement check above authoritative.
        if dst.is_dir():
            dst = dst / src.name
            try:
                dst = self._confine(str(dst), write=True)
            except PathNotAllowed as exc:
                return ToolResult.failure(str(exc), error=str(exc))
        if dst == src:
            return ToolResult.failure(f"The source and destination are the same file ({src}).")
        if dst.exists() and not overwrite:
            return ToolResult.failure(
                f"{dst} already exists. Set overwrite=true to replace it.")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if move:
                # replace() is atomic on the same volume and overwrites; across volumes it raises, and
                # shutil.move handles that case. Only reached when overwrite was authorised above.
                try:
                    src.replace(dst)
                except OSError:
                    shutil.move(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
        except OSError as exc:
            return ToolResult.failure(f"Could not {verb} {src} to {dst}: {exc}", error=str(exc))
        past = "Moved" if move else "Copied"
        return ToolResult.success(f"{past} {src.name} to {dst}.", data={"source": str(src),
                                                                       "destination": str(dst)})

    # --- dynamic risk --------------------------------------------------

    def _transfer_risk(self, arguments: dict) -> RiskLevel:
        """A transfer that would replace an existing file is HIGH; otherwise MEDIUM. Fail safe to HIGH.

        Same reasoning as ``_write_risk``: creating something new is recoverable, destroying something
        that was already there is not, so only the second asks the owner.
        """
        p = _risk_path(arguments.get("destination"))
        if p is None:
            return RiskLevel.HIGH
        try:
            if p.is_dir():
                source = _risk_path(arguments.get("source"))
                if source is None:
                    return RiskLevel.HIGH
                p = p / source.name
            return RiskLevel.HIGH if p.exists() else RiskLevel.MEDIUM
        except Exception:                                      # noqa: BLE001
            return RiskLevel.HIGH

    def _write_risk(self, arguments: dict) -> RiskLevel:
        """Modifying/overwriting an EXISTING file is HIGH (needs confirmation);
        creating a new file is MEDIUM. Fail safe to HIGH on any doubt.
        """
        p = _risk_path(arguments.get("path"))
        if p is None:
            return RiskLevel.HIGH
        try:
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
                name="find_directory",
                description=(
                    "Find FOLDERS by name anywhere under the allowed roots "
                    "(use this to locate a directory such as 'Hackathon' before "
                    "opening it or writing inside it). Matches the directory "
                    "name exactly (case-insensitive), or as a glob if the query "
                    "contains * ? or []. It never returns files and never picks "
                    "a winner: if several folders match, ALL are returned and "
                    "you must ask the owner which one. When the owner names a "
                    "parent location (e.g. 'Projects on my OneDrive Desktop' or "
                    "'Projects inside StudioVerse'), pass that hint as 'context' "
                    "to narrow the search - the engine filters candidates by it; "
                    "it will still be ambiguous if several remain."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "Exact folder name or glob."},
                        "root": {"type": "string",
                                 "description": ("Optional directory to search "
                                                 "under (must be allowed).")},
                        "context": {"type": "string",
                                    "description": ("Optional parent-location hint "
                                                    "from the request, e.g. "
                                                    "'OneDrive Desktop'.")},
                        "max_results": {"type": "integer",
                                        "description": "Max matches (default 25)."},
                    },
                    "required": ["query"],
                },
                handler=self.find_dir,
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
            Tool(
                name="get_file_info",
                description=(
                    "Describe a file or folder without opening it: size, when it was last modified, "
                    "whether it is read-only, and for a folder how many items it holds. Use this for "
                    "'how big is this?' or 'when did I last change this?' - it reads no contents."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File or folder to describe."},
                    },
                    "required": ["path"],
                },
                handler=self.stat,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="copy_file",
                description=(
                    "Copy a file to another location inside the allowed roots. The destination may be a "
                    "folder, meaning 'into that folder'. Copies files only, not folders. Replacing an "
                    "existing file requires overwrite=true and the owner's confirmation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "File to copy."},
                        "destination": {"type": "string",
                                        "description": "New path, or a folder to copy into."},
                        "overwrite": {"type": "boolean",
                                      "description": "Replace the destination if it exists."},
                    },
                    "required": ["source", "destination"],
                },
                handler=self.copy,
                # MEDIUM for a new file, HIGH when it would replace one. See _transfer_risk.
                risk=RiskLevel.MEDIUM,
                risk_fn=self._transfer_risk,
            ),
            Tool(
                name="move_file",
                description=(
                    "Move or rename a file inside the allowed roots. The destination may be a folder, "
                    "meaning 'into that folder'. Moves files only, not folders. Replacing an existing "
                    "file requires overwrite=true and the owner's confirmation."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "File to move."},
                        "destination": {"type": "string",
                                        "description": "New path, or a folder to move into."},
                        "overwrite": {"type": "boolean",
                                      "description": "Replace the destination if it exists."},
                    },
                    "required": ["source", "destination"],
                },
                handler=self.move,
                risk=RiskLevel.MEDIUM,
                risk_fn=self._transfer_risk,
            ),
        ]
