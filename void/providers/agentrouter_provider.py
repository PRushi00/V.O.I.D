"""AgentRouter provider - a THIRD-PARTY OpenAI-compatible gateway (https://agentrouter.org/v1), not OpenAI.

It is the same client as :class:`~void.providers.openai_provider.OpenAIProvider` (one HTTP / classification / rotation
implementation) with its own identity: provider name ``agentrouter``, its own credential slot ``AGENTROUTER_API_KEY`` (environment,
then the OS keyring key ``agentrouter_api_key``), a fixed endpoint and a host allow-list of exactly ``agentrouter.org``. There is a
single credential, so there is no rotation: a credential-specific failure ends the call. Everything the gateway returns is external,
untrusted data; the Agent / RiskGate / capability engine remain the authority over any tool call it proposes.
"""
from __future__ import annotations

from void.providers.openai_provider import OpenAIProvider

AGENTROUTER_BASE = "https://agentrouter.org/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
SLOT_NAME = "AGENTROUTER_API_KEY"


class AgentRouterProvider(OpenAIProvider):
    name = "agentrouter"
    _display = "AgentRouter"
    _slot_names = (SLOT_NAME,)
    _slot_labels = {SLOT_NAME: "primary"}
    _manifest_key = "agentrouter_credential_slots"
    _default_base = AGENTROUTER_BASE
    _allowed_hosts = frozenset({"agentrouter.org"})

    def __init__(self, model: str = DEFAULT_MODEL, timeout_s: float = 60.0, credential_pool=None,
                 api_base: str | None = None):
        super().__init__(model=model, timeout_s=timeout_s, credential_pool=credential_pool, api_base=api_base)
