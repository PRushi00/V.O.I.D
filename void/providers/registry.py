"""Provider registry: builds providers from config and picks an available one.

Selection order is ``llm.primary`` followed by ``llm.fallback``. The first
provider whose ``available()`` returns True is used. This is what lets the
system prefer Gemini but fall back to the local model when offline.
"""
from __future__ import annotations

from void.providers.base import LLMProvider, ProviderUnavailable


class ProviderRegistry:
    def __init__(self, providers: dict[str, LLMProvider], order: list[str]):
        self._providers = providers
        self._order = order

    @classmethod
    def from_config(cls, cfg) -> "ProviderRegistry":
        from void.providers.gemini_provider import DEFAULT_TIMEOUT_S, GeminiProvider
        from void.providers.agentrouter_provider import (
            DEFAULT_MODEL as AGENTROUTER_MODEL,
            AgentRouterProvider,
        )
        from void.providers.local_provider import LocalProvider
        from void.providers.openai_provider import (
            DEFAULT_MODEL as OPENAI_MODEL,
            DEFAULT_TIMEOUT_S as OPENAI_TIMEOUT_S,
            OpenAIProvider,
        )
        from void.security.credentials import CredentialPool

        providers: dict[str, LLMProvider] = {
            # The primary online brain. Credentials come from OPENAI_API_KEY[_BACKUP_1..4] (never from config).
            # A third-party OpenAI-compatible gateway with its own identity and credential (AGENTROUTER_API_KEY). It is
            # registered, but is only in the provider order if llm.primary / llm.fallback names it.
            "agentrouter": AgentRouterProvider(
                model=cfg.get("llm.agentrouter.model", AGENTROUTER_MODEL),
                timeout_s=cfg.get("llm.agentrouter.timeout_s", OPENAI_TIMEOUT_S),
            ),
            "openai": OpenAIProvider(
                model=cfg.get("llm.openai.model", OPENAI_MODEL),
                timeout_s=cfg.get("llm.openai.timeout_s", OPENAI_TIMEOUT_S),
            ),
            "gemini": GeminiProvider(
                model=cfg.get("llm.gemini.model", "gemini-1.5-flash"),
                temperature=cfg.get("llm.gemini.temperature", 0.2),
                max_output_tokens=cfg.get("llm.gemini.max_output_tokens", 2048),
                credential_pool=CredentialPool(),
                timeout_s=cfg.get("llm.gemini.timeout_s", DEFAULT_TIMEOUT_S),
                thinking=cfg.get("llm.gemini.thinking", None),
                # Empty/unset means "use llm.gemini.model". See GeminiProvider.vision_model for why
                # image requests get their own setting.
                vision_model=cfg.get("llm.gemini.vision_model", None) or None,
            ),
            "local": LocalProvider(
                base_url=cfg.get("llm.local.base_url", "http://localhost:11434"),
                model=cfg.get("llm.local.model", "llama3.1:8b"),
                temperature=cfg.get("llm.local.temperature", 0.2),
                timeout=cfg.get("llm.local.timeout_s", 120.0),
                think=cfg.get("llm.local.think", None),
                num_ctx=cfg.get("llm.local.num_ctx", 8192),
                keep_alive=cfg.get("llm.local.keep_alive", "10m"),
            ),
        }
        primary = cfg.get("llm.primary", "openai")
        fallback = cfg.get("llm.fallback", []) or []
        order = [primary] + [f for f in fallback if f != primary]
        return cls(providers, order)

    def names(self) -> list[str]:
        """Configured provider names, in preference order. Mirrors ``ToolRegistry.names()``; probes nothing."""
        return [n for n in self._order if n in self._providers]

    def get(self, name: str) -> LLMProvider | None:
        return self._providers.get(name)

    def available_order(self) -> list[LLMProvider]:
        """The provider to start with, followed by the ones that could take over.

        Only providers up to and including the first available one are probed. Asking a fallback whether it is
        ready costs a round trip it does not owe anybody until it is actually needed - on this machine the Ollama
        probe was 2 s, paid by every request including the ones Gemini answered perfectly well. If a fallback
        turns out to be unavailable when the time comes, it raises ``ProviderUnavailable`` and the ordinary
        failure classification moves on, which is precisely what that machinery is for.
        """
        configured = [p for p in (self._providers.get(n) for n in self._order) if p is not None]
        for i, provider in enumerate(configured):
            if provider.available():
                return configured[i:]
        return []

    def vision(self) -> LLMProvider:
        """The first available provider that can actually look at an image, in the configured order.

        Separate from :meth:`select` because the ordinary fallback chain is the wrong behaviour here, and
        dangerously so. ``llm.primary`` defaults to a text-only provider on this machine, and a fallback
        that quietly accepted an image request would either drop the image and describe nothing, or
        describe it from the prompt alone - a confident answer about a picture it never saw. So this
        filters on the declared ``supports_vision`` capability and raises if none of the configured
        providers has it. It never substitutes a text provider, and it never sends an image to a provider
        that did not declare it can receive one.
        """
        considered: list[str] = []
        for name in self._order:
            provider = self._providers.get(name)
            if provider is None or not getattr(provider, "supports_vision", False):
                continue
            considered.append(name)
            if provider.available():
                return provider
        if not considered:
            raise ProviderUnavailable(
                "None of the configured providers can analyse an image.")
        raise ProviderUnavailable(
            f"No provider that can analyse an image is available. Tried: {', '.join(considered)}.")

    def select(self) -> LLMProvider:
        """Return the first available provider in priority order."""
        tried = []
        for name in self._order:
            provider = self._providers.get(name)
            if provider is None:
                continue
            tried.append(name)
            if provider.available():
                return provider
        raise ProviderUnavailable(
            f"No LLM provider available. Tried: {', '.join(tried) or 'none'}. "
            f"Set OPENAI_API_KEY (or a Gemini key with 'python -m void set-key gemini') or start Ollama."
        )
