"""Search providers as replaceable, governed, observable things.

The first research implementation hardcoded two endpoint templates in a tuple. That was honest about what
it was, but it made three things impossible: knowing *why* a lookup came back thin, failing over on
evidence rather than on order, and answering "may this provider be used for this?" at all. This module
fixes all three without replacing the research engine - :class:`~void.research.ResearchEngine` keeps its
shape and gains a provider set.

**Providers are described, not assumed.** A provider carries its endpoint, timeout, priority, whether it is
local or remote, and whether it may be failed over to. Nothing about it is inferred from its name.

**Selection is governed.** Every provider is authorized through
:class:`~void.providers.policy.ProviderPolicy` before it is used, with the real data classes involved: a
search sends the owner's query (:data:`~void.providers.policy.DataClass.USER_TEXT`) and receives public web
content. A local-only research request can therefore use a self-hosted instance and nothing else - the
fallback cannot quietly cross from a local index to a remote one, because every candidate is authorized
against the same request. That is the policy's guarantee, reused rather than re-implemented here.

**Health is measured, not guessed.** Attempts, successes, failures, latency and the last failure reason are
recorded per provider, and a provider that keeps failing is rested for a while instead of being tried first
every time. This is the honest answer to providers that refuse automated access: V.O.I.D notices, says so,
and moves on.

**What this deliberately does NOT do.** It does not retry past a refusal, rotate identities, vary user
agents, solve challenges, or work around rate limits. A provider that declines automated access has
declined, and the correct behaviour is to record the reason and use a provider that permits it. Several
major engines refuse, which is exactly why this is a replaceable set rather than one hardcoded endpoint.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from void.perception import clean_text
from void.providers.policy import Capability, DataClass, Locality
from void.providers.policy import Request as PolicyRequest

_log = logging.getLogger(__name__)

LOCAL = "local"
CLOUD = "cloud"

#: Consecutive failures before a provider is rested.
FAILURES_BEFORE_REST = 3

#: How long a rested provider is skipped. Long enough to stop hammering something that is refusing, short
#: enough that a transient outage does not remove a provider for the session.
REST_SECONDS = 300.0


@dataclass(frozen=True)
class SearchProvider:
    """One search endpoint V.O.I.D may ask, and the terms on which it may be asked.

    ``name`` must match a profile in the provider policy, because that is what makes this governable: an
    endpoint whose name has no profile is refused rather than used ungoverned.
    """

    name: str
    #: URL template containing ``{query}``. The query is URL-encoded by the caller.
    endpoint: str = ""
    enabled: bool = True
    #: Lower runs first. Ties break on configuration order.
    priority: int = 100
    timeout_s: float = 20.0
    kind: str = CLOUD
    #: May this provider be used as a FALLBACK, as opposed to only as a primary.
    fallback_eligible: bool = True
    #: A human note for the config and for reports. Never used for a decision.
    note: str = ""

    @property
    def is_local(self) -> bool:
        return self.kind == LOCAL

    @property
    def usable(self) -> bool:
        """Structurally usable: enabled, and an endpoint that can actually take a query."""
        return bool(self.enabled and self.endpoint and "{query}" in self.endpoint)

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "enabled": self.enabled,
                "priority": self.priority, "timeout_s": self.timeout_s,
                "fallback_eligible": self.fallback_eligible, "usable": self.usable,
                "note": self.note}


@dataclass
class ProviderHealth:
    """What has actually happened when V.O.I.D asked this provider.

    Kept in memory for the session only. Health is an observation about now, and persisting it would mean
    a provider that was down yesterday starts today distrusted for reasons nobody can see.
    """

    name: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    #: The CLASS of the last failure, or a short reason. Never a response body.
    last_failure: str = ""
    rested_until: float = 0.0

    def record_success(self, latency_ms: float, results: int) -> None:
        self.attempts += 1
        self.successes += 1
        self.consecutive_failures = 0
        self.last_latency_ms = latency_ms
        self.total_latency_ms += latency_ms
        self.rested_until = 0.0
        _log.info("SEARCH_PROVIDER_OK provider=%s ms=%.0f results=%d",
                  self.name, latency_ms, results)

    def record_failure(self, reason: str, latency_ms: float = 0.0) -> None:
        self.attempts += 1
        self.failures += 1
        self.consecutive_failures += 1
        self.last_latency_ms = latency_ms
        self.total_latency_ms += latency_ms
        self.last_failure = clean_text(reason, 120)
        if self.consecutive_failures >= FAILURES_BEFORE_REST:
            self.rested_until = time.time() + REST_SECONDS
            _log.info("SEARCH_PROVIDER_RESTED provider=%s consecutive=%d",
                      self.name, self.consecutive_failures)

    def resting(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) < self.rested_until

    @property
    def reliability(self) -> float:
        """Successes over attempts, or 1.0 for a provider never tried.

        An untried provider is optimistic on purpose: pessimism would mean a newly configured provider
        never gets a turn.
        """
        return 1.0 if not self.attempts else self.successes / self.attempts

    @property
    def mean_latency_ms(self) -> float:
        return 0.0 if not self.attempts else self.total_latency_ms / self.attempts

    def as_dict(self) -> dict:
        return {"name": self.name, "attempts": self.attempts, "successes": self.successes,
                "failures": self.failures, "consecutive_failures": self.consecutive_failures,
                "reliability": round(self.reliability, 3),
                "mean_latency_ms": round(self.mean_latency_ms, 1),
                "last_failure": self.last_failure, "resting": self.resting()}


class SearchProviderSet:
    """The configured search providers, ordered by policy, priority and health."""

    def __init__(self, providers=None, policy=None):
        self._providers: list[SearchProvider] = list(providers or ())
        self._policy = policy
        self._health: dict[str, ProviderHealth] = {
            provider.name: ProviderHealth(name=provider.name) for provider in self._providers}

    # -- construction --
    @classmethod
    def from_config(cls, config, policy=None) -> "SearchProviderSet":
        """Build from ``research.providers``, falling back to the shipped set.

        Also accepts the older ``research.search_endpoints`` list of templates, so an existing
        configuration keeps working: each template becomes a provider named after its host.
        """
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default

        built: list[SearchProvider] = []
        raw = get("research.providers", None)
        if isinstance(raw, dict):
            for index, (name, entry) in enumerate(raw.items()):
                if not isinstance(name, str) or not name.strip():
                    continue
                entry = entry if isinstance(entry, dict) else {}
                kind = str(entry.get("kind", CLOUD)).strip().lower()
                try:
                    priority = int(entry.get("priority", 100 + index))
                except (TypeError, ValueError):
                    priority = 100 + index
                try:
                    timeout = min(max(float(entry.get("timeout_s", 20.0)), 1.0), 120.0)
                except (TypeError, ValueError):
                    timeout = 20.0
                built.append(SearchProvider(
                    name=name.strip().lower(),
                    endpoint=str(entry.get("endpoint", "") or "").strip(),
                    enabled=bool(entry.get("enabled", True)),
                    priority=priority, timeout_s=timeout,
                    kind=LOCAL if kind == LOCAL else CLOUD,
                    fallback_eligible=bool(entry.get("fallback_eligible", True)),
                    note=str(entry.get("note", "") or "")[:200]))
        if not built:
            legacy = get("research.search_endpoints", None)
            if isinstance(legacy, (list, tuple)):
                built = [_from_template(template, 100 + index)
                         for index, template in enumerate(legacy)
                         if isinstance(template, str) and "{query}" in template]
                built = [provider for provider in built if provider is not None]
        return cls(providers=built or list(DEFAULT_PROVIDERS), policy=policy)

    # -- inspection --
    def all(self) -> list[SearchProvider]:
        return list(self._providers)

    def health(self, name: str) -> ProviderHealth:
        return self._health.setdefault(name, ProviderHealth(name=name))

    def report(self) -> list[dict]:
        """Every provider with its configuration and its measured health, for honest reporting."""
        out = []
        for provider in self._providers:
            row = provider.as_dict()
            row["health"] = self.health(provider.name).as_dict()
            out.append(row)
        return out

    # -- selection --
    def ordered(self, locality: str = Locality.ANY, now: float | None = None) -> list[SearchProvider]:
        """Providers that may be asked, best first.

        Order: policy-permitted, structurally usable and not resting, then by priority, then by measured
        reliability, then by mean latency. Resting providers are appended at the end rather than dropped -
        if every healthy provider is exhausted, trying a rested one beats telling the owner nothing could
        be done.
        """
        permitted = [provider for provider in self._providers
                     if provider.usable and self.permits(provider, locality)]
        permitted.sort(key=lambda provider: (
            provider.priority,
            -self.health(provider.name).reliability,
            self.health(provider.name).mean_latency_ms))
        fresh = [provider for provider in permitted if not self.health(provider.name).resting(now)]
        rested = [provider for provider in permitted if self.health(provider.name).resting(now)]
        chain = fresh + rested
        # Being usable as a primary and being safe to fail over to are separate permissions.
        if len(chain) > 1:
            chain = [chain[0]] + [provider for provider in chain[1:] if provider.fallback_eligible]
        return chain

    def permits(self, provider: SearchProvider, locality: str = Locality.ANY) -> bool:
        """Whether the provider policy allows asking this provider at all."""
        decision = self.decide(provider, locality)
        return bool(decision is None or decision.allowed)

    def decide(self, provider: SearchProvider, locality: str = Locality.ANY):
        """The policy decision for this provider, or None when no policy is wired.

        A search transmits the owner's query and brings back public web content, so those are the data
        classes declared. Declaring them honestly is what lets an owner refuse a remote search engine
        without having to refuse research altogether.
        """
        if self._policy is None:
            return None
        return self._policy.authorize(
            provider.name,
            PolicyRequest(capability=Capability.SEARCH,
                          data_classes={DataClass.USER_TEXT, DataClass.PUBLIC_WEB},
                          locality=locality, purpose="web search"),
            declared={Capability.SEARCH})

    def refusals(self, locality: str = Locality.ANY) -> list[str]:
        """Why unusable providers are unusable, in words. For reporting, never for a decision."""
        out: list[str] = []
        for provider in self._providers:
            if not provider.enabled:
                out.append(f"{provider.name} is disabled")
                continue
            if not provider.usable:
                out.append(f"{provider.name} has no usable endpoint")
                continue
            decision = self.decide(provider, locality)
            if decision is not None and not decision.allowed:
                out.append(f"{provider.name}: {decision.reason}")
        return out


def _from_template(template: str, priority: int) -> SearchProvider | None:
    """Turn a bare endpoint template into a named provider, for backward compatibility."""
    try:
        from urllib.parse import urlparse
        host = (urlparse(template).netloc or "").lower()
    except Exception:                                           # noqa: BLE001
        return None
    if not host:
        return None
    name = host.split(":")[0]
    for suffix in (".nu", ".site", ".com", ".org", ".net", ".be", ".io"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.replace("search.", "").replace("www.", "").strip(".") or host
    local = host.startswith("127.0.0.1") or host.startswith("localhost")
    return SearchProvider(name=f"{name}-local" if local else name, endpoint=template,
                          priority=priority, kind=LOCAL if local else CLOUD,
                          note="from research.search_endpoints")


#: The shipped providers. Measured against the live web: Google, Bing and both DuckDuckGo HTML endpoints
#: refuse automated navigation, and Startpage and Mojeek answer 403, so none of them is here - V.O.I.D
#: does not try to defeat an access control. These two permit programmatic access.
#:
#: ``searxng-local`` is first and disabled: an owner running their own SearXNG only has to enable it, and
#: it is marked ``local`` so a local-only research request can be served without anything leaving the
#: machine. That is the configuration this architecture exists to make easy.
DEFAULT_PROVIDERS = (
    SearchProvider(name="searxng-local", endpoint="http://127.0.0.1:8888/search?q={query}",
                   enabled=False, priority=10, kind=LOCAL,
                   note="your own SearXNG; nothing leaves the machine. Enable to prefer it."),
    SearchProvider(name="searxng", endpoint="https://searxng.site/search?q={query}",
                   priority=20, kind=CLOUD,
                   note="public SearXNG instance; aggregates the major engines"),
    SearchProvider(name="marginalia", endpoint="https://search.marginalia.nu/search?query={query}",
                   priority=30, kind=CLOUD,
                   note="independent index; good for documents, weak for news"),
)
