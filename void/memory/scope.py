"""Run-scoped provenance for memory writes (a minimal V2.0 stand-in for the P1 taint model).

Two facts about the *current* operation must be known where a memory write happens, and
neither may ever be supplied by the model:

  * the CHANNEL the request came from (``cli``/``ui`` = the owner typing; ``voice`` = a
    speech transcript, which can be misheard or spoken by someone else);
  * whether the current agent run is TAINTED - has already consumed tool output (which can
    contain file contents, filenames or window titles an attacker controls).

Both live in context variables set by V.O.I.D's own code (the voice session, the agent
loop). The ``propose_memory`` tool handler reads them; the model can only pass text.
Absent a scope the run is treated as tainted (fail closed).
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field

OWNER_CHANNELS = frozenset({"cli", "ui"})

_channel: contextvars.ContextVar[str] = contextvars.ContextVar("void_memory_channel", default="cli")


@contextlib.contextmanager
def use_channel(name: str):
    token = _channel.set(name)
    try:
        yield
    finally:
        _channel.reset(token)


def current_channel() -> str:
    return _channel.get()


@dataclass
class RunScope:
    """Provenance of one agent run. ``tainted`` only ever goes False -> True."""
    task_id: str | None = None
    tainted: bool = False
    proposals: int = 0
    channel: str = field(default_factory=current_channel)

    def taint(self) -> None:
        self.tainted = True


_scope: contextvars.ContextVar[RunScope | None] = contextvars.ContextVar("void_memory_scope", default=None)


@contextlib.contextmanager
def bind(scope: RunScope | None):
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> RunScope | None:
    return _scope.get()

PROPOSE_TOOL = "propose_memory"      # the only tool through which a model can touch memory (proposal-only)
