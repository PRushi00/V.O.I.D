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
from void.actions.artifacts import ArtifactActions
from void.actions.research import ResearchActions
from void.actions.reference import ReferenceActions
from void.actions.resources import ResourceActions
from void.research import ResearchEngine
from void.system.devices import snapshot as device_snapshot
from void.system.resources import ResourceManager, ResourcePolicy
from void.orchestration.reference import ReferenceResolver
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
from void.orchestration.routes import ExistingTabRoutes, FastPathRoutes, RouteResolver, WorldState
from void.orchestration.verify import Verifier
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
        app_actions = AppActions(file_actions, catalog=catalog)
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
                                    kill_switch=self.kill_switch)
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
        # Artifact generation (V3). Writes through ``file_actions``, so the owner's allowed roots, the
        # protected roots and the overwrite confirmation all apply to a generated document exactly as they
        # do to any other write. Every document is reopened and counted before success is reported.
        self.artifacts = ArtifactActions(file_actions=file_actions,
                                         recent=lambda: getattr(self, "recent", None))
        self.tools.register_all(self.artifacts.tools())
        # Research (V3). Composes the browser and artifact layers; opens no network path of its own.
        self.research = ResearchEngine.from_config(self.config, browser=lambda: self.browser)
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
        # Route resolution over the deterministic launcher and any open browser tabs. Providers are
        # adapters over existing systems; the resolver only compares and chooses.
        self.routes = RouteResolver([FastPathRoutes(self._fast), ExistingTabRoutes()])

    def world_state(self) -> WorldState:
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
        running: frozenset = frozenset()
        try:
            running = frozenset(application.name.lower()
                                for application in self.applications.running())
        except Exception:                                      # noqa: BLE001 - stale beats crashing
            pass
        return WorldState(running_apps=running, open_tabs=tabs,
                          capabilities=frozenset(capabilities),
                          preferences=dict(self.applications.preferences))

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
            cancelled = self._set_task_status(Status.CANCELLED,
                                              frm=(Status.RUNNING, Status.PAUSED, Status.PLANNING,
                                                   Status.REPLANNING, Status.VERIFYING))
            return "Cancelled." if cancelled else "Nothing to cancel."
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

    def _set_task_status(self, to: str, *, frm: tuple) -> bool:
        """Move the most recent matching task to ``to``. Returns whether anything changed.

        Deliberately only task STATUS: a control command never executes a capability, never reverses a
        completed side effect, and never authorizes anything.
        """
        try:
            for task in self.store.list(limit=10):
                if task.status in frm:
                    task.status = to
                    task.touch()
                    self.store.save(task)
                    return True
        except Exception:                                      # noqa: BLE001
            return False
        return False

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

    def run(self, goal: str) -> AgentResult:
        # Control commands first: "stop" must not become a model call, and must not become a cancel.
        control = self._control_command(goal)
        if control is not None:
            return control
        handled = self._memory_command(goal)
        if handled is not None:
            return handled
        fast = self._fast_route(goal)
        if fast is not None:
            return fast
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
