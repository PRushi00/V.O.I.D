"""Tool interface shared by every action V.O.I.D can perform.

A :class:`Tool` bundles a callable with the metadata the LLM needs to decide
when and how to call it (name, description, JSON-schema parameters) plus a
risk level the security gate uses to decide autonomy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from void.security.risk import RiskLevel


@dataclass
class ToolResult:
    """Outcome of running a tool. ``summary`` is fed back to the LLM."""
    ok: bool
    summary: str
    data: Any = None
    error: str | None = None

    @classmethod
    def success(cls, summary: str, data: Any = None) -> "ToolResult":
        return cls(ok=True, summary=summary, data=data)

    @classmethod
    def failure(cls, summary: str, error: str | None = None) -> "ToolResult":
        return cls(ok=False, summary=summary, error=error or summary)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON-schema object describing arguments
    handler: Callable[..., ToolResult]
    risk: RiskLevel = RiskLevel.LOW
    # Optional per-call risk: lets a tool raise its risk based on the actual
    # arguments (e.g. writing to a file that ALREADY exists is higher risk than
    # creating a new one). Returns a RiskLevel given the call arguments.
    risk_fn: "Callable[[dict], RiskLevel] | None" = None
    # True ONLY for tools whose success needs no LLM interpretation - a fire-
    # and-forget action (launch an app, open a path) where the ToolResult
    # summary already IS the complete, final thing to tell the owner. False
    # (the default) for every tool that returns information the LLM might
    # need to read, choose between, or reason about next (find_app,
    # search_files, list_directory, ...). The agent loop uses this - ONLY
    # under narrow, additional conditions of its own - to skip the extra
    # "final answer" LLM call for a genuinely simple one-shot action; it
    # never affects authorization, RiskGate, or execution itself.
    terminal_on_success: bool = False

    def effective_risk(self, arguments: dict) -> RiskLevel:
        """Risk for this specific call. Falls back to the static level.

        If a risk_fn raises, we treat the call as HIGH (fail safe) so an
        error never silently downgrades a dangerous action.
        """
        if self.risk_fn is not None:
            try:
                return self.risk_fn(arguments or {})
            except Exception:
                return RiskLevel.HIGH
        return self.risk

    def run(self, **kwargs) -> ToolResult:
        try:
            return self.handler(**kwargs)
        except TypeError as exc:
            # Bad/missing arguments from the model - report, don't crash.
            return ToolResult.failure(
                f"Invalid arguments for '{self.name}': {exc}", error=str(exc)
            )
