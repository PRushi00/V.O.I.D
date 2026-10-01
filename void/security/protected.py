"""Engine-protected paths (P1 T1.1): V.O.I.D's own secrets and state are unreachable by any file tool.

The file tools are confined to ``security.allowed_roots`` (owner config). On the owner's machine that root is ``C:\\``,
which includes V.O.I.D's own state directory (device keys, pairing window, memory database, task store), the config
that holds the security settings, the OS credential stores and the browser profiles. Nothing a model, a tool result, a
memory or a request argument says may reach them, so this set is fixed BY THE ENGINE:

  * it is computed from code and the environment, never from model-controlled data;
  * ``security.protected_roots`` (owner config) can only ADD to it - there is no API that removes an entry;
  * it is enforced in ``FileActions._confine`` (every file tool and ``open_path`` for local paths funnels through it),
    and search / list / find omit its entries, so it does not depend on the prompt.

Two independent layers, both fail-closed (any error => deny):

  1. canonical  - reject UNC / device / extended-prefix forms that do not normalise to a local drive path and alternate
                  data streams; strip trailing dots/spaces Windows ignores; ``Path.resolve()`` (follows symlinks and
                  junctions); case-insensitive containment.
  2. identity   - walk the candidate and its nearest existing ancestors and compare the OS file identity
                  (device, index) with every protected root / file. This catches what a name comparison cannot:
                  hard links, junctions to a new path, 8.3 short names, admin shares.

Write/delete-deny roots (Windows, Program Files, ProgramData, V.O.I.D's own package) are refused for mutation only.
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterable

_WIN = sys.platform.startswith("win")
_DRIVE = re.compile(r"^[A-Za-z]:")
_EXTENDED_LOCAL = re.compile(r"^[A-Za-z]:[\\/]")
_SHARED_PROFILES = {"public", "default", "default user", "all users"}
_HARDLINK_SCAN_BUDGET = 5000      # entries walked to look for a hard link into a small secret directory
_BS = chr(92)                                            # a backslash, spelled without an escape sequence
_EXT_PREFIXES = (_BS * 2 + "?" + _BS, "//?/")
_UNC_PREFIXES = (_BS * 2, "//")


def _env(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v) if v else None


def _strip_component_tails(s: str) -> str:
    """Windows ignores trailing dots and spaces in a path component: ``State.\\key`` names ``State\\key``."""
    parts = re.split(r"[\\/]+", s)
    out = []
    for i, p in enumerate(parts):
        if p in ("", ".", "..") or (i == 0 and _DRIVE.match(p)):
            out.append(p)
        else:
            out.append(p.rstrip(". ") or p)
    return os.sep.join(out)


class EngineProtected:
    """Add-only set of protected paths. Construct with :meth:`default`; extend with :meth:`with_extra`."""

    def __init__(self, protected: Iterable[Path | str] = (), write_denied: Iterable[Path | str] = (),
                 large: Iterable[Path | str] = ()):
        self._prot = self._prep(protected)
        self._wdeny = self._prep(write_denied)
        # Huge protected trees (browser profiles, other users) are not scanned for hard links; they stay protected
        # by name and by directory identity.
        self._large = self._prep(large)

    @staticmethod
    def _prep(paths: Iterable[Path | str]) -> list[Path]:
        out: list[Path] = []
        for p in paths:
            try:
                out.append(Path(p).resolve())
            except (OSError, ValueError, RuntimeError):
                out.append(Path(os.path.abspath(str(p))))
        return out

    # --- construction --------------------------------------------------
    @classmethod
    def default(cls, state_dir: Path | str | None = None) -> "EngineProtected":
        from void.config import _CONFIG_DIR, _PKG_DIR
        home = Path(os.path.expanduser("~"))
        prot: list[Path] = [home / ".void", _CONFIG_DIR]                # state dir (default) + repo config incl. local_config.yaml
        if state_dir is not None:
            prot.append(Path(state_dir))
        prot += [home / ".ssh", home / ".aws", home / ".gnupg",         # other secret stores under the profile
                 home / ".omniroute"]                                    # OmniRoute keeps provider keys/tokens here
        roaming, local = _env("APPDATA"), _env("LOCALAPPDATA")
        for base in (roaming, local):                                   # DPAPI master keys, credential blobs, vaults
            if base:
                prot += [base / "Microsoft" / "Credentials", base / "Microsoft" / "Protect", base / "Microsoft" / "Vault"]
        if local:                                                       # browser profile stores (cookies, saved logins)
            prot += [local / "Google" / "Chrome" / "User Data", local / "Microsoft" / "Edge" / "User Data",
                     local / "BraveSoftware", local / "Opera Software"]
        if roaming:
            prot += [roaming / "Opera Software", roaming / "Mozilla" / "Firefox"]
        users_dir = home.parent
        if _WIN and users_dir.name.lower() == "users":                  # other people's profiles
            try:
                for child in users_dir.iterdir():
                    if child.is_dir() and child.name.lower() != home.name.lower() and child.name.lower() not in _SHARED_PROFILES:
                        prot.append(child)
            except OSError:
                pass
        large = [local / "Google" / "Chrome" / "User Data" if local else None,
                 local / "Microsoft" / "Edge" / "User Data" if local else None,
                 local / "BraveSoftware" if local else None, local / "Opera Software" if local else None,
                 roaming / "Opera Software" if roaming else None, roaming / "Mozilla" / "Firefox" if roaming else None]
        large += [c for c in prot if c.parent == users_dir and c.name.lower() != home.name.lower()]
        wdeny: list[Path] = [_PKG_DIR]                                   # V.O.I.D's own code: no self-modification through tools
        for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            p = _env(name)
            if p:
                wdeny.append(p)
        return cls(prot, wdeny, [p for p in large if p is not None])

    def with_extra(self, *paths: Path | str) -> "EngineProtected":
        """A NEW set with additional protected paths. There is deliberately no removal API."""
        merged = EngineProtected()
        merged._prot = [*self._prot, *self._prep(paths)]
        merged._wdeny = list(self._wdeny)
        merged._large = list(self._large)
        return merged

    @property
    def roots(self) -> list[Path]:
        return list(self._prot)

    # --- checks ----------------------------------------------------------
    @staticmethod
    def _within(path: Path, root: Path) -> bool:
        try:
            return path == root or path.is_relative_to(root)
        except ValueError:
            return False

    def covers(self, path: Path, *, write: bool = False) -> bool:
        """Fast lexical layer (no I/O beyond ``resolve``): for walks that filter directory entries."""
        try:
            p = Path(path).resolve()
        except (OSError, ValueError, RuntimeError):
            return True
        if any(self._within(p, r) for r in self._prot):
            return True
        return write and any(self._within(p, r) for r in self._wdeny)

    def denies_launch(self, target: object) -> str | None:
        """None if a program at ``target`` may be launched, else a short reason. A program stored INSIDE V.O.I.D's own
        state or a credential store is never started. Browser and other-user trees are not consulted here: installed
        programs legitimately live under per-user browser folders (they stay protected for READING via ``denies``)."""
        try:
            if not isinstance(target, str) or not target.strip() or "\x00" in target:
                return "invalid target"
            resolved = Path(os.path.expanduser(target.strip())).resolve()
            large = {os.path.normcase(str(x)) for x in self._large}
            for r in self._prot:
                if os.path.normcase(str(r)) not in large and self._within(resolved, r):
                    return "protected location"
            return None
        except Exception:                              # noqa: BLE001 - anything unexpected must deny
            return "unverifiable target"

    def _identity_ids(self) -> set[tuple[int, int]]:
        ids: set[tuple[int, int]] = set()
        for r in self._prot:
            try:
                st = os.stat(r)
            except OSError:
                continue
            if st.st_ino:
                ids.add((st.st_dev, st.st_ino))
        return ids

    def _identity(self, p: Path) -> bool:
        ids = self._identity_ids()
        if not ids:
            return False
        for x in [p, *p.parents]:
            try:
                st = os.stat(x)                        # follows junctions/symlinks: identity of the TARGET
            except OSError:
                continue                               # not there (yet): the nearest EXISTING ancestor decides
            if st.st_ino and (st.st_dev, st.st_ino) in ids:
                return True
        return self._is_hardlink_into_protected(p)

    def _is_hardlink_into_protected(self, p: Path) -> bool:
        """A pre-existing hard link is a second NAME for a protected file that name checks cannot see. Only an existing
        regular file with more than one link can be one, so only then look for its twin inside the small protected
        directories. If the scan budget runs out the answer is 'yes' (fail closed)."""
        try:
            st = os.stat(p)
        except OSError:
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_nlink <= 1 or not st.st_ino:
            return False
        want = (st.st_dev, st.st_ino)
        large = {os.path.normcase(str(x)) for x in self._large}
        seen = 0
        for root in self._prot:
            if os.path.normcase(str(root)) in large:
                continue
            try:
                rst = os.stat(root)
            except OSError:
                continue
            if stat.S_ISREG(rst.st_mode):
                if (rst.st_dev, rst.st_ino) == want:
                    return True
                continue
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                for name in filenames:
                    seen += 1
                    if seen > _HARDLINK_SCAN_BUDGET:
                        return True
                    try:
                        fst = os.stat(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    if (fst.st_dev, fst.st_ino) == want:
                        return True
        return False

    def denies(self, raw: object, *, write: bool = False) -> str | None:
        """None if ``raw`` may proceed to the normal allowed-roots check, else a short reason. Fail-closed."""
        try:
            if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
                return "invalid path"
            s = os.path.expanduser(raw.strip())
            if _WIN:
                if s.startswith(_EXT_PREFIXES):
                    rest = s[4:]
                    if not _EXTENDED_LOCAL.match(rest):
                        return "network or device path"
                    s = rest
                elif s.startswith(_UNC_PREFIXES):
                    return "network or device path"
                tail = s[2:] if _DRIVE.match(s) else s
                if ":" in tail:
                    return "alternate data stream"
            for candidate in dict.fromkeys((s, _strip_component_tails(s))):
                resolved = Path(candidate).resolve()
                if any(self._within(resolved, r) for r in self._prot):
                    return "protected location"
                if write and any(self._within(resolved, r) for r in self._wdeny):
                    return "protected for changes"
                if self._identity(resolved):
                    return "protected location"
            return None
        except Exception:                              # noqa: BLE001 - anything unexpected must deny
            return "unverifiable path"
