"""Which provider may perform which capability, using which data, under which conditions.

V.O.I.D already chose providers by *capability*: :meth:`ProviderRegistry.vision` refuses to hand an image
to a text-only model rather than falling back and describing a picture it never saw. This module
generalises that instinct into a policy, because capability is only half the question. The other half is
the data: Gemini being able to read an image does not mean Gemini may receive *this* image, when the image
is the owner's screen.

So every provider decision answers four things together - **provider, capability, data, conditions** - and
the answer comes from configuration, never from a model.

Three properties make this a security boundary rather than a preference list:

**Policy sits below the model.** The flow is

    LLM proposal -> provider policy -> security/permission policy -> capability layer -> provider adapter

and never model -> provider. :meth:`ProviderPolicy.authorize` takes a capability and a set of data classes
determined by *the calling capability layer* - the screen tool knows it holds a screen image - not by tool
arguments. There is no parameter a model could set to widen what it is allowed to do, and no provider name
a model supplies can reach a provider the policy excludes. A model's output is a proposal; this is the
thing that decides.

**A provider gains nothing by being installed.** Capabilities are the intersection of what the provider
declares it can do and what the policy permits it to do. Installing an SDK, or a provider class gaining a
``supports_vision`` attribute, cannot widen authority: the policy is the narrower side and it is written by
the owner.

**Fallback may only preserve or strengthen privacy.** This is where provider chains usually leak. If a
request is local-only, no cloud provider is eligible however unavailable the local one is - V.O.I.D reports
that it cannot do the thing instead of quietly sending the owner's screen to a cloud model. A fallback
target must be permitted for *every* data class the original request carried; being permitted for some of
them is not enough. See :meth:`eligible_chain`.

Deny-by-default throughout: a provider with no profile is denied, a data class no profile allows is denied,
and :data:`DataClass.CREDENTIAL` is denied to everything, always, with no configuration that can permit it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)


class DataClass:
    """Kinds of data a provider might be asked to process, by sensitivity rather than by format.

    The distinctions are the ones that matter for disclosure, which is why "an image" is not one class:
    a camera frame and a screenshot carry very different risks, and the owner may reasonably permit one
    and refuse the other.
    """

    #: Content already public. A web page V.O.I.D fetched, a search result.
    PUBLIC_WEB = "public_web"
    #: What the owner said or typed to V.O.I.D. Theirs, but deliberately addressed to the assistant.
    USER_TEXT = "user_text"
    #: A capture of the owner's display. Holds whatever happened to be open - documents, messages, tokens.
    SCREEN_IMAGE = "screen_image"
    #: A camera frame.
    CAMERA_IMAGE = "camera_image"
    #: Contents of a file on the owner's machine.
    PRIVATE_FILE = "private_file"
    #: Contents of V.O.I.D's persistent memory.
    MEMORY_CONTENT = "memory_content"
    #: Hardware and device inventory - names, models, pairing state.
    DEVICE_INFORMATION = "device_information"
    #: Page content from the owner's authenticated browser session.
    BROWSER_CONTENT = "browser_content"
    #: Message and conversation content from a messaging application.
    MESSAGING_CONTENT = "messaging_content"
    #: Secrets. Present as a NAMED class so that "may a provider receive this?" has an explicit, auditable
    #: answer of no, rather than relying on nobody ever passing one.
    CREDENTIAL = "credential"

    ALL = frozenset({PUBLIC_WEB, USER_TEXT, SCREEN_IMAGE, CAMERA_IMAGE, PRIVATE_FILE,
                     MEMORY_CONTENT, DEVICE_INFORMATION, BROWSER_CONTENT, MESSAGING_CONTENT,
                     CREDENTIAL})

    #: Never permitted to any provider, local or cloud, under any configuration. There is deliberately no
    #: code path that consults the owner's config for this one.
    NEVER = frozenset({CREDENTIAL})

    #: Classes that leaving the machine is a disclosure of the owner's private world. A cloud provider
    #: needs an explicit grant for each of these; being configured for "text" is not a grant.
    SENSITIVE = frozenset({SCREEN_IMAGE, CAMERA_IMAGE, PRIVATE_FILE, MEMORY_CONTENT,
                           BROWSER_CONTENT, MESSAGING_CONTENT, DEVICE_INFORMATION})


class Capability:
    """What a provider is being asked to do."""

    TEXT = "text"
    VISION = "vision"
    SEARCH = "search"
    EMBEDDING = "embedding"
    AUDIO = "audio"

    ALL = frozenset({TEXT, VISION, SEARCH, EMBEDDING, AUDIO})


class Locality:
    """Where a request is permitted to be served from."""

    #: Must not leave the machine. A cloud provider is never eligible, at any position in the chain.
    LOCAL_ONLY = "local_only"
    #: Either is acceptable, subject to the per-class permissions.
    ANY = "any"

    ALL = frozenset({LOCAL_ONLY, ANY})


#: Provider kinds. ``local`` means the computation happens on this machine and nothing is transmitted.
LOCAL = "local"
CLOUD = "cloud"


@dataclass(frozen=True)
class ProviderProfile:
    """What the owner permits one provider to do.

    Note what is NOT here: anything a model could influence. A profile is built from configuration at
    startup and is immutable afterwards.
    """

    name: str
    kind: str = CLOUD
    #: Capabilities the owner permits. Intersected with what the provider declares - the narrower wins.
    capabilities: frozenset = field(default_factory=frozenset)
    #: Data classes this provider may receive. Deny-by-default: an absent class is refused.
    data_classes: frozenset = field(default_factory=frozenset)
    enabled: bool = True
    #: Whether this provider may be used as a FALLBACK. A provider can be perfectly usable as a primary
    #: and still be a poor thing to fail over to silently.
    fallback_eligible: bool = True
    #: Whether using this provider needs the owner to say yes at the time. Independent of data class: an
    #: owner may permit a provider for screen images and still want to be asked each time.
    requires_consent: bool = False
    #: Models the owner permits, or empty for "whatever the provider is configured with".
    allowed_models: frozenset = field(default_factory=frozenset)

    @property
    def is_local(self) -> bool:
        return self.kind == LOCAL

    def permits_class(self, data_class: str) -> bool:
        if data_class in DataClass.NEVER:
            return False
        return data_class in self.data_classes

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind,
                "capabilities": sorted(self.capabilities),
                "data_classes": sorted(self.data_classes),
                "enabled": self.enabled, "fallback_eligible": self.fallback_eligible,
                "requires_consent": self.requires_consent,
                "allowed_models": sorted(self.allowed_models)}


@dataclass(frozen=True)
class Decision:
    """Whether one provider may serve one request, and why.

    The reason is part of the result because "no" has to be sayable to the owner. "I can't look at your
    screen with Gemini because you haven't allowed screen images to leave this machine" is a useful answer;
    a bare refusal is not.
    """

    allowed: bool
    provider: str = ""
    reason: str = ""
    needs_consent: bool = False

    def __bool__(self) -> bool:
        return self.allowed

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "provider": self.provider, "reason": self.reason,
                "needs_consent": self.needs_consent}


@dataclass(frozen=True)
class Request:
    """A provider request, described by the capability layer that holds the data.

    ``data_classes`` is the honest description of what would be sent. The screen tool passes
    ``{SCREEN_IMAGE}`` because that is what it holds; it is not a hint and it is not negotiable.
    """

    capability: str
    data_classes: frozenset = field(default_factory=frozenset)
    locality: str = Locality.ANY
    #: True when the owner has just been asked and agreed, for a provider whose profile requires it.
    consent: bool = False
    #: A description for logs. Never the data itself.
    purpose: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_classes", frozenset(self.data_classes or ()))
        if self.locality not in Locality.ALL:
            object.__setattr__(self, "locality", Locality.ANY)

    @property
    def sensitive(self) -> bool:
        return bool(self.data_classes & DataClass.SENSITIVE)


class ProviderPolicy:
    """The deterministic answer to "may this provider do this, with this data, now?".

    Holds no provider objects and performs no calls. It decides; the registry executes. That separation is
    what lets the policy be tested exhaustively without a network and reasoned about without reading an
    adapter.
    """

    def __init__(self, profiles=None, default_locality: str = Locality.ANY):
        # ``None`` means the shipped policy; an explicit empty sequence means deny everything, which is a
        # legitimate configuration and must not be silently replaced by the defaults.
        if profiles is None:
            profiles = DEFAULT_PROFILES
        self._profiles: dict[str, ProviderProfile] = {
            profile.name: profile for profile in profiles}
        self._default_locality = (default_locality if default_locality in Locality.ALL
                                  else Locality.ANY)

    # -- construction --
    @classmethod
    def from_config(cls, config) -> "ProviderPolicy":
        """Build from ``providers.*`` configuration, falling back to the shipped defaults.

        An unknown provider name in configuration produces a profile that permits nothing, rather than
        being ignored: a typo should make a provider unusable, not silently unrestricted.
        """
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default

        raw = get("providers.policy", {}) or {}
        profiles: list[ProviderProfile] = []
        if isinstance(raw, dict):
            for name, entry in raw.items():
                if not isinstance(name, str) or not name.strip():
                    continue
                entry = entry if isinstance(entry, dict) else {}
                kind = str(entry.get("kind", CLOUD)).strip().lower()
                profiles.append(ProviderProfile(
                    name=name.strip().lower(),
                    kind=LOCAL if kind == LOCAL else CLOUD,
                    capabilities=_as_set(entry.get("capabilities"), Capability.ALL),
                    data_classes=_as_set(entry.get("data_classes"), DataClass.ALL - DataClass.NEVER),
                    enabled=bool(entry.get("enabled", True)),
                    fallback_eligible=bool(entry.get("fallback_eligible", True)),
                    requires_consent=bool(entry.get("requires_consent", False)),
                    allowed_models=_as_set(entry.get("allowed_models"), None)))
        default_locality = str(get("providers.default_locality", Locality.ANY)).strip().lower()
        return cls(profiles=profiles or DEFAULT_PROFILES, default_locality=default_locality)

    # -- inspection --
    def profile(self, name: str) -> ProviderProfile | None:
        return self._profiles.get((name or "").strip().lower())

    def names(self) -> list[str]:
        return sorted(self._profiles)

    def describe(self) -> list[dict]:
        return [self._profiles[name].as_dict() for name in sorted(self._profiles)]

    # -- the decision --
    def authorize(self, provider: str, request: Request, declared=None) -> Decision:
        """May ``provider`` serve ``request``?

        ``declared`` is what the provider object itself says it can do, if known. It can only NARROW the
        answer: a provider that does not declare vision is refused a vision request even if the policy
        would permit it, and a provider that declares vision still needs the policy's permission. Neither
        side can widen the other.
        """
        name = (provider or "").strip().lower()
        profile = self._profiles.get(name)
        if profile is None:
            return Decision(False, name, "it is not in the provider policy")
        if not profile.enabled:
            return Decision(False, name, "it is disabled in the provider policy")

        # Credentials first: a hard rule, checked before anything configurable.
        forbidden = request.data_classes & DataClass.NEVER
        if forbidden:
            _log.warning("PROVIDER_POLICY_REFUSED_CREDENTIAL provider=%s", name)
            return Decision(False, name, "credentials are never sent to any provider")

        capability = (request.capability or "").strip().lower()
        if capability not in Capability.ALL:
            return Decision(False, name, f"'{request.capability}' is not a capability V.O.I.D governs")
        if capability not in profile.capabilities:
            return Decision(False, name, f"it is not permitted to do {capability} work")
        if declared is not None and capability not in frozenset(declared):
            return Decision(False, name, f"it cannot actually do {capability} work")

        if request.locality == Locality.LOCAL_ONLY and not profile.is_local:
            return Decision(False, name, "this has to stay on the machine and that provider is remote")

        missing = {data_class for data_class in request.data_classes
                   if not profile.permits_class(data_class)}
        if missing:
            readable = ", ".join(sorted(missing)).replace("_", " ")
            where = "leave this machine" if not profile.is_local else "be used that way"
            return Decision(False, name, f"you have not allowed {readable} to {where}")

        if profile.requires_consent and not request.consent:
            return Decision(False, name, "it needs your go-ahead first", needs_consent=True)
        return Decision(True, name, "permitted by the provider policy")

    def eligible_chain(self, candidates, request: Request, declared_for=None) -> list[str]:
        """The providers that may serve ``request``, in the order given, fallbacks included.

        The first permitted candidate is the primary. Every candidate after it must additionally be
        ``fallback_eligible``, so failing over is a separate permission from being usable.

        Because every member of the chain is authorized against the *same* request - the same data classes
        and the same locality - a fallback cannot be weaker than the primary. There is no code path that
        relaxes the request to find something that will accept it, which is the mistake that makes
        provider chains leak.
        """
        chain: list[str] = []
        for candidate in candidates or ():
            name = (candidate or "").strip().lower()
            declared = declared_for(name) if callable(declared_for) else None
            decision = self.authorize(name, request, declared=declared)
            if not decision.allowed:
                continue
            if chain:
                profile = self._profiles.get(name)
                if profile is None or not profile.fallback_eligible:
                    continue
            chain.append(name)
        return chain

    def refusal(self, candidates, request: Request, declared_for=None) -> str:
        """Why nothing could serve this request - the first real reason, for saying to the owner."""
        reasons: list[str] = []
        for candidate in candidates or ():
            name = (candidate or "").strip().lower()
            declared = declared_for(name) if callable(declared_for) else None
            decision = self.authorize(name, request, declared=declared)
            if decision.allowed:
                return ""
            reasons.append(f"{name}: {decision.reason}")
        if not reasons:
            return "No provider is configured for that."
        return "; ".join(reasons[:4])


def _as_set(value, allowed) -> frozenset:
    """Coerce a config value to a set of lowercase strings, keeping only recognised entries.

    Unrecognised entries are dropped rather than carried: a policy containing a capability V.O.I.D does not
    govern would read as permission for something nobody implemented.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    items = {str(entry).strip().lower() for entry in value if str(entry).strip()}
    if allowed is not None:
        items &= set(allowed)
    return frozenset(items)


#: The shipped policy, used when nothing is configured. Deliberately conservative: the local provider may
#: see the owner's private world, and the cloud providers may not - not screen captures, not file contents,
#: not memory, not browser or messaging content. An owner who wants cloud vision over their screen turns it
#: on for the specific provider and the specific class, in their own config file.
#:
#: This mirrors what V2 and V3 already do elsewhere: ``camera.allow_cloud_analysis`` and
#: ``screen.allow_cloud_analysis`` are both false by default. The policy makes that the default for every
#: provider and every sensitive class at once, instead of one switch per capability.
DEFAULT_PROFILES = (
    ProviderProfile(
        name="local", kind=LOCAL,
        capabilities=frozenset({Capability.TEXT, Capability.VISION, Capability.EMBEDDING}),
        # A local model processes on this machine, so nothing is disclosed by using it.
        data_classes=frozenset(DataClass.ALL - DataClass.NEVER),
        fallback_eligible=True),
    ProviderProfile(
        name="gemini", kind=CLOUD,
        capabilities=frozenset({Capability.TEXT, Capability.VISION}),
        data_classes=frozenset({DataClass.PUBLIC_WEB, DataClass.USER_TEXT}),
        fallback_eligible=True),
    ProviderProfile(
        name="openai", kind=CLOUD,
        capabilities=frozenset({Capability.TEXT, Capability.EMBEDDING}),
        data_classes=frozenset({DataClass.PUBLIC_WEB, DataClass.USER_TEXT}),
        fallback_eligible=True),
    ProviderProfile(
        name="anthropic", kind=CLOUD,
        capabilities=frozenset({Capability.TEXT, Capability.VISION}),
        data_classes=frozenset({DataClass.PUBLIC_WEB, DataClass.USER_TEXT}),
        fallback_eligible=True),
    ProviderProfile(
        name="agentrouter", kind=CLOUD,
        capabilities=frozenset({Capability.TEXT}),
        data_classes=frozenset({DataClass.PUBLIC_WEB, DataClass.USER_TEXT}),
        fallback_eligible=True),
    # Research providers. They receive the SEARCH QUERY, which is derived from what the owner asked, and
    # they return public web content. They are not permitted anything else.
    ProviderProfile(
        name="searxng", kind=CLOUD,
        capabilities=frozenset({Capability.SEARCH}),
        data_classes=frozenset({DataClass.PUBLIC_WEB, DataClass.USER_TEXT}),
        fallback_eligible=True),
    ProviderProfile(
        name="marginalia", kind=CLOUD,
        capabilities=frozenset({Capability.SEARCH}),
        data_classes=frozenset({DataClass.PUBLIC_WEB, DataClass.USER_TEXT}),
        fallback_eligible=True),
    ProviderProfile(
        name="searxng-local", kind=LOCAL,
        capabilities=frozenset({Capability.SEARCH}),
        data_classes=frozenset(DataClass.ALL - DataClass.NEVER),
        fallback_eligible=True),
)
