"""Assistant facade - wires config, providers, tools, security, and the
agent into one object the CLI and UI can drive.
"""
from __future__ import annotations

from typing import Callable

from void.actions.apps import AppActions
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.config import Config
from void.core.agent import Agent, AgentResult
from void.core.kill_switch import KillSwitch
from void.core.task import Task, TaskStore
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
        )
        app_actions = AppActions(file_actions)
        self.tools = ToolRegistry()
        self.tools.register_all(file_actions.tools())
        self.tools.register_all(app_actions.tools())

        # Providers
        self.providers = ProviderRegistry.from_config(self.config)

    def _agent(self) -> Agent:
        provider = self.providers.select()  # raises if none available
        return Agent(
            provider=provider,
            tools=self.tools,
            risk_gate=self.risk_gate,
            kill_switch=self.kill_switch,
            store=self.store,
            max_steps=self.config.get("agent.max_steps", 12),
            max_retries=self.config.get("agent.max_retries", 2),
            on_event=self.on_event,
        )

    def run(self, goal: str) -> AgentResult:
        return self._agent().run(goal)

    def resume(self, task_id: str) -> AgentResult:
        task = self.store.load(task_id)
        if task is None:
            raise ValueError(f"No such task: {task_id}")
        return self._agent().resume(task)

    def stop(self, reason: str = "manual stop", pin: str | None = None) -> bool:
        return self.kill_switch.engage(reason=reason, pin=pin)

    def clear_stop(self) -> None:
        """Clear an engaged stop so tasks can run/resume again."""
        self.kill_switch.reset()
