"""Which model ids really exist, and the refusal that makes naming one by voice safe.

The failure this prevents is specific, and every prompt pack in ``docs/PROMPT_PACK_SOURCES.md`` tells
the same story about it: the owner says "Opus 5.5", a loose matcher keeps the family word and throws
the version away, something called Opus loads, and V.O.I.D cheerfully announces it did what was
asked. An hour is then spent testing the wrong brain. The rule is therefore absolute and is the whole
point of this module:

    When a spoken name is turned into an exact model id, a near miss REFUSES.
    It never falls back to the closest thing available.

What makes that honest rather than merely strict is where the set of real ids comes from. V.O.I.D does
not ship a hardcoded catalogue of other companies' model names - a list like that is stale the week
after it is written, and inventing ids V.O.I.D has never reached would be the same sin in a different
place. The known set is built from **configuration**: the models this installation actually names
(``llm.<provider>.model``, ``llm.gemini.vision_model``) plus anything the owner has explicitly added
under ``llm.known_models``. Adding a model really is one line, exactly as the packs promise, and
nothing is ever in the set because a model guessed it.

Two further boundaries, both deliberate:

**A swap is runtime only.** Nothing here writes to a file, so a restart always returns to the model
in configuration and the owner cannot strand themselves on a brain they did not mean to keep.

**A swap is not an authorization.** Choosing which model thinks changes nothing about which
capabilities exist or which data may leave the machine: ``void/providers/policy.py`` is consulted at
egress, per capability, and is not an input to anything here. Switching the chat brain also leaves
Gemini's separate ``vision_model`` alone, because a text swap that silently re-pointed image analysis
would be a surprise with a real quota cost behind it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

#: Providers whose chat model may be swapped at runtime, in the order they are offered to the owner.
#: ``local`` is included: switching to the offline brain on purpose is a legitimate request.
SWAPPABLE = ("gemini", "openai", "agentrouter", "local")

#: Family words a spoken name may begin with, mapped to the providers whose ids use them. Used ONLY
#: to recognise that the owner named a model at all, and to build a candidate id; the candidate is
#: still checked against the known set, which is the part that matters.
FAMILIES: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini",),
    "gpt": ("openai", "agentrouter"),
    "qwen": ("local",),
    "llama": ("local",),
    "mistral": ("local",),
    "phi": ("local",),
    "claude": ("openai", "agentrouter"),
    "opus": ("openai", "agentrouter"),
    "sonnet": ("openai", "agentrouter"),
    "haiku": ("openai", "agentrouter"),
    "fable": ("openai", "agentrouter"),
}

#: The words that ask for a swap. Whole-utterance shapes only, the same discipline
#: ``void/orchestration/commands.py`` uses, so "what is Gemini good at?" is a question and not a
#: command.
_SWITCH = re.compile(
    r"\A(?:void[,\s]+)?(?:please\s+)?"
    r"(?:switch(?:\s+(?:to|over\s+to|your\s+brain\s+to))?|change\s+(?:to|your\s+brain\s+to)|"
    r"try\s+on|try|use|run\s+on|think\s+(?:with|on)|put\s+on)\s+(?P<name>.+?)"
    r"(?:\s+(?:please|instead|for\s+(?:now|this)))?\s*\Z", re.IGNORECASE)

#: And the words that ask for the configured brain back.
_REVERT = re.compile(
    r"\A(?:void[,\s]+)?(?:please\s+)?(?:go\s+back\s+to|back\s+to|return\s+to|restore)\s+"
    r"(?:(?:your|the)\s+)?(?:normal|usual|default|original|configured)\s+(?:brain|model|self)\s*\Z"
    r"|\A(?:void[,\s]+)?(?:use|switch\s+to)\s+(?:(?:your|the)\s+)?"
    r"(?:normal|usual|default|configured)\s+(?:brain|model)\s*\Z", re.IGNORECASE)

#: Which brain is thinking right now, asked rather than commanded.
_WHICH = re.compile(r"\A(?:void[,\s]+)?(?:which|what)\s+(?:brain|model)\s+"
                    r"(?:are\s+you\s+(?:using|on|running)|is\s+(?:this|thinking))\s*\??\s*\Z",
                    re.IGNORECASE)

#: Spoken version tokens that are really digits. Kept tiny: a recogniser writes "5.5" far more often
#: than "five point five", and a long homophone table is a source of wrong matches, not right ones.
_SPOKEN_DIGITS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                  "six": "6", "seven": "7", "eight": "8", "nine": "9"}

_PUNCT = re.compile(r"[\s_]+")


class Intent:
    """What a brain utterance asked for."""

    SWITCH = "switch"
    REVERT = "revert"
    WHICH = "which"
    NONE = "none"

    ALL = frozenset({SWITCH, REVERT, WHICH, NONE})


@dataclass(frozen=True)
class Request:
    """A parsed brain utterance. ``spoken`` is the name as the owner said it, never normalised away."""

    intent: str
    spoken: str = ""


@dataclass(frozen=True)
class Resolution:
    """The outcome of turning a spoken name into an exact model id.

    ``refusal`` carries the sentence to say. It is populated exactly when ``model`` is empty, and it
    always names what IS available, because "I don't have that one" on its own sends the owner
    guessing again.
    """

    provider: str = ""
    model: str = ""
    refusal: str = ""
    #: Every id the owner could have meant, for the refusal sentence and for diagnostics.
    known: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.model)


#: Words that say the owner is talking about which model is thinking, whatever else is in the
#: sentence. Their presence is enough to claim an utterance for this route.
_SAYS_BRAIN = re.compile(r"\b(?:brain|model|llm)\b", re.IGNORECASE)

#: A version number in the name. "opus 5.5" is unambiguously a model; a bare family word is not.
_HAS_VERSION = re.compile(r"\d")


def parse(utterance: object, *, known=None) -> Request:
    """Classify a brain utterance deterministically, before any model sees it.

    ``known`` is the catalog, when the caller has one. It matters for a specific collision rather
    than for politeness: this route runs AHEAD of the website and application matchers, so claiming
    every sentence that happens to contain a family word would steal them. "switch to chat gpt" is a
    request to open ChatGPT the website, and the only honest way to tell it from "switch to GPT 5.6"
    is that the second one names a version.

    So an utterance is claimed only when the owner clearly meant a model: a version number is
    present, or the sentence says "brain"/"model", or the name resolves to an id this installation
    really knows. Anything else falls through to the routes behind this one - which is why a bare
    "switch to haiku" is left alone rather than refused.
    """
    text = " ".join(str(utterance or "").split()).strip(" .!,")
    if not text:
        return Request(Intent.NONE)
    if _REVERT.match(text):
        return Request(Intent.REVERT)
    if _WHICH.match(text):
        return Request(Intent.WHICH)
    match = _SWITCH.match(text)
    if match:
        name = match.group("name").strip(" .!,")
        # A name only counts when it begins with a family word, which is also what lets the refusal
        # be specific about what IS available.
        if _family_of(name) is not None and _means_a_model(text, name, known):
            return Request(Intent.SWITCH, spoken=name)
    return Request(Intent.NONE)


def _means_a_model(text: str, name: str, known) -> bool:
    """Did the owner clearly name a MODEL, as opposed to a website that shares a word with one?"""
    if _SAYS_BRAIN.search(text) or _HAS_VERSION.search(_normalise(name)):
        return True
    return known is not None and known.resolve(name).ok


def _normalise(name: str) -> str:
    text = _PUNCT.sub(" ", str(name or "").lower().strip())
    words = []
    for word in text.split():
        words.append(_SPOKEN_DIGITS.get(word, word))
    text = " ".join(words)
    # "three point eight" -> "3.8": the leading space goes too, or the version splits in two.
    text = re.sub(r"\s*\bpoint\s+(\d)", r".\1", text)
    text = re.sub(r"\b(the|your|brain|model|a)\b", " ", text)
    return " ".join(text.split())


def _family_of(name: str) -> str | None:
    words = _normalise(name).split()
    for word in words:
        for family in FAMILIES:
            if word == family or (word.startswith(family) and word[len(family):][:1].isdigit()):
                return family
    return None


def _candidates(name: str) -> list[str]:
    """Candidate exact ids for a spoken name, most specific first.

    This is a NAME NORMALISER, not a matcher: it only turns the owner's words into the shapes an id
    could take ("opus 5.5" -> "opus-5-5", "opus-5.5", "opus5.5"). Whether any of them exists is
    decided by the known set, and a name that produces no candidate present there is refused rather
    than approximated.
    """
    text = _normalise(name)
    if not text:
        return []
    # Split a glued family+version ("opus5", "gpt5.6") so the version is read as its own token.
    text = re.sub(r"\b([a-z]+?)(\d)", r"\1 \2", text)
    tokens = text.split()
    joined = "-".join(tokens)
    out = [joined, joined.replace(".", "-"), "".join(tokens), ":".join(tokens),
           "-".join(tokens[:1]) + ":" + "".join(tokens[1:]) if len(tokens) > 1 else joined]
    seen, unique = set(), []
    for cand in out:
        if cand and cand not in seen:
            seen.add(cand)
            unique.append(cand)
    return unique


@dataclass
class ModelCatalog:
    """The closed set of model ids this installation knows exist, per provider."""

    known: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Spoken name -> exact id, for names whose id cannot be derived ("astra", "the fast one").
    aliases: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: The model each provider was configured with, so a revert has something to return to.
    configured: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg) -> "ModelCatalog":
        known: dict[str, list[str]] = {}
        configured: dict[str, str] = {}
        for provider in SWAPPABLE:
            model = cfg.get(f"llm.{provider}.model", None)
            if model:
                configured[provider] = str(model)
                known.setdefault(provider, []).append(str(model))
        vision = cfg.get("llm.gemini.vision_model", None)
        if vision:
            known.setdefault("gemini", []).append(str(vision))
        # Extra ids the owner has verified against their own credential. The one line the packs
        # promise: llm.known_models: {gemini: ["gemini-3.6-flash"]}.
        extra = cfg.get("llm.known_models", {}) or {}
        if isinstance(extra, dict):
            for provider, ids in extra.items():
                if provider in SWAPPABLE and isinstance(ids, (list, tuple)):
                    known.setdefault(str(provider), []).extend(str(i) for i in ids if i)
        aliases: dict[str, tuple[str, str]] = {}
        spoken = cfg.get("llm.model_aliases", {}) or {}
        if isinstance(spoken, dict):
            for alias, target in spoken.items():
                provider, _, model = str(target or "").partition(":")
                # "gemini:gemini-3.6-flash" names the provider explicitly; a bare id is searched for.
                if model and provider in SWAPPABLE:
                    aliases[_normalise(alias)] = (provider, model)
                elif provider and not model:
                    for prov, ids in known.items():
                        if provider in ids:
                            aliases[_normalise(alias)] = (prov, provider)
                            break
        deduped = {p: tuple(dict.fromkeys(ids)) for p, ids in known.items()}
        return cls(known=deduped, aliases=aliases, configured=configured)

    def all_ids(self) -> tuple[str, ...]:
        return tuple(sorted({model for ids in self.known.values() for model in ids}))

    def _provider_of(self, model: str) -> str:
        for provider, ids in self.known.items():
            if model in ids:
                return provider
        return ""

    def resolve(self, spoken: str) -> Resolution:
        """Turn a spoken name into an exact id, or refuse.

        No branch of this function returns a model the owner did not name. There is no scoring, no
        edit distance and no "closest match": either a candidate id is in the known set, or the
        answer is a refusal that lists what is.
        """
        ids = self.all_ids()
        alias = self.aliases.get(_normalise(spoken))
        if alias is not None:
            provider, model = alias
            if model in self.known.get(provider, ()):
                return Resolution(provider=provider, model=model, known=ids)
            return Resolution(refusal=self._refuse(spoken, ids), known=ids)
        candidates = _candidates(spoken)
        for candidate in candidates:
            provider = self._provider_of(candidate)
            if provider:
                return Resolution(provider=provider, model=candidate, known=ids)
        # The one completion allowed, and it completes rather than approximates: a local model id
        # carries a size tag ("qwen3:8b") that nobody says out loud, so a name that matches exactly
        # one known id up to the colon resolves to it. A name matching TWO sizes is ambiguous and is
        # refused like anything else - the rule is never to choose on the owner's behalf, and
        # nothing here discards a version, which is the failure the refusal exists for.
        for candidate in candidates:
            tagged = sorted({model for model in ids
                             if ":" in model and model.split(":", 1)[0] == candidate})
            if len(tagged) == 1:
                return Resolution(provider=self._provider_of(tagged[0]), model=tagged[0], known=ids)
            if len(tagged) > 1:
                return Resolution(
                    refusal=(f"'{' '.join(str(spoken).split())}' matches more than one of mine: "
                             f"{', '.join(tagged)}. Say which."), known=ids)
        _log.info("MODEL_REFUSED candidates=%d known=%d", len(candidates), len(ids))
        return Resolution(refusal=self._refuse(spoken, ids), known=ids)

    @staticmethod
    def _refuse(spoken: str, ids: tuple[str, ...]) -> str:
        have = ", ".join(ids) if ids else "none - no model is configured"
        return (f"I do not have '{' '.join(str(spoken).split())}'. What I can run is: {have}. "
                f"I will not load something near it and tell you it is that one.")


@dataclass
class BrainSwitch:
    """Applies a resolved model to the live provider registry, for this process only."""

    catalog: ModelCatalog
    #: provider name -> the model it is running right now, once something has been swapped.
    current: dict[str, str] = field(default_factory=dict)

    def active(self, registry) -> tuple[str, str]:
        """(provider name, model id) of the brain that would answer right now, or ("", "")."""
        try:
            provider = registry.select()
        except Exception:                             # ProviderUnavailable: nothing to report
            return "", ""
        return getattr(provider, "name", ""), str(getattr(provider, "model", "") or "")

    def switch(self, registry, spoken: str) -> tuple[bool, str]:
        """Resolve ``spoken`` and apply it. Returns ``(changed, sentence to say)``."""
        resolution = self.catalog.resolve(spoken)
        if not resolution.ok:
            return False, resolution.refusal
        provider = registry.get(resolution.provider)
        if provider is None:
            return False, (f"I know the model '{resolution.model}' but the "
                           f"{resolution.provider} provider is not built in this installation.")
        if not hasattr(provider, "model"):
            return False, f"The {resolution.provider} provider does not take a model name."
        provider.model = resolution.model
        self.current[resolution.provider] = resolution.model
        _log.info("MODEL_SWITCHED provider=%s", resolution.provider)
        return True, (f"Running on {resolution.model} now. A restart puts me back on "
                      f"{self.catalog.configured.get(resolution.provider, 'the configured model')}.")

    def revert(self, registry) -> tuple[bool, str]:
        """Put every swapped provider back on its configured model."""
        restored = []
        for provider_name, model in self.catalog.configured.items():
            provider = registry.get(provider_name)
            if provider is not None and hasattr(provider, "model"):
                provider.model = model
                restored.append(model)
        self.current.clear()
        if not restored:
            return False, "There is no configured model to go back to."
        name, model = self.active(registry)
        return True, f"Back on {model or restored[0]}."
