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
        from void.providers.gemini_provider import GeminiProvider
        from void.providers.local_provider import LocalProvider
        from void.security.credentials import CredentialPool

        providers: dict[str, LLMProvider] = {
            "gemini": GeminiProvider(
                model=cfg.get("llm.gemini.model", "gemini-1.5-flash"),
                temperature=cfg.get("llm.gemini.temperature", 0.2),
                max_output_tokens=cfg.get("llm.gemini.max_output_tokens", 2048),
                credential_pool=CredentialPool(),
            ),
            "local": LocalProvider(
                base_url=cfg.get("llm.local.base_url", "http://localhost:11434"),
                model=cfg.get("llm.local.model", "llama3.1:8b"),
                temperature=cfg.get("llm.local.temperature", 0.2),
            ),
        }
        primary = cfg.get("llm.primary", "gemini")
        fallback = cfg.get("llm.fallback", []) or []
        order = [primary] + [f for f in fallback if f != primary]
        return cls(providers, order)

    def get(self, name: str) -> LLMProvider | None:
        return self._providers.get(name)

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
            f"Set a Gemini key (python -m void set-key gemini) or start Ollama."
        )
