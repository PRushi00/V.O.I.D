"""``propose_memory``: the ONLY way a model can touch memory - and it can only SUGGEST.

The handler calls ``MemoryService.propose``, which structurally cannot produce an ``active``
item: a proposal lands ``proposed`` (owner reviews it) or ``quarantined`` (the run has read
tool output, or the text reads like a permission claim). The model can neither read
memory through this tool, nor see quarantined items, nor learn whether a similar memory
already exists (duplicates and new proposals get the same reply). The provenance that decides
the outcome - the run's taint flag - comes from the agent loop via a context variable; the
model supplies only ``text`` and ``kind``.

The tool is LOW risk because it changes nothing that any V.O.I.D behaviour depends on:
memory is context, never authority.
"""
from __future__ import annotations

import logging

from void.actions.base import Tool, ToolResult
from void.memory import scope
from void.memory.crypto import MemoryUnavailable
from void.memory.policy import MAX_TEXT_CHARS
from void.security.risk import RiskLevel

_log = logging.getLogger(__name__)

RECORDED = "Suggestion recorded for the owner to review. It is not active memory and changes nothing."
NOT_STORED = "That suggestion was not stored."
LIMIT = "Suggestion limit reached for this task."
UNAVAILABLE = "Memory is unavailable."


def make_tool(service) -> Tool:
    def handler(text: str, kind: str = "fact") -> ToolResult:
        run = scope.current_scope()
        tainted = True if run is None else run.tainted          # no scope -> fail closed
        cap = service.settings.max_proposals_per_task
        if run is not None:
            if run.proposals >= cap:
                return ToolResult.failure(LIMIT)
            run.proposals += 1
        try:
            res = service.propose(str(text), kind=str(kind), tainted=tainted,
                                  task_id=run.task_id if run else None)
        except MemoryUnavailable:
            return ToolResult.failure(UNAVAILABLE)
        except Exception:
            _log.exception("MEMORY_PROPOSE_FAILED")
            return ToolResult.failure(NOT_STORED)
        return ToolResult.failure(NOT_STORED) if res.status == "rejected" else ToolResult.success(RECORDED)

    return Tool(
        name=scope.PROPOSE_TOOL,
        description=("Suggest a durable fact or preference about the owner for later recall (e.g. a stated "
                     "preference or project). Only for information the owner clearly stated. It is only a "
                     "suggestion: the owner reviews it, and it never takes effect, grants a permission or "
                     "changes any rule. Never include secrets."),
        parameters={"type": "object",
                    "properties": {"text": {"type": "string", "maxLength": MAX_TEXT_CHARS,
                                            "description": "The fact or preference, as one short statement."},
                                   "kind": {"type": "string", "enum": ["preference", "fact", "episode"]}},
                    "required": ["text"]},
        handler=handler,
        risk=RiskLevel.LOW,
    )
