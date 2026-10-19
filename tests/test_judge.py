"""The receipt judge: an answer may not claim an action that no tool receipt supports.

Two halves. The first is classification - which sentences are completed first-person claims about
the world and which are offers, denials, drafts, questions or reports of what a note said. Getting
that wrong in either direction is a real cost, so both directions are tested here in quantity. The
second is the gate in the agent loop: the owner hears the correction, the message history keeps what
the model actually wrote, and a receipt makes a true claim go through untouched.
"""
import logging

import pytest

from tests.helpers import FakeProvider, tool_call
from void.actions.base import Tool, ToolResult
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import Status, TaskStore
from void.orchestration import judge
from void.orchestration.judge import (ASK_LINE, CLAIM_VERBS, HONEST_LINES, TOOL_EFFECTS, Effect,
                                      Receipt, Ruling, screen)
from void.providers.base import LLMResponse
from void.security.risk import RiskGate, RiskLevel


# ================================================================== sentences that ARE claims

@pytest.mark.parametrize("answer, effect", [
    ("I've sent the email to Mike.", Effect.SEND),
    ("I sent it just now.", Effect.SEND),
    ("Done. I emailed the invoice over.", Effect.SEND),
    ("I have forwarded your reply.", Effect.SEND),
    ("The invoice has been sent.", Effect.SEND),
    ("Sent. The table is for seven.", Effect.SEND),
    ("I called the restaurant and it is fine.", Effect.CALL),
    ("I've phoned them.", Effect.CALL),
    ("I booked the kickoff for Monday at ten.", Effect.BOOK),
    ("I've scheduled it for Tuesday.", Effect.BOOK),
    ("I paid the deposit.", Effect.PAY),
    ("I've ordered the part.", Effect.PAY),
    ("I posted it to LinkedIn.", Effect.PUBLISH),
    ("I've uploaded it for you.", Effect.PUBLISH),
    ("I deleted the old report.", Effect.DELETE),
    ("Everything has now been deleted.", Effect.DELETE),
])
def test_a_completed_claim_with_no_receipt_is_blocked(answer, effect):
    ruling = screen(answer, [])
    assert ruling.blocked and ruling.unbacked == (effect,)
    assert ruling.answer == ruling.replacement and ruling.original == answer


@pytest.mark.parametrize("answer", [
    "I sent the email and booked the table.",
    "I've sent the email, then booked the table.",
    "Done. I sent the email and I booked the table.",
])
def test_a_second_claim_verb_in_the_same_sentence_is_not_missed(answer):
    assert set(screen(answer, []).unbacked) == {Effect.SEND, Effect.BOOK}


def test_several_unbacked_claims_in_one_answer_are_all_named():
    ruling = screen("I sent the email and booked the table.", [])
    assert ruling.blocked
    assert set(ruling.unbacked) == {Effect.SEND, Effect.BOOK}
    assert HONEST_LINES[Effect.SEND] in ruling.answer
    assert HONEST_LINES[Effect.BOOK] in ruling.answer


def test_the_correction_ends_with_something_the_owner_can_act_on():
    assert screen("I've sent it.", []).answer.endswith(ASK_LINE)


# ================================================================== sentences that are NOT claims

@pytest.mark.parametrize("answer", [
    # offers and intentions
    "Shall I send it?",
    "Would you like me to send it?",
    "I can send it whenever you say.",
    "I could call them if you want.",
    "I'll send it once you confirm.",
    "I am ready to post it.",
    "Do you want me to book that?",
    # denials and failures
    "I have not sent it.",
    "I did not send the email.",
    "I could not send it; the draft is in your editor.",
    "Nothing was sent.",
    "I was unable to book anything.",
    "I failed to post it.",
    # drafts
    "I've drafted a reply for you to review.",
    "The draft is saved and nothing was sent.",
    # about V.O.I.D's own machinery, not the world
    "I called the search tool and found three notes.",
    "I posted the summary below.",
    "I called this one 'pricing'.",
    # reporting what a source says
    "Your note says the kickoff has been booked for Monday.",
    "According to your notes the deposit was paid in March.",
    "Your email mentions it has been sent already.",
    # ordinary answers with no claim at all
    "Three notes mention pricing; the retainer is 2400 a month.",
    "Notepad is open and the heading is in place.",
    "The report has been updated with the new figures.",
    "That is not in the notes.",
])
def test_an_answer_that_claims_nothing_goes_through_untouched(answer):
    ruling = screen(answer, [])
    assert not ruling.blocked
    assert ruling.answer == answer and ruling.replacement == ""


def test_a_question_is_never_a_claim():
    assert not screen("Have I sent it? I do not think so.", []).blocked


# ================================================================== receipts

def test_a_claim_backed_by_a_successful_receipt_goes_through():
    ruling = screen("I deleted the old report.", [Receipt("delete_file", True)])
    assert not ruling.blocked
    assert ruling.claimed == (Effect.DELETE,) and ruling.unbacked == ()


def test_a_failed_receipt_does_not_back_a_claim():
    """The tool ran and said no. That is not a receipt for success."""
    assert screen("I deleted it.", [Receipt("delete_file", False)]).blocked


def test_a_receipt_from_an_unrelated_tool_backs_nothing():
    assert screen("I deleted it.", [Receipt("search_files", True)]).blocked
    assert screen("I've sent it.", [Receipt("write_file", True)]).blocked


def test_the_five_classes_void_cannot_perform_can_never_be_backed():
    """Structural: no tool produces these effects, so no set of receipts can back one."""
    every_tool_succeeded = [Receipt(name, True) for name in _all_tool_names()]
    claims = {Effect.SEND: "I sent the invoice to Mike.",
              Effect.CALL: "I phoned the restaurant.",
              Effect.BOOK: "I booked the table for seven.",
              Effect.PAY: "I paid the deposit.",
              Effect.PUBLISH: "I posted it to LinkedIn."}
    for effect, sentence in claims.items():
        assert TOOL_EFFECTS[effect] == frozenset()
        ruling = screen(sentence, every_tool_succeeded)
        assert ruling.blocked and effect in ruling.unbacked


def _all_tool_names() -> list[str]:
    import tempfile
    from pathlib import Path
    from void.app import Assistant
    from void.config import Config
    tmp = Path(tempfile.mkdtemp())
    (tmp / "work").mkdir(parents=True, exist_ok=True)
    cfg = Config({"app": {"state_dir": str(tmp / "state")},
                  "security": {"allowed_roots": [str(tmp / "work")]},
                  "camera": {"enabled": True}, "browser": {"enabled": True},
                  "desktop": {"enabled": True}, "screen": {"enabled": True},
                  "vault": {"notes_root": str(tmp / "work")}})
    return sorted(Assistant(config=cfg).tools.names())


def test_every_tool_that_could_produce_one_of_these_effects_is_in_the_table():
    """The guard that keeps the table honest as V.O.I.D grows.

    A new tool that sends, calls, books, pays, posts or deletes must be added to TOOL_EFFECTS, or the
    judge would block a true claim about it. This test lists the tools whose NAMES suggest such an
    effect and requires each to be accounted for - either mapped, or named here as a tool that only
    opens, reads or prepares rather than performing the effect itself.
    """
    mapped = {name for names in TOOL_EFFECTS.values() for name in names}
    # Tools whose name contains one of these words but which deliberately do NOT produce the effect.
    # Each entry is a decision, not an exemption: `open_conversation` opens a chat window and cannot
    # send in it (void/actions/messaging.py refuses a control that would act), and `propose_memory`
    # only proposes - the owner's review is what stores anything.
    no_effect = {"open_conversation", "propose_memory", "research_to_document", "create_document",
                 "inspect_document", "remember_reference", "resolve_reference", "read_page",
                 "write_note", "write_file", "copy_file", "move_file", "navigate", "look"}
    suspicious = {"send", "mail", "message", "text", "call", "dial", "book", "schedul", "pay",
                  "purchas", "order", "post", "publish", "upload", "delete", "remove", "erase"}
    for name in _all_tool_names():
        if any(word in name for word in suspicious):
            assert name in mapped or name in no_effect, (
                f"{name} looks like it produces an outside effect: map it in "
                f"judge.TOOL_EFFECTS, or add it to this test's no_effect set with a reason")


def test_every_claim_class_has_a_line_to_say_and_a_table_entry():
    assert set(CLAIM_VERBS) == Effect.ALL
    assert set(TOOL_EFFECTS) == Effect.ALL
    assert set(HONEST_LINES) == Effect.ALL


def test_the_judge_never_raises_whatever_it_is_handed():
    """A gate that can fail is a gate that can take an answer down with it."""
    for answer in (None, "", "   ", "?" * 500, "I've sent it." * 400):
        for receipts in (None, (), [Receipt("x", True)], [object()]):
            assert isinstance(screen(answer, receipts), Ruling)


# ================================================================== the gate in the agent loop

def make_agent(tmp_path, script, tools=None):
    registry = tools or ToolRegistry()
    return Agent(provider=FakeProvider(script), tools=registry,
                 risk_gate=RiskGate(confirm_at_or_above="high", confirm_fn=lambda *_a, **_k: True),
                 kill_switch=KillSwitch(stop_file=tmp_path / "STOP"),
                 store=TaskStore(tmp_path / "tasks.sqlite"))


def test_the_owner_is_told_the_truth_and_the_history_keeps_what_the_model_wrote(tmp_path, caplog):
    agent = make_agent(tmp_path, [LLMResponse(text="Done. I've sent the invoice to Mike.")])
    with caplog.at_level(logging.WARNING, logger="void.orchestration.judge"):
        out = agent.run("email mike the invoice")
    assert out.status == Status.COMPLETED
    assert HONEST_LINES[Effect.SEND] in out.result and "I've sent the invoice" not in out.result
    assert out.engine_authored is True
    assert any(m["role"] == "assistant" and "I've sent the invoice to Mike." in m["content"]
               for m in out.task.messages)
    assert "JUDGE_BLOCKED" in caplog.text and "I've sent" not in caplog.text   # classes, not content


def test_a_true_claim_backed_by_a_real_tool_call_reaches_the_owner_unchanged(tmp_path):
    deleted = []
    registry = ToolRegistry()
    registry.register(Tool(name="delete_file", description="d", parameters={"type": "object"},
                           handler=lambda **kw: (deleted.append(kw) or
                                                 ToolResult.success("Moved to the Recycle Bin.")),
                           risk=RiskLevel.HIGH))
    agent = make_agent(tmp_path, [LLMResponse(tool_calls=[tool_call("delete_file", path="x")]),
                                  LLMResponse(text="I deleted it.")], tools=registry)
    out = agent.run("delete x")
    assert deleted and out.status == Status.COMPLETED and out.result == "I deleted it."


def test_a_claim_after_a_tool_that_failed_never_even_reaches_the_owner(tmp_path):
    """The engine's own completion guard gets there first, which is the stronger behaviour.

    A step whose tool failed and was not resolved cannot complete at all, so "Done, I deleted it."
    is refused before the judge is consulted. Asserted here so that the two gates are known to
    overlap in the right order; the judge's own handling of a failed receipt is unit-tested above.
    """
    registry = ToolRegistry()
    registry.register(Tool(name="delete_file", description="d", parameters={"type": "object"},
                           handler=lambda **kw: ToolResult.failure("File is in use."),
                           risk=RiskLevel.HIGH))
    agent = make_agent(tmp_path, [LLMResponse(tool_calls=[tool_call("delete_file", path="x")]),
                                  LLMResponse(text="Done, I deleted it.")], tools=registry)
    out = agent.run("delete x")
    assert out.status == Status.FAILED and "I deleted it" not in (out.result or "")


def test_an_ordinary_answer_is_not_touched_by_the_gate(tmp_path):
    agent = make_agent(tmp_path, [LLMResponse(text="The retainer is 2400 a month.")])
    out = agent.run("what is the retainer")
    assert out.result == "The retainer is 2400 a month." and out.engine_authored is False


def test_receipts_do_not_survive_into_a_later_run(tmp_path):
    """A resumed or repeated run must not inherit a previous run's proof of success."""
    registry = ToolRegistry()
    registry.register(Tool(name="delete_file", description="d", parameters={"type": "object"},
                           handler=lambda **kw: ToolResult.success("Moved to the Recycle Bin."),
                           risk=RiskLevel.HIGH))
    agent = make_agent(tmp_path, [LLMResponse(tool_calls=[tool_call("delete_file", path="x")]),
                                  LLMResponse(text="I deleted it."),
                                  LLMResponse(text="I deleted it again.")], tools=registry)
    assert agent.run("delete x").result == "I deleted it."
    assert HONEST_LINES[Effect.DELETE] in agent.run("delete x again").result
