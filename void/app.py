"""Assistant facade - wires config, providers, tools, security, and the
agent into one object the CLI and UI can drive.
"""
from __future__ import annotations

import time
from typing import Callable

from void import perf
from void.actions.apps import AppActions
from void.actions.computer import AppCatalog, ComputerActions, make_backend
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.agent import Agent, AgentResult
from void.core.kill_switch import KillSwitch
from void.core.task import Status, Task, TaskStore
from void.providers.registry import ProviderRegistry
from void.security.risk import RiskGate

ConfirmFn = Callable[[str], bool]
OnEvent = Callable[[str], None]


class Assistant:
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

        # Providers
        self.providers = ProviderRegistry.from_config(self.config)

    def _agent(self) -> Agent:
        provider = self.providers.select()  # raises if none available
        perf.emit("route", provider=getattr(provider, "name", "unknown"), reason="select")
        return Agent(
            provider=provider,
            tools=self.tools,
            risk_gate=self.risk_gate,
            kill_switch=self.kill_switch,
            store=self.store,
            max_steps=self.config.get("agent.max_steps", 12),
            max_retries=self.config.get("agent.max_retries", 2),
            on_event=self.on_event,
            defer_confirmation=self._confirm_fn is None,
        )

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

    def run(self, goal: str) -> AgentResult:
        return self._measured(lambda: self._agent().run(goal))

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
