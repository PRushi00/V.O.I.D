"""The application registry: what is installed, what is running, what the owner prefers.

V2 already discovers installed applications - ``void.actions.computer.AppCatalog`` has the identity model,
the Start-Menu/App-Paths/PATH discovery, the match tiers and the TTL refresh that were built and measured
for V2. This module does **not** discover anything again. It joins that catalog with observed runtime state
and the owner's stated preferences, because the five things the blueprint asks V.O.I.D to distinguish are
not all the same question:

    installed          the catalog knows this
    currently running  only the window/process list knows this
    previously seen    only a record over time knows this
    preferred          only the owner's configuration knows this
    available          installed AND the machinery to launch it is present

Keeping them apart is what lets the route resolver prefer "activate what is already open" over "launch
another copy", and lets "what browser does the owner prefer?" be answered from policy data rather than from
a sentence in a prompt.

**Identity, not names.** An application's identity is the catalog's ``app_id``. A display name is how the
owner refers to it and how a window labels itself - useful for matching, unreliable as identity, and
attacker-influenced in the case of a window title. The registry matches on names but *keys* on ids.

**It observes; it does not act.** There is no launch here. A registry entry describes a launch route; the
route resolver turns that into a route, and the existing ``launch_app`` tool executes it through the usual
funnel. Nothing in this module can start a program.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

#: How long observed runtime state is trusted before it is read again. Short: an application the owner
#: closed two minutes ago must not still look running, because that would make the resolver choose
#: "activate the existing window" for a window that is gone.
RUNNING_TTL_S = 5.0

#: Cap on how many applications the registry will describe in one answer.
MAX_APPLICATIONS = 60

#: Preference keys the registry understands. A closed set: an unrecognised preference is ignored rather
#: than becoming a free-form channel from config into routing decisions.
PREFERENCE_KEYS = frozenset({"browser", "editor", "terminal", "music", "mail", "messaging"})


@dataclass
class Application:
    """One application, as V.O.I.D knows it."""

    #: Stable identity from the catalog. The key for everything.
    app_id: str
    name: str
    #: How it would be launched: the catalog's kind ("exe" / "lnk" / "uwp").
    launch_kind: str = ""
    installed: bool = True
    running: bool = False
    #: When V.O.I.D last saw it running. None means never observed running.
    last_running_at: float | None = None
    #: True when this is the owner's preferred application for some role, and which role.
    preferred_for: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        """Installed, with a usable launch route. "Available" is not the same as "installed"."""
        return bool(self.installed and self.launch_kind)

    def as_dict(self) -> dict:
        out = {"app_id": self.app_id, "name": self.name, "installed": self.installed,
               "running": self.running, "available": self.available,
               "launch_kind": self.launch_kind}
        if self.last_running_at is not None:
            out["last_running_at"] = round(self.last_running_at, 1)
        if self.preferred_for:
            out["preferred_for"] = list(self.preferred_for)
        return out


class ApplicationRegistry:
    """Installed state from the catalog, running state from observation, preference from config.

    All three sources are injected. The registry owns the *join* and the freshness policy, not the data -
    which is why it adds no discovery mechanism and cannot drift from what the catalog believes.

    ``catalog_entries`` returns objects with ``app_id``, ``name``, ``kind``;
    ``running_apps`` returns ``[{"name": str, "pid": int}, ...]`` from the existing window layer;
    ``preferences`` is the owner's config mapping, e.g. ``{"browser": "opera gx"}``.
    """

    def __init__(self, *, catalog_entries: Callable[[], Iterable] | None = None,
                 running_apps: Callable[[], list[dict]] | None = None,
                 preferences: dict | None = None,
                 running_ttl_s: float = RUNNING_TTL_S,
                 now: Callable[[], float] | None = None):
        self._catalog_entries = catalog_entries
        self._running_apps = running_apps
        self._preferences = {k: v for k, v in (preferences or {}).items() if k in PREFERENCE_KEYS}
        self._ttl = max(0.0, float(running_ttl_s))
        self._now = now or time.monotonic
        self._running: set[str] = set()
        self._running_read_at: float | None = None
        #: app_id -> last time it was observed running. The "previously observed" dimension.
        self._seen_running: dict[str, float] = {}

    # -- preferences, as data --
    @property
    def preferences(self) -> dict:
        return dict(self._preferences)

    def preferred(self, role: str) -> str | None:
        """The owner's preferred application for a role, lower-cased, or None.

        Policy data, deliberately not prompt text: the blueprint is explicit that "preferred browser =
        Opera GX" belongs in configuration where routing can read it, not in a sentence a model may ignore.
        """
        value = self._preferences.get((role or "").strip().lower())
        return value.strip().lower() if isinstance(value, str) and value.strip() else None

    # -- observed runtime state --
    def _refresh_running(self, force: bool = False) -> None:
        """Re-read the running set when the cached answer is older than the TTL."""
        if self._running_apps is None:
            return
        now = self._now()
        if (not force and self._running_read_at is not None
                and now - self._running_read_at < self._ttl):
            return
        try:
            observed = self._running_apps() or []
        except Exception:                                      # noqa: BLE001 - stale beats crashing
            return
        names = set()
        for row in observed:
            name = (row.get("name") if isinstance(row, dict) else None) or ""
            name = str(name).strip().lower()
            if name:
                names.add(name)
        self._running = names
        self._running_read_at = now

    def refresh(self) -> None:
        """Force a re-read of runtime state. The blueprint's "do not assume knowledge is permanent"."""
        self._refresh_running(force=True)

    # -- the join --
    def applications(self, limit: int = MAX_APPLICATIONS) -> list[Application]:
        """Every known application, with installed/running/preferred resolved."""
        self._refresh_running()
        entries = []
        if self._catalog_entries is not None:
            try:
                entries = list(self._catalog_entries() or [])
            except Exception:                                  # noqa: BLE001
                entries = []
        out: list[Application] = []
        for entry in entries[:max(1, int(limit))]:
            app_id = str(getattr(entry, "app_id", "") or "")
            name = str(getattr(entry, "name", "") or "")
            if not app_id or not name:
                continue
            running = self._looks_running(name)
            if running:
                self._seen_running[app_id] = time.time()
            out.append(Application(
                app_id=app_id, name=name,
                launch_kind=str(getattr(entry, "kind", "") or ""),
                installed=True, running=running,
                last_running_at=self._seen_running.get(app_id),
                preferred_for=self._roles_for(name)))
        return out

    def _looks_running(self, name: str) -> bool:
        """Is something matching this display name running?

        Substring both ways, because a catalog name ("Opera GX Browser") and a process name ("opera.exe")
        rarely match exactly. Deliberately permissive: a false positive makes the resolver prefer
        "activate" and the activation then fails verification, which is recoverable; a false negative
        launches a second copy, which is the waste this is meant to prevent.
        """
        needle = (name or "").strip().lower()
        if not needle:
            return False
        stem = needle.split()[0] if needle.split() else needle
        for observed in self._running:
            base = observed[:-4] if observed.endswith(".exe") else observed
            if needle == observed or needle == base:
                return True
            if len(stem) >= 4 and (stem in base or base in stem):
                return True
        return False

    def _roles_for(self, name: str) -> tuple[str, ...]:
        low = (name or "").strip().lower()
        return tuple(sorted(role for role, wanted in self._preferences.items()
                            if isinstance(wanted, str) and wanted.strip()
                            and wanted.strip().lower() in low))

    # -- the questions the blueprint asks the registry to answer --
    def find(self, name: str) -> Application | None:
        """The best match for a display name, preferring an exact one.

        Name matching only - no launching, and no catalog mutation. The caller gets an ``app_id`` and can
        hand that to the existing launch capability.
        """
        needle = (name or "").strip().lower()
        if not needle:
            return None
        applications = self.applications()
        for application in applications:
            if application.name.strip().lower() == needle:
                return application
        for application in applications:
            if needle in application.name.strip().lower():
                return application
        return None

    def is_installed(self, name: str) -> bool:
        application = self.find(name)
        return bool(application and application.installed)

    def is_running(self, name: str) -> bool:
        self._refresh_running()
        application = self.find(name)
        if application is not None:
            return application.running
        # Not in the catalog but possibly running anyway (a portable binary, something launched by hand).
        return self._looks_running(name)

    def launch_route(self, name: str) -> dict | None:
        """How this application would be launched: its identity and kind, never a raw path.

        A path is deliberately not returned. The launch capability resolves an ``app_id`` against the
        catalog itself, which is what keeps a caller-supplied path from ever becoming an execution target.
        """
        application = self.find(name)
        if application is None or not application.available:
            return None
        return {"app_id": application.app_id, "name": application.name,
                "kind": application.launch_kind, "running": application.running}

    def running(self) -> list[Application]:
        return [application for application in self.applications() if application.running]

    def snapshot(self) -> dict:
        """Counts and preferences, for an event or a status answer. No application names."""
        applications = self.applications()
        return {"installed": len(applications),
                "running": sum(1 for application in applications if application.running),
                "available": sum(1 for application in applications if application.available),
                "preferences": sorted(self._preferences),
                "observed_running_names": len(self._running)}
