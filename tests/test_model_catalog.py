"""Naming a model by voice: the refusal that makes it safe, and the runtime swap behind it.

The property under test is one sentence long and the reason the module exists: a spoken name that
does not land on an exact known id is REFUSED, with the list of what is available, and never
approximated. Everything else here guards the edges of that - the known set comes from
configuration, a swap is runtime only, a swap authorizes nothing, and Gemini's separate vision model
is not dragged along by a text swap.
"""
import pytest

from void.app import Assistant
from void.config import Config
from void.providers.base import LLMProvider, LLMResponse
from void.providers.catalog import (FAMILIES, SWAPPABLE, BrainSwitch, Intent, ModelCatalog,
                                    Resolution, parse)
from void.providers.registry import ProviderRegistry


class Brain(LLMProvider):
    def __init__(self, name, model):
        self.name, self.model = name, model

    def available(self) -> bool:
        return True

    def generate(self, messages, tools=None):
        return LLMResponse(text="ok")


def catalog(**known) -> ModelCatalog:
    configured = {provider: ids[0] for provider, ids in known.items()}
    return ModelCatalog(known={p: tuple(ids) for p, ids in known.items()}, configured=configured)


GEMINI = catalog(gemini=["gemini-3.8-flash", "gemini-3.5-flash"], local=["qwen3:8b"])


# ================================================================== the known set comes from config

def test_the_known_set_is_what_configuration_names(tmp_path):
    cfg = Config({"llm": {"gemini": {"model": "gemini-3.8-flash",
                                     "vision_model": "gemini-3.5-flash"},
                          "local": {"model": "qwen3:8b"}}})
    cat = ModelCatalog.from_config(cfg)
    assert cat.all_ids() == ("gemini-3.5-flash", "gemini-3.8-flash", "qwen3:8b")
    assert cat.configured["gemini"] == "gemini-3.8-flash"


def test_adding_a_verified_model_is_one_line_of_configuration():
    cat = ModelCatalog.from_config(Config({
        "llm": {"gemini": {"model": "gemini-3.8-flash"},
                "known_models": {"gemini": ["gemini-3.6-flash"]}}}))
    assert cat.resolve("gemini 3.6 flash").model == "gemini-3.6-flash"


def test_an_unknown_provider_in_known_models_is_ignored_rather_than_obeyed():
    cat = ModelCatalog.from_config(Config({
        "llm": {"gemini": {"model": "gemini-3.8-flash"},
                "known_models": {"nonsense": ["anything-at-all"]}}}))
    assert "anything-at-all" not in cat.all_ids()


def test_a_spoken_alias_resolves_only_to_an_id_that_is_really_known():
    good = ModelCatalog.from_config(Config({
        "llm": {"gemini": {"model": "gemini-3.8-flash"},
                "model_aliases": {"the fast one": "gemini:gemini-3.8-flash"}}}))
    assert good.resolve("the fast one").model == "gemini-3.8-flash"
    bad = ModelCatalog.from_config(Config({
        "llm": {"gemini": {"model": "gemini-3.8-flash"},
                "model_aliases": {"astra": "gemini:openai/gpt-6-astra"}}}))
    assert not bad.resolve("astra").ok and bad.resolve("astra").refusal


# ================================================================== the refusal

@pytest.mark.parametrize("spoken", [
    "gemini 4", "gemini 3.9 flash", "gemini 3.8 pro", "opus 5.5", "opus5", "gpt 6",
    "claude fable 5.1", "llama 4", "gemini", "gpt 5.6 sol",
])
def test_a_name_that_is_not_in_the_known_set_is_refused(spoken):
    resolution = GEMINI.resolve(spoken)
    assert not resolution.ok
    assert resolution.model == ""
    assert resolution.refusal


def test_the_refusal_names_what_is_available_so_the_owner_is_not_left_guessing():
    refusal = GEMINI.resolve("opus 5.5").refusal
    assert "opus 5.5" in refusal
    for known in GEMINI.all_ids():
        assert known in refusal


def test_the_version_is_never_discarded_to_reach_a_near_match():
    """The exact failure the rule exists for: "3.9" must not quietly load 3.8."""
    for spoken in ("gemini 3.9 flash", "gemini 3 flash", "gemini 3.85 flash"):
        assert not GEMINI.resolve(spoken).ok
    assert GEMINI.resolve("gemini 3.8 flash").model == "gemini-3.8-flash"


def test_a_glued_family_and_version_is_read_as_a_version(tmp_path):
    cat = catalog(gemini=["gemini-3.8-flash"])
    assert not cat.resolve("gemini3").ok          # a real version, and not one we have
    assert cat.resolve("gemini3.8 flash").model == "gemini-3.8-flash"


@pytest.mark.parametrize("spoken, expected", [
    ("gemini 3.8 flash", "gemini-3.8-flash"),
    ("gemini-3.8-flash", "gemini-3.8-flash"),
    ("Gemini 3.5 Flash", "gemini-3.5-flash"),
    ("gemini three point eight flash", "gemini-3.8-flash"),
])
def test_the_ways_a_known_name_can_be_said_all_reach_the_same_id(spoken, expected):
    assert GEMINI.resolve(spoken).model == expected


def test_a_size_tag_may_be_completed_but_only_when_it_is_unambiguous():
    assert GEMINI.resolve("qwen3").model == "qwen3:8b"            # one match: completed
    two_sizes = catalog(local=["qwen3:8b", "qwen3:14b"])
    resolution = two_sizes.resolve("qwen3")
    assert not resolution.ok and "more than one" in resolution.refusal
    assert "qwen3:8b" in resolution.refusal and "qwen3:14b" in resolution.refusal


def test_an_empty_catalogue_refuses_everything_and_says_so():
    resolution = ModelCatalog().resolve("gemini 3.8 flash")
    assert not resolution.ok and "no model is configured" in resolution.refusal


def test_every_family_word_names_providers_that_really_exist():
    for family, providers in FAMILIES.items():
        assert providers, family
        for provider in providers:
            assert provider in SWAPPABLE, f"{family} names a provider that cannot be swapped"


# ================================================================== parsing the utterance

@pytest.mark.parametrize("utterance", [
    "switch to gemini 3.8 flash", "switch gemini 3.8 flash", "change to gemini 3.8 flash",
    "try on gemini 3.5 flash", "use qwen3", "run on gemini 3.8 flash",
    "think with gemini 3.8 flash", "Void, switch to gemini 3.8 flash",
    "switch to gemini 3.8 flash please",
])
def test_the_ways_a_swap_is_asked_for_are_recognised(utterance):
    assert parse(utterance).intent == Intent.SWITCH


@pytest.mark.parametrize("utterance", [
    "go back to your normal brain", "back to your usual model", "return to the default brain",
    "use your normal brain",
])
def test_asking_for_the_configured_brain_back_is_recognised(utterance):
    assert parse(utterance).intent == Intent.REVERT


@pytest.mark.parametrize("utterance", [
    "which model are you using", "what brain are you on", "which brain is thinking?",
])
def test_asking_which_brain_is_a_question_not_a_swap(utterance):
    assert parse(utterance).intent == Intent.WHICH


@pytest.mark.parametrize("utterance", [
    "", "   ", "switch to the other window", "switch to dark mode", "use the kitchen table",
    "what is gemini good at", "tell me about gpt 5", "my notes mention gemini 3.8 flash",
    "switch tabs", "change the volume",
])
def test_an_utterance_that_is_not_about_the_brain_is_left_alone(utterance):
    assert parse(utterance).intent == Intent.NONE


# ================================================================== the swap itself

def registry() -> ProviderRegistry:
    return ProviderRegistry({"gemini": Brain("gemini", "gemini-3.8-flash"),
                             "local": Brain("local", "qwen3:8b")}, ["gemini", "local"])


def test_a_swap_changes_the_live_model_and_says_what_a_restart_will_do():
    reg, switch = registry(), BrainSwitch(GEMINI)
    changed, said = switch.switch(reg, "gemini 3.5 flash")
    assert changed and reg.get("gemini").model == "gemini-3.5-flash"
    assert "gemini-3.5-flash" in said and "restart" in said.lower()


def test_a_refused_swap_changes_nothing():
    reg, switch = registry(), BrainSwitch(GEMINI)
    changed, said = switch.switch(reg, "opus 5.5")
    assert not changed and reg.get("gemini").model == "gemini-3.8-flash"
    assert "I do not have" in said


def test_a_revert_puts_every_swapped_provider_back_on_its_configured_model():
    reg, switch = registry(), BrainSwitch(GEMINI)
    switch.switch(reg, "gemini 3.5 flash")
    changed, _said = switch.revert(reg)
    assert changed and reg.get("gemini").model == "gemini-3.8-flash"
    assert switch.current == {}


def test_nothing_about_a_swap_is_written_down(tmp_path):
    """Runtime only: a fresh catalogue from the same config still names the configured model."""
    reg, switch = registry(), BrainSwitch(GEMINI)
    switch.switch(reg, "gemini 3.5 flash")
    assert ModelCatalog.from_config(
        Config({"llm": {"gemini": {"model": "gemini-3.8-flash"}}})).configured["gemini"] == \
        "gemini-3.8-flash"


# ================================================================== through the Assistant

def make(tmp_path, **llm) -> Assistant:
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    cfg = Config({"app": {"state_dir": str(tmp_path / "state")},
                  "security": {"allowed_roots": [str(tmp_path / "work")]},
                  "memory": {"enabled": False},
                  "llm": {"primary": "gemini", "fallback": ["local"],
                          "gemini": {"model": "gemini-3.8-flash",
                                     "vision_model": "gemini-3.5-flash"},
                          "local": {"model": "qwen3:8b"}, **llm}})
    assistant = Assistant(config=cfg)
    assistant.providers = registry()
    assistant.brains = BrainSwitch(ModelCatalog.from_config(cfg))
    return assistant


def test_the_owner_can_swap_by_sentence_without_a_model_being_consulted(tmp_path):
    a = make(tmp_path)
    out = a.run("switch to gemini 3.5 flash")
    assert out.steps == 0 and out.engine_authored is True
    assert a.providers.get("gemini").model == "gemini-3.5-flash"


def test_an_unknown_name_said_out_loud_is_refused_out_loud(tmp_path):
    a = make(tmp_path)
    out = a.run("try on opus 5.5")
    assert out.steps == 0 and "I do not have 'opus 5.5'" in out.result
    assert "gemini-3.8-flash" in out.result
    assert a.providers.get("gemini").model == "gemini-3.8-flash"


def test_asking_which_brain_reports_the_one_that_would_actually_answer(tmp_path):
    a = make(tmp_path)
    assert "gemini-3.8-flash" in a.run("which model are you using").result


def test_a_text_swap_leaves_the_separate_vision_model_alone(tmp_path):
    """Image analysis has its own model for measured quota reasons; a chat swap must not move it."""
    a = make(tmp_path)
    a.providers = ProviderRegistry.from_config(a.config)      # the real Gemini provider
    gemini = a.providers.get("gemini")
    before = gemini.vision_model
    a.run("switch to gemini 3.5 flash")
    assert gemini.model == "gemini-3.5-flash"
    assert gemini.vision_model == before


def test_a_swap_grants_no_capability_and_touches_no_authorization(tmp_path):
    a = make(tmp_path)
    tools_before, roots_before = set(a.tools.names()), list(a.config.allowed_roots())
    a.run("switch to gemini 3.5 flash")
    assert set(a.tools.names()) == tools_before
    assert list(a.config.allowed_roots()) == roots_before
    assert a.kill_switch.engaged is False
