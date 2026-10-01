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


class VisionUnsupported(ProviderUnavailable):
    """Raised when a provider is asked to look at an image and cannot.

    A subclass of :class:`ProviderUnavailable` on purpose: every caller that already handles "this provider
    cannot serve this request" handles this too, without a new failure path.
    """


class VisionBusy(ProviderUnavailable):
    """The vision model was reachable but temporarily overloaded. Asking again later may well work.

    Distinct from :class:`VisionUnsupported` (which never works) and from a bare failure (which may be
    anything) because the two deserve different things said to the owner: "this cannot be done" against
    "the service is busy, try again in a moment". Measured during development: Gemini returns 503
    UNAVAILABLE with "experiencing high demand" for exactly this, intermittently, on requests whose format
    is otherwise accepted.
    """


class LLMProvider(ABC):
    name: str = "base"

    #: Whether this provider can be asked to describe an image. False by default, so a provider is
    #: text-only until it says otherwise - adding vision to the interface must not quietly imply that
    #: every existing provider has it. Nothing infers this from a model name: it is declared in code.
    supports_vision: bool = False

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

    def describe_image(self, image: bytes, mime_type: str, prompt: str) -> LLMResponse:
        """Describe one image in words. Optional: a text-only provider raises.

        Deliberately NOT abstract. Making it abstract would force every provider - OpenAI, Ollama,
        AgentRouter - to implement or stub a capability it may not have, which is a lot of change for one
        feature and invites a stub that silently drops the image and describes nothing. A provider that
        does not override this declares ``supports_vision = False`` and raises here, and the caller is
        expected to check the flag rather than discover it from an exception.

        This is a one-shot request with no conversation and no tools: an image description must not be
        able to ask for an action. Implementations pass no tool declarations.
        """
        raise VisionUnsupported(f"The '{self.name}' provider cannot analyse images.")
