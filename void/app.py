"""Assistant facade - wires config, providers, tools, security, and the
agent into one object the CLI and UI can drive.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from void import perf
from void.actions.apps import AppActions
from void.actions.computer import AppCatalog, ComputerActions, make_backend
from void.actions.files import FileActions, PathNotAllowed
from void.actions.browser import BrowserActions
from void.actions.observe import ObserveActions
from void.actions.screen import ScreenActions
from void.actions.desktop import DesktopActions
from void.actions.messaging import MessagingActions
from void.actions.artifacts import ArtifactActions
from void.actions.research import ResearchActions
from void.actions.reference import ReferenceActions
from void.actions.resources import ResourceActions
from void.obs import TelemetryPolicy, configure as configure_telemetry
from void.providers.policy import ProviderPolicy
from void.a2a import A2aGateway, A2aPolicy
from void.ui.a2ui import A2uiSession
from void.ui.agui import AgUiAdapter
from void.research import ResearchEngine
from void.system.devices import snapshot as device_snapshot
from void.system.resources import ResourceManager, ResourcePolicy
from void.orchestration.reference import ReferenceResolver, parse_reference
from void.orchestration.overrides import extract_overrides
from void.maintenance import Maintenance
from void.state import StateStore
from void.orchestration.referents import (RecentThings, recent_candidates, tab_candidates,
                                          window_candidates)
from void.actions.folders import DEFAULT_DEPTH, FolderCatalog, scan_roots
from void.actions.vision import VisionActions
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.agent import Agent, AgentResult
from void.core.fast_path import FastPath
from void.orchestration.apps import ApplicationRegistry
from void.orchestration.commands import ControlContext, ControlIntent, classify as classify_control
from void.orchestration.events import EventLog
from void.orchestration.messaging import (ConversationRoutes, MessagingAction,
                                          installed_via_catalog, observed_conversations)
from void.orchestration.routes import ExistingTabRoutes, FastPathRoutes, RouteResolver, WorldState
from void.orchestration.verify import Verifier
from void.orchestration.websites import WebsiteRoutes, parse_target
from void.core.kill_switch import KillSwitch
from void.core.task import Status, Task, TaskStore
from void.memory import intent as memory_intent
from void.memory import scope as memory_scope
from void.memory.crypto import MemoryUnavailable
from void.memory.persist import Injection
from void.memory.service import MemoryService
from void.memory.tool import make_tool as make_memory_tool
from void.providers.base import LLMProvider, ProviderUnavailable
from void.providers.registry import ProviderRegistry
from void.security.protected import EngineProtected
from void.security.risk import RiskGate

_log = logging.getLogger(__name__)

ConfirmFn = Callable[[str], bool]
OnEvent = Callable[[str], None]


class _NoProvider(LLMProvider):
    """The provider of a run that must make NO model call (the deterministic fast path). Any attempt to use it is a
    bug, so it refuses loudly instead of silently reaching a real model."""
    name = "none"

    def available(self) -> bool:
        return False

    def generate(self, messages, tools=None):
        raise ProviderUnavailable("This run is deterministic and makes no model call.")


def _build_browser(config):
    """The V.O.I.D browser adapter, or None when browser automation is switched off.

    Lazy by construction: PlaywrightBrowser starts nothing until a tool is actually used, so holding one
    costs nothing for an owner who never asks V.O.I.D to use a browser.
    """
    from void.browser import BrowserPolicy
    policy = BrowserPolicy.from_config(config)
    if not policy.enabled:
        return None
    try:
        from void.browser.playwright_adapter import PlaywrightBrowser
        return PlaywrightBrowser(policy)
    except Exception:                                          # noqa: BLE001 - never block startup
        _log.exception("BROWSER_ADAPTER_UNAVAILABLE")
        return None


def _device_registry(state_dir):
    """The EXISTING pairing registry, read for its trust records.

    Opened read-only in effect: ``device_trust`` only ever calls ``list()``. Pairing, granting and revoking
    stay where they were - the ``void device`` CLI commands, which the owner runs deliberately. There is no
    tool that pairs a device, because pairing is an authorization and authorizations are not something a
    model should be able to reach.

    Returns None when there is no registry file yet, so an owner who has never paired anything sees "no
    devices are paired" rather than an error.
    """
    try:
        from void.device.identity import DeviceRegistry
        path = state_dir / "devices" / "devices.json"
        if not path.exists():
            path = state_dir / "devices.json"
        if not path.exists():
            return None
        return DeviceRegistry(path)
    except Exception:                                          # noqa: BLE001
        _log.info("DEVICE_REGISTRY_UNAVAILABLE")
        return None


def _short_goal(goal: object, limit: int = 40) -> str:
    """A task's goal, shortened for naming it back to the owner when asking which one they meant."""
    text = " ".join(str(goal or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _build_desktop(config):
    """The V.O.I.D desktop adapter, or None when UI Automation is switched off.

    Returns None rather than raising when ``uiautomation`` is missing, so a machine without the dependency
    still starts and simply reports desktop automation as unavailable. Like the browser, nothing is
    initialised until a tool is used - the COM apartment is created on the adapter's own thread on first
    call, not here.
    """
    from void.desktop import DesktopPolicy
    policy = DesktopPolicy.from_config(config)
    if not policy.enabled:
        return None
    try:
        from void.desktop.uia_adapter import UiaDesktop
        return UiaDesktop(policy)
    except Exception:                                          # noqa: BLE001 - never block startup
        _log.exception("DESKTOP_ADAPTER_UNAVAILABLE")
        return None


def _file_facts(file_actions, path: str) -> dict | None:
    """Existence and size for verification, through the CONFINED file layer.

    Returns None when the path is not one V.O.I.D may inspect, which the verifier reports as "could not
    check" rather than as a failure. Verification is not a way around allowed roots: it sees exactly what
    the owner already permitted the file tools to see.
    """
    try:
        result = file_actions.stat(path)
    except Exception:                                          # noqa: BLE001
        return None
    if not result.ok or not isinstance(result.data, dict):
        return None
    return {"exists": True, "size_bytes": int(result.data.get("size_bytes") or 0)}


def _sentence(text: object) -> str:
    """A plan's reason as something fit to say: capitalised, full-stopped.

    The planner's reasons are written as fragments ("tell me whose conversation to open") because
    they are also log and test material. This is presentation only - no text from a tool, a window
    title or a model passes through it.
    """
    body = " ".join(str(text or "").split())
    if not body:
        return "I could not work out what to open."
    body = body[0].upper() + body[1:]
    return body if body.endswith((".", "?", "!")) else body + "."


class Assistant:
    memory: MemoryService | None = None      # set in __init__; None when disabled

    def __init__(self, config: Config | None = None,
                 confirm_fn: ConfirmFn | None = None,
                 on_event: OnEvent | None = None):
        self.config = config or Config.load()
        self.on_event = on_event or (lambda _m: None)
        # No synchronous confirmer (headless/unattended) -> HIGH-risk actions
        # are deferred as a durable AWAITING_CONFIRMATION state instead of
        # being decided inline. A present confirmer keeps synchronous prompts.
        self._confirm_fn = confirm_fn

        # Persistence directory is needed early for the cross-process stop file.
        state_dir = self.config.state_dir()

        # Security / control
        self.kill_switch = KillSwitch(
            phrase=self.config.get("kill_switch.phrase", "VOID, STOP EVERYTHING"),
            require_pin=self.config.get("kill_switch.require_pin", False),
            stop_file=state_dir / "STOP",
        )
        self.risk_gate = RiskGate(
            confirm_at_or_above=self.config.get("security.confirm_at_or_above",
                                                "high"),
            confirm_fn=confirm_fn,
        )

        # Persistence
        self.store = TaskStore(state_dir / "tasks.sqlite")

        # Provider governance (V3). WHICH provider may perform WHICH capability using WHICH data.
        # Built FIRST, before any capability that could reach a provider, because several of them consult
        # it: the screen tool asks whether this provider may receive a screen image, research asks whether
        # a search provider may be used at all. Deliberately independent of the provider registry - the
        # policy decides, the registry executes - and nothing a model produces is an input to it.
        # See void/providers/policy.py.
        # Structured local state (V3): preferences, system snapshots, detected changes, maintenance
        # runs. Separate file and separate concept from void.memory, which stays the encrypted,
        # owner-reviewed store - this one holds plain knowledge about the COMPUTER and no secrets.
        # See void/state/store.py for the boundary and what it refuses to hold.
        self.state = StateStore(state_dir / "state.sqlite")

        self.provider_policy = ProviderPolicy.from_config(self.config)

        # Tools (actions)
        file_actions = FileActions(
            allowed_roots=self.config.allowed_roots(),
            delete_to_recycle_bin=self.config.get(
                "security.delete_to_recycle_bin", True),
            protected_roots=self.config.protected_roots(),
            engine_protected=EngineProtected.default(state_dir=state_dir),
        )
        # Windows application/window control (Phase 8A). The backend is lazy:
        # nothing Windows-specific is imported until a computer tool is used.
        backend = make_backend()
        catalog = AppCatalog(backend)
        # ``recent`` is read lazily: RecentThings is built further down, and a reference is
        # only ever recorded at the moment a path is actually opened.
        app_actions = AppActions(file_actions, catalog=catalog,
                                 recent=lambda: getattr(self, "recent", None))
        computer_actions = ComputerActions(
            backend, catalog,
            protected_processes=self.config.protected_processes())
        self.tools = ToolRegistry()
        self.tools.register_all(file_actions.tools())
        self.tools.register_all(app_actions.tools())
        self.tools.register_all(computer_actions.tools())
        # Read-only observation of the machine itself: health, attached devices, network state
        # (V2 domains 3, 6 and 7). Holds no state and caches nothing - see void/actions/observe.py
        # for the invariants every tool in it shares.
        self.tools.register_all(ObserveActions().tools())
        # Camera (V2 domain 5). Registered always, but the gate is deny-by-default: while
        # camera.enabled is false every one of these tools refuses, so registration grants nothing.
        # See void/vision/__init__.py for the four independent controls.
        # ``providers`` is a callable because the registry is built further down: resolving it lazily keeps
        # the construction order free and means the camera asks for a vision provider only if an image is
        # actually about to be sent. It is only ever asked for a VISION-capable one.
        self.vision = VisionActions(config=self.config,
                                    providers=lambda: getattr(self, "providers", None),
                                    kill_switch=self.kill_switch)
        self.tools.register_all(self.vision.tools())
        # Screen understanding (V3). SEPARATE switches from the camera: a screen holds passwords and
        # documents, so screen.enabled and screen.allow_cloud_analysis are their own decisions. The
        # vision TRANSPORT is reused from the provider layer rather than duplicated.
        self.screen = ScreenActions(config=self.config,
                                    providers=lambda: getattr(self, "providers", None),
                                    kill_switch=self.kill_switch,
                                    provider_policy=lambda: getattr(self, "provider_policy", None))
        self.tools.register_all(self.screen.tools())
        # Browser (V3). Deny-by-default like the camera: while browser.enabled is false every tool here
        # refuses, so registration grants nothing. Playwright lives only in void/browser/.
        self.browser = _build_browser(self.config)
        self.tools.register_all(BrowserActions(browser=lambda: self.browser).tools())
        # Desktop automation through UI Automation (V3). The most powerful capability in V.O.I.D and the
        # most tightly gated: deny-by-default behind desktop.enabled, scoped to one window at a time,
        # protected processes refused, no typing into password fields. See void/desktop/__init__.py.
        self.desktop = _build_desktop(self.config)
        self.tools.register_all(DesktopActions(desktop=lambda: self.desktop).tools())
        # Conversations with people (V3). Composes the desktop adapter above and the EXISTING
        # validated launcher - it opens no window and starts no program by any other route, and it
        # cannot send: a control that would act rather than open is refused before it is clicked.
        self.messaging = MessagingActions(desktop=lambda: self.desktop, catalog=catalog,
                                          launch=app_actions.launch_app)
        self.tools.register_all(self.messaging.tools())
        # Artifact generation (V3). Writes through ``file_actions``, so the owner's allowed roots, the
        # protected roots and the overwrite confirmation all apply to a generated document exactly as they
        # do to any other write. Every document is reopened and counted before success is reported.
        self.artifacts = ArtifactActions(file_actions=file_actions,
                                         recent=lambda: getattr(self, "recent", None))
        self.tools.register_all(self.artifacts.tools())
        # Research (V3). Composes the browser and artifact layers; opens no network path of its own.
        self.research = ResearchEngine.from_config(self.config, browser=lambda: self.browser,
                                                   policy=self.provider_policy)
        self.tools.register_all(ResearchActions(engine=lambda: self.research,
                                                artifacts=self.artifacts).tools())
        # What V.O.I.D has recently produced or the owner recently referred to, so that "this chart" and
        # "the document I was looking at" have something to resolve against. References only: labels and
        # targets, never content and never permissions.
        self.recent = RecentThings()
        self.references = ReferenceResolver([
            recent_candidates(self.recent),
            tab_candidates(lambda: self.browser),
            window_candidates(lambda: self.desktop),
        ])
        self.tools.register_all(
            ReferenceActions(resolver=lambda: self.references, recent=self.recent).tools())
        # Resource control (V3) and device standing. The resource manager is deny-by-default and can only
        # lower a process's CPU priority, reversibly - see void/system/resources.py for why that is the
        # whole of it. Device standing composes the EXISTING pairing registry with the EXISTING hardware
        # reading; it builds no second registry and can grant nothing.
        # --- V3 interoperability surfaces -------------------------------------------
        # All three are adapters over systems that already exist, all three are off by default, and none
        # of them is an authority. See void/ui/agui.py, void/ui/a2ui.py, void/a2a/__init__.py.
        #
        # AG-UI publishes task events to a front end. Outbound only - subscribing to the event log cannot
        # become a way to drive V.O.I.D, because this adapter has no method that acts.
        self.agui = (AgUiAdapter(event_log=self.events,
                                 thread_id=self.config.get("agui.thread_id", "void"))
                     if self.config.get("agui.enabled", False) else None)
        # A2UI mints and resolves approval tokens for dynamic surfaces. A returned token is evidence that
        # a human clicked on a specific pending action; it is an INPUT to the RiskGate, never a bypass.
        self.a2ui = A2uiSession()
        # A2A is the untrusted external-agent boundary. It decides and returns; it never executes. An
        # accepted request still goes through Assistant.run and the whole funnel, with the skill's tool
        # ceiling on top.
        self.a2a = A2aGateway(A2aPolicy.from_config(self.config))

        # Weekly maintenance (V3). Constructed, NOT scheduled: the runner claims its own week in the
        # state database, so whatever fires it cannot cause a second pass, and nothing here starts a
        # timer. Deliberately not hooked into the voice runtime's monitor loop - that code is frozen for
        # this milestone - so `void maintenance run` (or any scheduler pointed at it) is the trigger.
        # It can only PROPOSE memory; see void/maintenance/__init__.py for the promotion boundary.
        self.maintenance = Maintenance(
            self.state, catalog=catalog, config=self.config,
            memory=lambda: getattr(self, "memory", None))

        self.resources = ResourceManager(ResourcePolicy.from_config(self.config))
        self.tools.register_all(ResourceActions(
            manager=lambda: self.resources,
            registry=lambda: _device_registry(state_dir),
            devices=lambda: device_snapshot(),
        ).tools())

        # Persistent memory (V2.0). Lazy: no file and no key exist until the first write.
        # Memory is DATA - it never feeds RiskGate. The model may only SUGGEST via
        # propose_memory; its suggestions need owner review before they can be recalled.
        if self.config.get("memory.enabled", True):
            self.memory = MemoryService.from_config(self.config)
            if self.config.get("memory.propose_tool", True):
                self.tools.register(make_memory_tool(self.memory))

        # --- V3 orchestration (void/orchestration) -----------------------------------
        # Built over the systems that already exist: the registry reads THIS catalog, the resolver uses
        # THIS fast path, the verifier observes through THESE actions. Nothing here discovers, launches or
        # authorizes anything - orchestration decides what to attempt, and every attempt still goes
        # through Agent._run_call (kill switch -> risk -> RiskGate) unchanged.
        self.events = EventLog()
        # Observability (V3). Configures the official OpenTelemetry SDK so the spans already emitted
        # through the API in void/orchestration/trace.py reach a collector. Off by default; a failure here
        # is reported as degradation on self.telemetry and never raised, because a collector being down
        # must not fail a task. See void/obs/__init__.py.
        self.telemetry = configure_telemetry(TelemetryPolicy.from_config(self.config))
        if self.telemetry.degraded:
            _log.warning("TELEMETRY_DEGRADED reason=%s", self.telemetry.reason)
        self.applications = ApplicationRegistry(
            catalog_entries=catalog.entries,
            running_apps=lambda: (computer_actions.list_running_apps().data or []),
            preferences=dict(self.config.get("preferences", {}) or {}))
        self.verifier = Verifier(
            list_windows=lambda: (computer_actions.list_windows().data or []),
            running_processes=lambda: {str(row.get("name", "")).lower()
                                       for row in (computer_actions.list_running_apps().data or [])},
            active_window=lambda: computer_actions.get_active_window().data,
            file_facts=lambda path: _file_facts(file_actions, path))
        # Providers
        self.providers = ProviderRegistry.from_config(self.config)
        # Deterministic fast path for plain "open <known app>" commands (no model call). Off switch: fast_path.enabled.
        self._fast = (FastPath(catalog,
                               answer_unknown=self.config.get("fast_path.answer_unknown_apps", True),
                               folders=self._folder_catalog(file_actions))
                      if self.config.get("fast_path.enabled", True) else None)
        # Route resolution over the deterministic launcher, any open browser tabs, and conversations
        # with people. All three are adapters over systems that already exist; the resolver only
        # compares and chooses. The conversation provider's two sources are callables so that an
        # ordinary goal costs nothing - neither the catalog nor the window list is read unless the
        # utterance is actually about a conversation.
        self._conversations = ConversationRoutes(
            installed=lambda: installed_via_catalog(catalog),
            conversations=self._open_conversations)
        # Websites are a fourth entity kind, and the one that had no route at all: "Open YouTube"
        # went to the application matcher, correctly found nothing installed, and was left to the
        # model. ExistingTabRoutes still outranks this one, so a page already open is reused.
        self._websites = WebsiteRoutes()
        self.routes = RouteResolver([FastPathRoutes(self._fast), ExistingTabRoutes(),
                                     self._conversations, self._websites])

    def world_state(self, goal: str = "") -> WorldState:
        """What V.O.I.D can observe about the computer right now, for route resolution.

        Everything here is READ, never asserted: running applications come from the window list, open tabs
        from the browser adapter, preferences from the owner's config. A model cannot put an application
        into the running set or invent a tab.

        Capabilities are reported as present only when the machinery actually exists, so a route that
        requires a browser is dropped before selection on a machine where browser automation is off -
        rather than being chosen and then failing.
        """
        capabilities = {"native_app", "desktop"}
        tabs: tuple = ()
        if self.browser is not None and self.browser.available():
            capabilities.add("browser")
            try:
                tabs = tuple((tab.browser, tab.url, tab.title) for tab in self.browser.tabs())
            except Exception:                                  # noqa: BLE001 - stale beats crashing
                tabs = ()
        if getattr(self, "screen", None) is not None and self.screen.policy.enabled:
            capabilities.add("screen")
        # UI Automation is a separate capability from V2's lightweight window list: "desktop" is
        # being able to see and activate windows, "desktop_ui" is being able to reach inside one.
        # A route that needs the second is dropped when desktop.enabled is false, which is how
        # "open Rushi's chat" reports honestly instead of failing mid-way.
        if getattr(self, "desktop", None) is not None:
            try:
                if self.desktop.available():
                    capabilities.add("desktop_ui")
            except Exception:                                  # noqa: BLE001 - absent beats crashing
                _log.debug("DESKTOP_UI_PROBE_FAILED", exc_info=True)
        running: frozenset = frozenset()
        try:
            running = frozenset(application.name.lower()
                                for application in self.applications.running())
        except Exception:                                      # noqa: BLE001 - stale beats crashing
            pass
        # Preferences come from BOTH sources, the durable one winning: config is the shipped default,
        # the state store is what the owner has actually chosen since. Same flat shape either way, so
        # routing consumes them through the mechanism it already had.
        preferences = dict(self.applications.preferences)
        try:
            preferences.update(self.state.as_routing_map())
        except Exception:                                      # noqa: BLE001 - a bad store must not
            _log.debug("PREFERENCES_UNAVAILABLE", exc_info=True)   # stop routing; config still applies
        # What the owner asked for in THIS utterance, which outranks any stored preference.
        overrides = extract_overrides(goal) if goal else {}
        return WorldState(running_apps=running, open_tabs=tabs,
                          capabilities=frozenset(capabilities),
                          preferences=preferences, overrides=overrides)

    def _open_conversations(self):
        """Conversation windows open right now, for the messaging route provider.

        Read through the UI Automation adapter, and only when it is actually available - this is on
        the path of a conversation request, so it must not pay for a disabled capability or raise
        into route resolution. Window titles are untrusted data; they are compared, never believed.
        """
        desktop = getattr(self, "desktop", None)
        if desktop is None:
            return ()
        try:
            if not desktop.available():
                return ()
            return observed_conversations(desktop.windows())
        except Exception:                                      # noqa: BLE001 - no candidates is a
            _log.debug("CONVERSATIONS_UNAVAILABLE", exc_info=True)   # fine answer; crashing is not
            return ()

    def _folder_catalog(self, file_actions):
        """A shallow index of the owner's folder names, or None when folder resolution is off.

        It is handed the FILE LAYER's confinement check rather than the configuration: allowed roots, protected
        roots and the engine's own protected locations are evaluated in exactly one place, and the resulting path is
        confined a second time by ``open_path`` when it runs.
        """
        if not self.config.get("fast_path.resolve_folders", True):
            return None

        def confine(path):
            try:
                return file_actions._confine(path)
            except PathNotAllowed:
                return None
            except OSError:
                return None

        return FolderCatalog(
            roots=scan_roots(Path.home(), [str(r) for r in self.config.allowed_roots()]),
            confine=confine,
            depth=int(self.config.get("fast_path.folder_depth", DEFAULT_DEPTH)),
            ttl_s=float(self.config.get("fast_path.folder_ttl_s", 600.0)))

    def _agent(self, memory_first: "bool | str" = False) -> Agent:
        order = getattr(self.providers, "available_order", None)
        chain = list(order()) if callable(order) else []
        provider = chain[0] if chain else self.providers.select()   # select() raises if none available
        perf.emit("route", provider=getattr(provider, "name", "unknown"), reason="select")
        return Agent(
            provider=provider,
            fallbacks=chain[1:],
            tools=self.tools,
            risk_gate=self.risk_gate,
            kill_switch=self.kill_switch,
            store=self.store,
            max_steps=self.config.get("agent.max_steps", 12),
            max_retries=self.config.get("agent.max_retries", 2),
            on_event=self.on_event,
            defer_confirmation=self._confirm_fn is None,
            memory_context=self._memory_context_fn(chain or [provider], memory_first=memory_first),
            recall_only=bool(memory_first),
        )

    def _memory_context_fn(self, chain, memory_first: "bool | str" = False):
        """Goal -> [context messages]. Sensitive / non-cloud memory is withheld whenever the
        selected provider is not the local one (unknown providers count as cloud).

        ``chain`` is every provider this run might use, not just the first: a run that could hand over to a cloud
        provider must be built under cloud rules from the start, or failing over would send it memory that was
        only ever cleared for the local model.
        """
        if self.memory is None:
            return None
        providers = list(chain) if isinstance(chain, (list, tuple)) else [chain]
        for_cloud = any(getattr(p, "name", "") != "local" for p in providers)

        def fn(goal: str) -> Injection:
            try:
                block = self.memory.build_context(goal, for_cloud=for_cloud, recent_fallback=memory_first in ("personal", "explicit"))
            except MemoryUnavailable as exc:
                _log.warning("MEMORY_UNAVAILABLE code=%s", exc.code)   # the run proceeds without memory
                block = None
            if memory_first:
                msgs = (memory_intent.recall_context_message(block),)
            else:
                msgs = (block.as_message(),) if block else ()
            # ``protected``: the memory strings that must never reach plaintext task history.
            return Injection(messages=msgs, protected=block.texts if block else (),
                             carries_memory=block is not None)

        return fn

    def _fast_route(self, goal: str) -> AgentResult | None:
        """A plain "open <known app>" command, executed with NO model call (void/core/fast_path.py).

        The fast path only decides WHAT to call; the call still runs through ``Agent._run_call`` (kill switch, risk
        level, ``RiskGate.authorize``, telemetry). Anything it does not recognise, cannot resolve to exactly one
        application, or that fails returns None and the ordinary agent handles the goal exactly as before."""
        if self._fast is None or self.kill_switch.engaged:
            return None
        decision = self._fast.decide(goal)
        if decision.plan is None:
            if decision.matched:
                perf.emit("route", provider="none", reason="fast_path_miss", why=decision.why, llm_calls=0)
            if decision.reply:
                # Several installed applications match the name. Answering the question here is deterministic and
                # instant, and is the only answer available at all when no model can be reached; NOTHING is
                # launched, no tool runs, and the next command is unaffected.
                kind = "not_found" if decision.why == "unknown" else "clarify"
                perf.emit("route", provider="none", reason="fast_path", kind=kind, llm_calls=0)
                task = Task(goal="[app clarification]", id="(apps)", status=Status.COMPLETED,
                            result=decision.reply)
                return AgentResult(task=task, status=Status.COMPLETED, result=decision.reply, steps=0)
            return None
        t0 = time.monotonic()
        with perf.ensure_interaction("cli"):
            agent = Agent(provider=_NoProvider(), tools=self.tools, risk_gate=self.risk_gate,
                          kill_switch=self.kill_switch, store=self.store, on_event=self.on_event,
                          defer_confirmation=self._confirm_fn is None)
            result = agent.run_direct_targets(
                goal, [(t.label, t.alternatives) for t in decision.plan.targets],
                failures=decision.failures)
            if result is None:
                perf.emit("route", provider="none", reason="fast_path_miss", why="failed", llm_calls=0)
                return None
            perf.emit("route", provider="none", reason="fast_path", kind=decision.plan.kind,
                      targets=len(decision.plan.targets), missing=len(decision.failures), llm_calls=0)
            perf.emit("complete", status=result.status, total_s=round(time.monotonic() - t0, 3), steps=result.steps)
            return result

    def _conversation_route(self, goal: str, *, named_only: bool = False) -> AgentResult | None:
        """A request for a person's conversation, answered deterministically - no model call.

        Why this exists. "Open my chat" used to take 24-49 seconds and end either in a
        model-written clarification or in ``failed``, depending on how Gemini felt: the planner
        correctly declined (no person is named), the messaging route was dropped because
        ``desktop.enabled`` is false, and the model was then left to grope at
        ``resolve_reference``, ``list_windows``, ``list_tabs``, ``get_active_window`` and
        ``list_app_windows`` - several of which answer "not configured". All of that was reproduced.

        Every one of those outcomes is already decided, exactly and instantly, by
        :func:`void.orchestration.messaging.plan_conversation`. Handing the question to a model
        could only add latency and variance to an answer the engine already had, and the two could
        disagree. So this answers from the plan, and the request always reaches a terminal state
        at once.

        It uses the SAME provider instance the route resolver uses, so the deterministic answer and
        the route can never disagree about what was decided. ``None`` means "not a conversation
        request, or one that can genuinely be acted on" - and the ordinary path then runs unchanged.

        **Why this is consulted twice, around the fast path.** With ``named_only`` it runs FIRST,
        because a request that names a person is unambiguously about a conversation and the
        application matcher would otherwise claim it: "Open Rushi's chat" was being answered "I
        can't find rushi's chat on this machine", which is confidently wrong - it looked for an
        application by that name. Without ``named_only`` it runs AFTER, so a bare "open chat" still
        gets its turn at being an application that is genuinely installed under that name, and only
        falls back to "whose conversation?" when it is not.
        """
        if self.kill_switch.engaged:
            return None
        try:
            plan = self._conversations.plan(goal, self.world_state(goal))
        except Exception:                                      # noqa: BLE001 - never block a goal
            _log.debug("CONVERSATION_PLAN_UNAVAILABLE", exc_info=True)
            return None
        if plan.irrelevant:
            return None                                        # not about a conversation at all
        if named_only and not plan.contact:
            # No person named, so this could still be an application whose name happens to contain a
            # conversation noun. Let the matcher win when it can genuinely RESOLVE one; claim the
            # phrase when all it could offer is "not installed on this machine".
            #
            # This ordering became necessary when the launch grammar learned to drop "my": the
            # matcher then accepted "my personal chat" as a name, found nothing, and answered
            # "I can't find personal chat on this machine" - where the useful answer is to ask whose
            # conversation is meant. A decision with a plan still takes precedence, so an
            # application genuinely called "Chat" is unaffected.
            try:
                if self._fast is not None and self._fast.decide(goal).plan is not None:
                    return None
            except Exception:                                  # noqa: BLE001 - matcher is optional
                return None

        if plan.action == MessagingAction.ASK:
            reply = plan.question()
        elif not plan.actionable:
            reply = _sentence(plan.reason)
        elif "desktop_ui" not in self.world_state(goal).capabilities:
            # The plan can be carried out in principle, but this machine cannot reach inside an
            # application. Saying so here - rather than letting the route be silently dropped and
            # the model improvise - is the difference between "I cannot do that, here is why" and
            # two minutes of working.
            reply = (f"I can see {plan.display}, but reaching inside an application needs desktop "
                     f"automation, which is switched off. Set desktop.enabled in your local config "
                     f"and ask me again.")
        else:
            # Genuinely actionable. Execute the route deterministically, exactly as the fast path
            # executes a launch: through Agent.run_direct, so the kill switch, the tool's effective
            # risk and RiskGate.authorize all apply unchanged.
            executed = self._execute_conversation(goal)
            if executed is not None:
                return executed
            if not named_only:
                return None                                    # ordinary path owns it
            # run_direct declined (the step needs the owner's approval, or every alternative
            # failed). A request that NAMES a person still must not fall through to the
            # application matcher, which answers "I can't find rushi's whatsapp chat on this
            # machine" - confidently wrong, and the exact mis-answer this ordering exists to stop.
            return self._measured(lambda: self._agent().run(goal))
        perf.emit("route", provider="none", reason="conversation", kind=plan.action, llm_calls=0)
        task = Task(goal="[conversation]", id="(chat)", status=Status.COMPLETED, result=reply)
        return AgentResult(task=task, status=Status.COMPLETED, result=reply, steps=0,
                           engine_authored=True)

    def _reference_route(self, goal: str) -> AgentResult | None:
        """"Open the folder you just found" - answered from what was already resolved.

        The failure this fixes, in the owner's words: V.O.I.D found the Vibe Coding folder, said so,
        and then could not find it again when asked to open "the vibe coding folder you found". The
        path had been verified once and thrown away, so the follow-up started another filesystem
        search. It appeared to work only while the Explorer window stayed open, because the window
        list offers a candidate of its own.

        ``open_path`` now records every path it opens, so the reference resolver has the verified
        path to hand and this opens it directly - no search, no model.

        Deliberately narrow. It acts only when the phrase is genuinely referring (a deictic, a
        recency phrase or a noun naming a kind - see ``Reference.resolvable``), the resolution is
        DECISIVE rather than ambiguous, the referent is a folder or document, and the recorded path
        still exists. Anything else returns None and the ordinary path runs unchanged; an ambiguous
        reference in particular must stay a question rather than become a guess.
        """
        if self.kill_switch.engaged:
            return None
        try:
            reference = parse_reference(goal)
            if not reference.resolvable:
                return None
            resolution = self.references.resolve(goal)
        except Exception:                                      # noqa: BLE001 - never block a goal
            _log.debug("REFERENCE_ROUTE_UNAVAILABLE", exc_info=True)
            return None
        choice = resolution.choice
        if choice is None:
            # Ambiguous between things of this kind is not a failure and not a guess - it is a
            # question, and the resolver already phrases it. Answering here keeps it instant and
            # accurate: handing it to the model instead produced "I cannot proceed without knowing
            # which folder you are referring to" after 15.8 seconds, which is the same question
            # asked worse and slower.
            openable = [c for c in resolution.alternatives if c.kind in ("folder", "document")]
            if resolution.ambiguous and len(openable) > 1:
                question = resolution.question()
                if question:
                    perf.emit("route", provider="none", reason="reference", kind="ambiguous",
                              llm_calls=0)
                    task = Task(goal="[reference]", id="(ref)", status=Status.COMPLETED,
                                result=question)
                    return AgentResult(task=task, status=Status.COMPLETED, result=question,
                                       steps=0)
            return None
        if choice.kind not in ("folder", "document"):
            return None                                        # not something with a path
        target = str(choice.target or "")
        if not target or not Path(target).exists():
            # A recorded path that has since moved or been deleted is not an answer. Falling
            # through lets the ordinary path search for it properly.
            return None

        with perf.ensure_interaction("cli"):
            agent = Agent(provider=_NoProvider(), tools=self.tools, risk_gate=self.risk_gate,
                          kill_switch=self.kill_switch, store=self.store, on_event=self.on_event,
                          defer_confirmation=self._confirm_fn is None)
            outcome = agent.invoke_tool("open_path", {"target": target})
        if outcome.kind in ("unauthorized", "unknown"):
            return None
        if not outcome.ok:
            reply = _sentence(outcome.summary) or f"I could not open {choice.label}."
            task = Task(goal="[reference]", id="(ref)", status=Status.FAILED, result=reply,
                        error=reply)
            return AgentResult(task=task, status=Status.FAILED, result=reply, steps=1)
        reply = f"Opened {choice.label}."
        perf.emit("route", provider="none", reason="reference", kind=choice.kind, llm_calls=0)
        task = Task(goal="[reference]", id="(ref)", status=Status.COMPLETED, result=reply)
        return AgentResult(task=task, status=Status.COMPLETED, result=reply, steps=1)

    def _website_route(self, goal: str) -> AgentResult | None:
        """A request for a website, navigated and VERIFIED with no model call.

        "Open YouTube" had no deterministic route: the application matcher answered "I can't find
        youtube on this machine" (true - it is not an application) and the model was left to guess
        that ``navigate`` with an invented URL was the move. Measured on this machine that cost
        30-40 s of provider retries, succeeded only sometimes, and once produced the worst outcome
        of all - "I have opened the Wikipedia page" when the navigation had actually been refused.

        The destination comes from :mod:`void.orchestration.websites` - a table row or a hostname
        the owner spoke - so there is nothing for a model to invent, and the result is checked
        against what the browser reports rather than against what anything claims.
        """
        if self.kill_switch.engaged:
            return None
        target = parse_target(goal)
        if not target.resolved:
            return None

        state = self.world_state(goal)
        if "browser" not in state.capabilities:
            # Honest refusal rather than a fabricated success. This is the exact case that produced
            # the false "I have opened the Wikipedia page".
            reply = (f"I can reach {target.display}, but browser automation is switched off. Set "
                     f"browser.enabled in your local config and ask me again.")
            task = Task(goal="[website]", id="(web)", status=Status.FAILED, result=reply,
                        error=reply)
            return AgentResult(task=task, status=Status.FAILED, result=reply, steps=0,
                               engine_authored=True)

        # A page already open wins: ExistingTabRoutes proposes EXISTING_STATE, which outscores the
        # BROWSER route, so this picks reuse over opening another tab without knowing about tabs.
        routes = [route for route in self.routes.candidates(goal, state) if route.executable]
        routes = [route for route in routes
                  if route.calls[0].name in (self._websites.NAVIGATE_TOOL,
                                             ExistingTabRoutes.ACTIVATE_TOOL)]
        if not routes:
            return None
        call = routes[0].calls[0]
        with perf.ensure_interaction("cli"):
            agent = Agent(provider=_NoProvider(), tools=self.tools, risk_gate=self.risk_gate,
                          kill_switch=self.kill_switch, store=self.store, on_event=self.on_event,
                          defer_confirmation=self._confirm_fn is None)
            outcome = agent.invoke_tool(call.name, dict(call.arguments))
        if outcome.kind in ("unauthorized", "unknown"):
            return None                                        # the ordinary path owns confirmation
        if not outcome.ok:
            reply = _sentence(outcome.summary) or f"I could not open {target.display}."
            task = Task(goal="[website]", id="(web)", status=Status.FAILED, result=reply,
                        error=reply)
            return AgentResult(task=task, status=Status.FAILED, result=reply, steps=1)

        # -- verification: what did the browser actually load? --
        #
        # The tool returning ok means the call did not raise. What makes this COMPLETED is the page
        # the browser reports, compared against the host that was asked for.
        verified, detail = self._verify_page(target, outcome.data)
        if verified:
            reply = f"Opened {target.display}."
        else:
            # Engine-composed on purpose: ``detail`` names the host the BROWSER reported, which is
            # external text and must not be spoken. It is kept on the task, where the command line
            # can show it, and left out of the sentence the owner hears.
            reply = (f"I asked the browser to open {target.display}, but could not confirm it "
                     f"loaded.")
        status = Status.COMPLETED if verified else Status.FAILED
        task = Task(goal="[website]", id="(web)", status=status, result=reply,
                    error="" if verified else f"{reply} ({detail})".strip())
        perf.emit("route", provider="none", reason="website", verified=bool(verified), llm_calls=0)
        return AgentResult(task=task, status=status, result=reply, steps=1,
                           engine_authored=True)

    def _verify_page(self, target, navigated: object = None) -> tuple[bool, str]:
        """``(loaded, why_not)`` for the page just navigated to, as the browser reported it.

        Compares HOSTS rather than whole URLs: a site redirects ("youtube.com" ->
        "www.youtube.com/"), appends a locale or a consent parameter, and a string comparison would
        call every one of those a failure. A host match is the strongest claim available without
        reading the page, and when the browser cannot say, this reports that it cannot say.

        The evidence is the URL the NAVIGATION itself returned (``navigate`` already reports
        ``{"url", "title"}`` for the page it produced). An earlier version called ``browser.read()``
        instead, which reads whichever page is current - so with two tabs open it compared the wrong
        one and failed a navigation that had in fact succeeded: "open Wikipedia about artificial
        intelligence" was reported unconfirmed while the page was loaded, because a YouTube tab from
        the previous command answered the read. ``read()`` remains the fallback for a route that
        reports no URL of its own, such as activating an existing tab.
        """
        from urllib.parse import urlsplit

        wanted = urlsplit(target.url).hostname or ""
        reported = ""
        if isinstance(navigated, dict):
            reported = str(navigated.get("url") or "")
        if not reported:
            browser = self.browser
            if browser is None:
                return False, "the browser layer is not available"
            try:
                reported = str(getattr(browser.read(), "url", "") or "")
            except Exception as exc:                           # noqa: BLE001
                return False, f"reading the page failed ({type(exc).__name__})"
        got = urlsplit(reported).hostname or ""
        if not got:
            return False, "the browser did not report a page"
        if got == wanted or got.endswith("." + wanted) or wanted.endswith("." + got):
            return True, ""
        return False, f"the browser is showing {got} instead"

    def _execute_conversation(self, goal: str) -> AgentResult | None:
        """Run the chosen conversation route with no model call, or None if there is none to run.

        The resolver decides WHAT to call; ``Agent.invoke_tool`` runs it through the same funnel
        every other call uses - kill switch, the tool's effective risk, ``RiskGate.authorize``,
        audit, telemetry. Nothing here authorizes anything, and the arguments come from the plan
        rather than from the sentence.

        ``invoke_tool`` rather than ``run_direct``, and the reason is the whole point of this
        method. ``run_direct`` treats the calls it is given as ALTERNATIVES and returns None when
        every one of them fails, so that the ordinary agent can try something better - exactly
        right for a launch, where another route may well exist. It is wrong here: when
        ``open_conversation`` reports "I could not find Rushi in WhatsApp's visible list", that is
        the complete and final answer, and discarding it sent the request to the model instead -
        measured at 33 seconds, nine steps, ending in ``failed`` with nothing to tell the owner.
        Keeping the tool's own sentence turns that into an immediate, accurate reply.

        An ``unauthorized`` verdict is deliberately NOT answered here: the risk gate asked for the
        owner's approval, and the ordinary path owns confirmation and its deferral.
        """
        state = self.world_state(goal)
        routes = [route for route in self.routes.candidates(goal, state) if route.executable]
        if not routes:
            return None
        call = routes[0].calls[0]
        with perf.ensure_interaction("cli"):
            agent = Agent(provider=_NoProvider(), tools=self.tools, risk_gate=self.risk_gate,
                          kill_switch=self.kill_switch, store=self.store, on_event=self.on_event,
                          defer_confirmation=self._confirm_fn is None)
            outcome = agent.invoke_tool(call.name, dict(call.arguments))
        if outcome.kind in ("unauthorized", "unknown"):
            return None
        reply = _sentence(outcome.summary) if outcome.summary else None
        if outcome.ok:
            task = Task(goal="[conversation]", id="(chat)", status=Status.COMPLETED,
                        result=reply or call.reply)
            return AgentResult(task=task, status=Status.COMPLETED,
                               result=reply or call.reply, steps=1)
        # A definitive failure, reported as one. FAILED rather than COMPLETED because nothing was
        # opened - but with the tool's own explanation attached, so the owner is told why instead
        # of being handed a status with no text.
        task = Task(goal="[conversation]", id="(chat)", status=Status.FAILED,
                    result=reply, error=reply or "the conversation could not be opened")
        return AgentResult(task=task, status=Status.FAILED, result=reply, steps=1)

    def _measured(self, fn) -> AgentResult:
        """Run one agent operation inside a telemetry interaction (joining the
        voice session's id when there is one) and record its completion. Pure
        observation: the result and any exception pass through unchanged."""
        t0 = time.monotonic()
        with perf.ensure_interaction("cli"):
            result = fn()
            perf.emit("complete", status=result.status,
                      total_s=round(time.monotonic() - t0, 3), steps=result.steps)
            return result

    def _memory_command(self, goal: str) -> AgentResult | None:
        """"remember that ..." and friends, handled deterministically BEFORE any model sees the
        goal. The goal is not stored as a task (tasks.sqlite is plaintext)."""
        if self.memory is None or self.kill_switch.engaged:
            return None
        reply = memory_intent.handle(self.memory, goal, memory_scope.current_channel())
        if reply is None:
            return None
        task = Task(goal="[memory command]", id="(memory)", status=Status.COMPLETED, result=reply)
        return AgentResult(task=task, status=Status.COMPLETED, result=reply, steps=0)

    def _control_command(self, goal: str) -> AgentResult | None:
        """"stop" / "pause" / "resume" / "cancel" / "instead of that ...", decided deterministically.

        Runs BEFORE the fast path and before any model, the same way ``_memory_command`` does, because
        whether the owner wants silence or wants the work abandoned is a control signal rather than a
        semantic judgement - and getting it wrong is expensive in both directions.

        The context comes from real state: whether TTS is actually speaking, and whether a task is actually
        running. Never from the words. A bare "stop" can never reach CANCEL_TASK from any state; cancelling
        needs an explicit cancel phrase. The kill switch is untouched and keeps its own deliberate full
        phrase - this layer is about the task, not about halting V.O.I.D.
        """
        if self.kill_switch.engaged:
            return None
        command = classify_control(goal, self._control_context())
        if not command.is_control:
            return None
        reply = self._apply_control(command)
        if reply is None:
            return None
        task = Task(goal="[control command]", id="(control)", status=Status.COMPLETED, result=reply)
        return AgentResult(task=task, status=Status.COMPLETED, result=reply, steps=0,
                           local_action=command.intent == ControlIntent.STOP_SPEAKING)

    def _control_context(self) -> ControlContext:
        """What V.O.I.D is doing right now, read from real state rather than inferred."""
        speaking = False
        speaker = getattr(self, "speaking_now", None)
        if callable(speaker):
            try:
                speaking = bool(speaker())
            except Exception:                                  # noqa: BLE001
                speaking = False
        running, paused = False, False
        try:
            for task in self.store.list(limit=10):
                if task.status in (Status.RUNNING, Status.PLANNING, Status.REPLANNING,
                                   Status.VERIFYING):
                    running = True
                elif task.status == Status.PAUSED:
                    paused = True
        except Exception:                                      # noqa: BLE001 - never block a control word
            pass
        return ControlContext(speaking=speaking, task_running=running, task_paused=paused)

    def _apply_control(self, command) -> str | None:
        """Carry out a control command against task state. Returns what to say, or None to fall through.

        Speech is stopped by the voice runtime, which owns the TTS backend: ``stop_speaking`` is set by the
        runtime when one exists. With no voice attached there is nothing speaking, so the answer is simply
        an acknowledgement.
        """
        intent = command.intent
        if intent == ControlIntent.STOP_SPEAKING:
            stopper = getattr(self, "stop_speaking", None)
            if callable(stopper):
                try:
                    stopper()
                except Exception:                              # noqa: BLE001
                    pass
            return ""                                           # silence IS the response
        if intent == ControlIntent.PAUSE_TASK:
            changed = self._set_task_status(Status.PAUSED,
                                            frm=(Status.RUNNING, Status.PLANNING,
                                                 Status.REPLANNING, Status.VERIFYING))
            return "Paused." if changed else "Nothing is running."
        if intent == ControlIntent.RESUME_TASK:
            resumed = self._set_task_status(Status.RUNNING, frm=(Status.PAUSED,))
            return "Resuming." if resumed else "Nothing is paused."
        if intent == ControlIntent.CANCEL_TASK:
            # Asks rather than guessing when several jobs are running: cancelling the wrong one is not
            # recoverable, and "cancel that" does not identify which. See _destructive_status_change.
            return self._destructive_status_change(
                Status.CANCELLED, verb="cancel",
                frm=(Status.RUNNING, Status.PAUSED, Status.PLANNING,
                     Status.REPLANNING, Status.VERIFYING))
        if intent == ControlIntent.MODIFY_TASK:
            from void.orchestration.replan import apply_modification
            for task in self.store.list(limit=10):
                if task.status in Status.RESUMABLE:
                    if apply_modification(task, command.modification) is not None:
                        self.store.save(task)
                        return "Changed - carrying on from there."
                    break
            # No live task to amend: this is a new request, so let the ordinary path handle it.
            return None
        return None

    def _candidate_tasks(self, frm: tuple) -> list:
        """Tasks currently in one of ``frm``, most recent first."""
        try:
            return [task for task in self.store.list(limit=10) if task.status in frm]
        except Exception:                                      # noqa: BLE001 - never block a control word
            return []

    def _set_task_status(self, to: str, *, frm: tuple) -> bool:
        """Move the most recent matching task to ``to``. Returns whether anything changed.

        Deliberately only task STATUS: a control command never executes a capability, never reverses a
        completed side effect, and never authorizes anything.
        """
        for task in self._candidate_tasks(frm):
            try:
                task.status = to
                task.touch()
                self.store.save(task)
                return True
            except Exception:                                  # noqa: BLE001
                return False
        return False

    def _destructive_status_change(self, to: str, *, frm: tuple, verb: str) -> str:
        """Change one task's status, or ASK when more than one task could be the one meant.

        Cancelling is not recoverable the way pausing is, and "cancel that" with two jobs running does not
        identify either of them. Taking the most recent would be a guess with a real cost: abandoning work
        the owner never mentioned. So when several tasks qualify, V.O.I.D names them and asks - the same
        rule the reference resolver follows for "open that chart", applied to the one control command that
        cannot be undone.

        With exactly one candidate there is no ambiguity and nothing to ask about.
        """
        candidates = self._candidate_tasks(frm)
        if not candidates:
            return f"Nothing to {verb}."
        if len(candidates) > 1:
            listed = ", ".join(f"'{_short_goal(task.goal)}'" for task in candidates[:3])
            return (f"You have {len(candidates)} jobs going - {listed}. "
                    f"Which one should I {verb}?")
        return "Cancelled." if self._set_task_status(to, frm=frm) else f"Nothing to {verb}."

    def _recall_route(self, goal: str) -> "AgentResult | str | bool":
        """Memory questions are answered from memory, not by searching the machine.

        Returns the recall kind (truthy) for a memory-first turn (the agent gets the retrieved memory and NO tools),
        an ``AgentResult`` when the owner asked what V.O.I.D remembers and nothing is stored (answered
        deterministically, no model and no tools), or False for the normal agent. Only a *narrower*
        set of capabilities is ever granted here; authorization is untouched."""
        if (self.memory is None or not self.config.get("memory.recall_routing", True)
                or self.kill_switch.engaged):
            return False
        kind = memory_intent.classify_recall(goal)
        if kind is None:
            return False
        t0 = time.perf_counter()
        try:
            hit = bool(self.memory.retrieve(goal, for_cloud=False, limit=1, recent_fallback=kind != "entity"))
            pending = 0 if hit or kind != "explicit" else self.memory.pending_matches(goal)
        except MemoryUnavailable as exc:
            _log.warning("MEMORY_UNAVAILABLE code=%s", exc.code)
            return False
        perf.emit("memory", op="route", n=int(hit), duration_s=round(time.perf_counter() - t0, 6))
        if hit:
            return kind                             # truthy: memory-first (personal | explicit | entity)
        if kind == "explicit":                     # asked what I remember; I remember nothing relevant
            reply = memory_intent.RECALL_PENDING if pending else memory_intent.RECALL_NOTHING
            task = Task(goal="[memory recall]", id="(memory)", status=Status.COMPLETED, result=reply)
            return AgentResult(task=task, status=Status.COMPLETED, result=reply, steps=0)
        return False                               # personal-looking question with no memory: use the agent

    #: What to say when a command arrives while the stop is engaged. A CONSTANT string: nothing from
    #: the goal, a tool or an error is interpolated, so this can never become a channel for untrusted
    #: text - the same rule void/voice/status_phrases.py follows.
    STOPPED_REPLY = ("V.O.I.D is stopped. Rearm it from the tray menu - or run "
                     "'python -m void clear-stop' - and I will pick things up again.")

    def run(self, goal: str) -> AgentResult:
        # An engaged stop is answered here, before anything else, and WITHOUT clearing it.
        #
        # This was a silent dead end. Every path below is gated on the switch: _control_command
        # returns None while engaged, and the agent's first act is raise_if_engaged(), so each
        # command created a task, paused it, and returned an AgentResult whose `result` is None -
        # because task.result is only ever set on COMPLETED. The owner therefore said something,
        # saw and heard nothing, and had no way to find out why; "resume", "continue" and "rearm"
        # behaved identically, since they are control commands and control commands were skipped.
        # Recovery existed only through the tray's Rearm item or the CLI. Reproduced directly.
        #
        # Deliberately NOT a way out: this reports, it does not clear. The stop is a security
        # control with its own full phrase and optional PIN, and letting any spoken sentence lift it
        # would be exactly the weakening that protection exists to prevent. What was missing was the
        # explanation, not an escape hatch.
        if self.kill_switch.engaged:
            # PAUSED, not COMPLETED: nothing the owner asked for happened, and a status of
            # "completed" on a refused command would be its own small lie. What was missing was
            # never the status - it was the sentence. Two existing security tests assert this
            # status alongside the properties that matter (nothing executed, no provider call, the
            # switch still engaged), and all of those continue to hold here: this path runs no
            # tool, builds no agent and consults no model.
            task = Task(goal="[stopped]", id="(stopped)", status=Status.PAUSED,
                        result=self.STOPPED_REPLY)
            return AgentResult(task=task, status=Status.PAUSED, result=self.STOPPED_REPLY,
                               steps=0, engine_authored=True)
        # Control commands next: "stop" must not become a model call, and must not become a cancel.
        control = self._control_command(goal)
        if control is not None:
            return control
        handled = self._memory_command(goal)
        if handled is not None:
            return handled
        # A conversation that NAMES someone outranks the application matcher - see
        # _conversation_route on why it is consulted on both sides of the fast path.
        named = self._conversation_route(goal, named_only=True)
        if named is not None:
            return named
        # A website before the application matcher, for the same reason a named conversation goes
        # first: "YouTube" is not an application, and the matcher answering "not installed on this
        # machine" is a confidently wrong answer to a question about a website.
        web = self._website_route(goal)
        if web is not None:
            return web
        fast = self._fast_route(goal)
        if fast is not None:
            return fast
        # After the matchers, because a name V.O.I.D can resolve outright beats a reference to
        # something it happens to remember; before the model, because re-searching for a path that
        # was already verified is exactly the waste the owner reported.
        referred = self._reference_route(goal)
        if referred is not None:
            return referred
        chat = self._conversation_route(goal)
        if chat is not None:
            return chat
        route = self._recall_route(goal)
        if isinstance(route, AgentResult):
            return route
        return self._measured(lambda: self._agent(memory_first=route).run(goal))

    def resume(self, task_id: str) -> AgentResult:
        task = self.store.load(task_id)
        if task is None:
            raise ValueError(f"No such task: {task_id}")
        return self._measured(lambda: self._agent().resume(task))

    def approve(self, task_id: str) -> AgentResult:
        """Owner approves a task's pending HIGH-risk step; execute it once."""
        task = self._load_awaiting(task_id)
        return self._measured(lambda: self._agent().resume_pending(task, decision=True))

    def deny(self, task_id: str) -> AgentResult:
        """Owner denies a task's pending HIGH-risk step; it will not execute."""
        task = self._load_awaiting(task_id)
        return self._measured(lambda: self._agent().resume_pending(task, decision=False))

    def clarify(self, task_id: str, selection) -> AgentResult:
        """Owner resolves a BLOCKED directory-disambiguation by number; the
        original task then continues through the normal risk pipeline. A plain
        ``resume`` never consumes the pending choice."""
        task = self._load_blocked_disambiguation(task_id)
        return self._measured(lambda: self._agent().resume_clarification(task, selection))

    def cancel(self, task_id: str) -> Task:
        """Owner cancels a task (terminal). No pending action executes."""
        task = self.store.load(task_id)
        if task is None:
            raise ValueError(f"No such task: {task_id}")
        task.pending = None
        task.status = Status.CANCELLED
        self.store.save(task)
        return task

    def _load_awaiting(self, task_id: str) -> Task:
        task = self.store.load(task_id)
        if task is None:
            raise ValueError(f"No such task: {task_id}")
        if task.status != Status.AWAITING_CONFIRMATION or not task.pending:
            raise ValueError(f"Task {task_id} has no pending confirmation.")
        return task

    def _load_blocked_disambiguation(self, task_id: str) -> Task:
        task = self.store.load(task_id)
        if task is None:
            raise ValueError(f"No such task: {task_id}")
        pending = task.pending or {}
        if (task.status != Status.BLOCKED
                or pending.get("kind") != "directory_disambiguation"):
            raise ValueError(
                f"Task {task_id} has no pending directory choice.")
        return task

    def stop(self, reason: str = "manual stop", pin: str | None = None) -> bool:
        return self.kill_switch.engage(reason=reason, pin=pin)

    def clear_stop(self) -> None:
        """Clear an engaged stop so tasks can run/resume again."""
        self.kill_switch.reset()
