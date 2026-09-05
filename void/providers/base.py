"""Provider-neutral types and the LLMProvider interface.

Keeping this layer thin and neutral is what makes AI providers replaceable:
the agent speaks only in these types, and each provider translates to and
from its own API. Messages use a small normalized shape::

    {"role": "system"|"user"|"assistant"|"tool", "content": str,
     "tool_call_id": <optional>, "name": <optional tool name>}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    """A tool description handed to the model (JSON-schema parameters)."""
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """A model's request to call a tool.

    ``signature`` is an opaque, provider-specific token that some models (e.g.
    Gemini 3.x "thought_signature") attach to a function call and require to be
    echoed back on the next turn. It is stored as a base64 string so it survives
    JSON checkpointing. Providers that don't use it leave it None.
    """
    name: str
    arguments: dict
    id: str | None = None
    signature: str | None = None


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot serve a request (no key, offline, ...)."""


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> bool:
        """True if the provider can currently serve requests."""

    @abstractmethod
    def generate(
        self,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
    ) -> LLMResponse:
        """Produce the next assistant turn (text and/or tool calls)."""
