"""The route model and resolver: *how* V.O.I.D should reach a goal.

A **route** is an inspectable, scored, executable way of accomplishing something. The resolver's job is to
turn one goal into candidate routes and choose among them. That choice is what the blueprint calls the most
important V3 component, and the reason is a concrete failure it prevents:

    "Open Gmail"  ->  launch a browser -> search Google -> search Gmail -> click through

when the owner already had Gmail open in the browser they prefer. The shortest reliable route was "activate
that tab", and nothing in V2 could express or prefer it.

So routes are explicit objects rather than branches in a function. Each one says what it needs, how
reliable it is, roughly what it costs, what it would execute, and whether it reuses state that already
exists. Scoring is a pure function over those facts, which makes "why did it pick that?" answerable and
testable instead of a matter of reading control flow.

**Route kinds, in the blueprint's preference order.** This ordering is the policy, and it is deliberate:
structured state beats structured interfaces, which beat automating a UI, which beats looking at pixels.
Pixels are last because they are the least reliable and the most privacy-costly, not because they are
unfashionable.

**A route is a proposal, never an authorization.** ``Route.calls`` are tool calls with engine-chosen
arguments, executed through ``Agent.run_direct`` / ``_run_call`` exactly as the V2 fast path's are: kill
switch, then the tool's own effective risk, then ``RiskGate``. A route carries a ``risk`` field for
*scoring* - so a cheap safe route is preferred over an expensive dangerous one - and that field can never
lower what the gate asks. A resolver that scored a HIGH-risk route first would still get a confirmation
prompt.

**No caller text becomes an argument.** A route's arguments come from the registry, the catalog or a fixed
alias map. The owner's words select a route; they never travel into one. This is the same rule that keeps
the V2 fast path from turning speech into a command line.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

from void.core.fast_path import DirectCall
from void.security.risk import RiskLevel


class RouteKind:
    """How a route does its work, in the order the blueprint prefers.

    ``rank`` is the preference: lower is better. It encodes "prefer the shortest reliable path" as data, so
    the resolver does not need to know anything about individual routes to order them sensibly.
    """

    #: The goal is already satisfied, or nearly so, by state that exists right now - a tab already open, an
    #: application already running. Always preferred: it is the fastest and the least disruptive.
    EXISTING_STATE = "existing_state"
    #: A structured interface: an API, an SDK, an MCP tool. No UI involved, so nothing to misread.
    STRUCTURED = "structured"
    #: Launching or driving a native application through its own supported entry point.
    NATIVE_APP = "native_app"
    #: Structured browser automation - DOM and accessibility, not pixels.
    BROWSER = "browser"
    #: Windows UI Automation against a native application's control tree.
    DESKTOP_UI = "desktop_ui"
    #: Reading the screen: OCR or a vision model. Expensive, and the owner's screen is sensitive.
    VISION = "vision"
    #: Raw keyboard/pointer at coordinates. Last resort: brittle and unverifiable.
    INPUT_FALLBACK = "input_fallback"

    _ORDER = (EXISTING_STATE, STRUCTURED, NATIVE_APP, BROWSER, DESKTOP_UI, VISION, INPUT_FALLBACK)
    ALL = frozenset(_ORDER)

    @classmethod
    def rank(cls, kind: str) -> int:
        """Preference rank; unknown kinds sort last rather than raising."""
        try:
            return cls._ORDER.index(kind)
        except ValueError:
            return len(cls._ORDER)


@dataclass(frozen=True)
class Route:
    """One way to accomplish a goal, with everything needed to compare it against another.

    Frozen, because a route is a candidate under consideration: scoring must not be able to mutate what it
    is scoring, and a route handed to an executor must be the one that was chosen.
    """

    kind: str
    #: What this route achieves, in words safe to speak. Not the owner's phrasing - V.O.I.D's description.
    describes: str
    #: The tool calls, in order, with ENGINE-chosen arguments. Empty for a route that only answers.
    calls: tuple[DirectCall, ...] = ()
    #: Stable id, minted here so an event or a log line can refer to a specific candidate.
    id: str = field(default_factory=lambda: "route-" + uuid.uuid4().hex[:8])
    #: Does this reuse state that already exists rather than creating more?
    reuses_existing: bool = False
    #: Capability names this route needs present (e.g. "browser", "desktop_ui"). Used to drop routes whose
    #: machinery is not installed, so an unavailable adapter is never selected and then discovered missing.
    requires: frozenset[str] = frozenset()
    #: 0..1, how often this kind of route is expected to work. An estimate, used only for ordering.
    reliability: float = 0.5
    #: Rough seconds. An estimate for ordering, not a deadline.
    latency_s: float = 1.0
    #: The highest risk among this route's calls, for SCORING only. RiskGate still decides at execution.
    risk: RiskLevel = RiskLevel.LOW
    #: One line on why this route exists as a candidate.
    why: str = ""

    def __post_init__(self) -> None:
        if self.kind not in RouteKind.ALL:
            raise ValueError(f"unknown route kind: {self.kind!r}")
        object.__setattr__(self, "reliability", min(1.0, max(0.0, float(self.reliability))))
        object.__setattr__(self, "latency_s", max(0.0, float(self.latency_s)))

    @property
    def rank(self) -> int:
        return RouteKind.rank(self.kind)

    @property
    def executable(self) -> bool:
        return bool(self.calls)

    def as_dict(self) -> dict:
        """Inspectable form, for an event or an explanation. Names and numbers; never arguments."""
        return {"id": self.id, "kind": self.kind, "describes": self.describes,
                "calls": [call.name for call in self.calls],
                "reuses_existing": self.reuses_existing,
                "requires": sorted(self.requires),
                "reliability": round(self.reliability, 2),
                "latency_s": round(self.latency_s, 2),
                "risk": self.risk.name, "why": self.why}


def score(route: Route) -> tuple:
    """A sort key: lower is better. Pure, total, and the whole of the preference policy.

    The ordering, most significant first:

    1. **Reuse existing state.** Activating the tab the owner already has open is better than opening
       another one, every time. This is a separate and stronger signal than the kind ranking because a
       browser route that reuses a session should beat a native route that starts something new.
    2. **Route kind.** The blueprint's ladder: structured state, structured interfaces, native app,
       browser, desktop UI, vision, raw input.
    3. **Lower risk.** Among equally direct routes, prefer the one that needs less authority. This never
       *grants* anything - it means a route that would interrupt the owner for confirmation loses to one
       that would not, which is a usability preference, not a security decision.
    4. **Higher reliability**, then **lower latency**. Correctness before speed.
    5. **Fewer calls**, then **id**, so the order is total and stable across runs.
    """
    return (0 if route.reuses_existing else 1,
            route.rank,
            int(route.risk),
            -route.reliability,
            route.latency_s,
            len(route.calls),
            route.id)


class RouteProvider(Protocol):
    """Something that can propose routes for a goal.

    Deliberately a Protocol rather than a base class: the V2 ``FastPath`` becomes a provider through a thin
    adapter without inheriting anything, and a test provider is three lines. A provider that raises is
    isolated by the resolver - one broken source must not prevent the others from answering.
    """

    name: str

    def propose(self, goal: str, state: "WorldState") -> Iterable[Route]:
        ...


@dataclass
class WorldState:
    """What V.O.I.D has observed about the computer, as the resolver needs it.

    Deliberately small and flat. This is not a model of the machine - it is the handful of facts that
    actually change which route wins, gathered from the registries rather than guessed. Anything a provider
    needs beyond this it reads itself (and pays for itself).

    Everything here is OBSERVED, never asserted by a model: ``running_apps`` comes from the window list,
    ``open_tabs`` from the browser adapter, ``preferences`` from the owner's config.
    """

    #: Lower-cased process/application names observed running right now.
    running_apps: frozenset[str] = frozenset()
    #: Open browser tabs as (browser, url, title) - whatever the browser layer could see.
    open_tabs: tuple[tuple[str, str, str], ...] = ()
    #: Capability names currently available, e.g. {"native_app", "browser"}. A route requiring something
    #: absent is dropped rather than attempted.
    capabilities: frozenset[str] = frozenset()
    #: Owner preferences as data, NOT prompt text: {"browser": "opera gx"}. Policy, not persuasion.
    preferences: dict = field(default_factory=dict)
    observed_at: float = field(default_factory=time.time)

    def is_running(self, name: str) -> bool:
        needle = (name or "").strip().lower()
        if not needle:
            return False
        return any(needle == app or needle in app for app in self.running_apps)

    def tabs_matching(self, needle: str) -> tuple[tuple[str, str, str], ...]:
        """Open tabs whose url or title contains ``needle``. Case-insensitive, substring.

        Substring on purpose: "gmail" should match ``mail.google.com`` only if that string appears, so this
        answers "is there a tab that looks like this?" without pretending to understand sites.
        """
        probe = (needle or "").strip().lower()
        if not probe:
            return ()
        return tuple(tab for tab in self.open_tabs
                     if probe in (tab[1] or "").lower() or probe in (tab[2] or "").lower())

    def preferred(self, key: str) -> str | None:
        value = self.preferences.get(key)
        return value.strip().lower() if isinstance(value, str) and value.strip() else None


@dataclass(frozen=True)
class Resolution:
    """What the resolver decided, and what it considered.

    ``candidates`` is kept so the decision is explainable after the fact and so a UI can offer the
    alternatives when the choice was close or ambiguous.
    """

    selected: Route | None
    candidates: tuple[Route, ...] = ()
    #: True when two or more candidates are equally good AND materially different, so the owner should
    #: choose. The resolver does not guess in that case.
    ambiguous: bool = False
    why: str = ""

    @property
    def resolved(self) -> bool:
        return self.selected is not None

    def as_dict(self) -> dict:
        return {"selected": self.selected.as_dict() if self.selected else None,
                "candidates": [route.as_dict() for route in self.candidates],
                "ambiguous": self.ambiguous, "why": self.why}


class RouteResolver:
    """Goal -> candidate routes -> selected route.

    Holds providers, not capabilities: it never executes anything and never talks to a model. Given a goal
    and observed state it asks each provider what it could do, drops routes whose machinery is absent,
    scores what remains, and returns the winner plus the field.

    Ambiguity is reported rather than resolved. Two routes that score identically and differ in *kind* mean
    V.O.I.D genuinely does not know which the owner wants - the blueprint says ask, and that is what
    ``Resolution.ambiguous`` is for. Two routes that score identically and are the same kind are not
    ambiguous in any way the owner would care about, so the stable tie-break just picks one.
    """

    def __init__(self, providers: Iterable[RouteProvider] = (),
                 on_audit: Callable[[str], None] | None = None):
        self._providers = list(providers)
        self._audit = on_audit or (lambda line: None)

    def add(self, provider: RouteProvider) -> None:
        self._providers.append(provider)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(getattr(p, "name", type(p).__name__) for p in self._providers)

    def candidates(self, goal: str, state: WorldState | None = None) -> list[Route]:
        """Every route any provider proposes that this machine can actually run, best first."""
        state = state or WorldState()
        found: list[Route] = []
        for provider in self._providers:
            name = getattr(provider, "name", type(provider).__name__)
            try:
                proposed = list(provider.propose(goal, state) or ())
            except Exception as exc:                            # noqa: BLE001
                # One broken provider must not stop the others from answering.
                self._audit(f"ROUTE_PROVIDER_FAILED {name} ({type(exc).__name__})")
                continue
            for route in proposed:
                if not isinstance(route, Route):
                    continue
                missing = route.requires - state.capabilities
                if missing:
                    # Dropped BEFORE selection: a route whose adapter is not installed must never be
                    # chosen and then fail at execution, which would look like a bug to the owner.
                    self._audit(f"ROUTE_UNAVAILABLE {route.id} needs {sorted(missing)}")
                    continue
                found.append(route)
        found.sort(key=score)
        return found

    def resolve(self, goal: str, state: WorldState | None = None) -> Resolution:
        """Pick the best route, or report that the choice needs the owner."""
        routes = self.candidates(goal, state)
        if not routes:
            return Resolution(None, (), why="no route was available for that goal")
        best = routes[0]
        tied = [route for route in routes if score(route)[:-1] == score(best)[:-1]]
        if len(tied) > 1 and len({route.kind for route in tied}) > 1:
            return Resolution(None, tuple(routes), ambiguous=True,
                              why="two materially different routes are equally good")
        self._audit(f"ROUTE_SELECTED {best.id} kind={best.kind} reuse={best.reuses_existing}")
        return Resolution(best, tuple(routes), why=best.why or f"best available {best.kind} route")


# --- providers over the existing V2 systems -------------------------------------------------------
#
# These are adapters, not new capabilities. Each one turns something V2 already knows into routes the
# resolver can compare, which is the whole mechanism by which V3 gets to prefer "reuse the open tab" over
# "launch a browser" without either system knowing about the other.

class FastPathRoutes:
    """The V2 deterministic launcher, as a route provider.

    ``void.core.fast_path`` already resolves "open <known app>" into engine-chosen tool calls, with the
    catalog matching, de-gluing and multi-target handling that was built and measured for V2. Re-deriving
    any of that here would be the duplicate-system mistake the blueprint warns about, so this wraps it.

    Routes are NATIVE_APP kind, and marked as reusing existing state when the application is already
    running - launching something already open is the exact waste the resolver exists to avoid.
    """

    name = "fast_path"

    def __init__(self, fast_path):
        self._fast = fast_path

    def propose(self, goal: str, state: WorldState) -> Iterable[Route]:
        if self._fast is None:
            return ()
        decision = self._fast.decide(goal)
        plan = getattr(decision, "plan", None)
        if plan is None:
            return ()
        routes: list[Route] = []
        for target in plan.targets:
            if not target.alternatives:
                continue
            already = state.is_running(target.label)
            routes.append(Route(
                kind=RouteKind.NATIVE_APP,
                describes=f"open {target.label}",
                calls=tuple(target.alternatives),
                reuses_existing=already,
                requires=frozenset({"native_app"}),
                # Measured in V2: the deterministic launcher resolves and launches in milliseconds and
                # was validated across the owner's real catalog.
                reliability=0.95 if already else 0.9,
                latency_s=0.05,
                why=("the application is already running, so this activates what is there"
                     if already else "a known application with a validated launch route"),
            ))
        return routes


class ExistingTabRoutes:
    """Open browser tabs, as routes that reuse an authenticated session.

    This is the provider that makes the blueprint's Gmail example work. It proposes nothing unless a tab
    actually matches, and it never *opens* anything - activating a tab is the browser layer's job, and the
    call it emits names that capability.

    ``activate_tab`` is referenced by name rather than imported: the browser capability is a separate
    adapter, and a provider should not import one in order to describe a route that uses it. If the browser
    layer is absent, ``requires`` drops the route before it can be chosen.
    """

    name = "existing_tabs"

    #: Tool the browser layer registers to bring a tab forward. Named, not imported.
    ACTIVATE_TOOL = "activate_tab"

    def propose(self, goal: str, state: WorldState) -> Iterable[Route]:
        needle = _site_needle(goal)
        if not needle:
            return ()
        preferred = state.preferred("browser")
        routes: list[Route] = []
        for browser, url, title in state.tabs_matching(needle):
            # A tab in the owner's preferred browser is better than the same page elsewhere.
            in_preferred = bool(preferred and preferred in (browser or "").lower())
            routes.append(Route(
                kind=RouteKind.EXISTING_STATE,
                describes=f"switch to the {needle} tab already open in {browser or 'the browser'}",
                calls=(DirectCall(name=self.ACTIVATE_TOOL,
                                  arguments={"url": url},
                                  reply=f"Switched to {needle}."),),
                reuses_existing=True,
                requires=frozenset({"browser"}),
                reliability=0.97 if in_preferred else 0.9,
                latency_s=0.2,
                why=("that page is already open in your preferred browser" if in_preferred
                     else "that page is already open"),
            ))
        routes.sort(key=score)
        return routes


#: Words that mean "put something in front of me" rather than naming the thing. Stripped to find the target.
_OPEN_VERBS = ("open", "go to", "goto", "navigate to", "take me to", "show me", "show",
               "bring up", "pull up", "launch", "start", "switch to", "visit")


def _site_needle(goal: object) -> str:
    """The thing an "open X" goal is about, or "" when the goal is not of that shape.

    Deliberately simple string handling, and deliberately not a model call: deciding whether a sentence
    names a site is not semantic work worth a round trip. A goal this does not understand yields no routes
    from this provider, and other providers still get their turn.
    """
    if not isinstance(goal, str):
        return ""
    text = " ".join(goal.lower().split()).strip(".,!?;:")
    for verb in sorted(_OPEN_VERBS, key=len, reverse=True):
        if text.startswith(verb + " "):
            rest = text[len(verb) + 1:].strip()
            for article in ("the ", "my ", "a "):
                if rest.startswith(article):
                    rest = rest[len(article):]
            # Only the first couple of words: "open pinterest and find recipes" is about pinterest.
            words = rest.split()
            return " ".join(words[:2]).strip() if words else ""
    return ""
