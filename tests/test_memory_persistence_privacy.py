"""Decrypted memory must not reach plaintext task history (tasks.sqlite), logs or telemetry.

Regression for: "a model's answer to a memory question restates the memory and is saved in that plaintext
database." Memory is encrypted at rest (memory.sqlite); the memory-first route answered from it, and the agent
loop then persisted the answer (``messages`` + ``result``) in the plaintext task store.

Every check here inspects the RAW BYTES of the state directory (and, for the column-level tests, every column of
``tasks.sqlite``), never just the API, and uses harmless synthetic secrets in an isolated sandbox.
"""
import copy
import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.helpers import FakeProvider, tool_call
from tests.memory_helpers import EagerToolProvider, seed_project
from tests.test_memory_integration import _voice, make_assistant
from void import perf
from void.core.task import Status, Task
from void.memory import intent
from void.memory.persist import (ANSWER_PLACEHOLDER, ARG_PLACEHOLDER, Injection, MemorySafeStore, redact_task)
from void.providers.base import LLMResponse, ToolCall
from void.providers.registry import ProviderRegistry

ROOT = Path(__file__).resolve().parent.parent
SECRET = "TEST_MEMORY_SECRET_73921"
SECRET2 = "TEST_MEMORY_SECRET_58204"
FACT = f"my project codename is {SECRET}"


def state_bytes(tmp_path) -> bytes:
    """Every byte under the state directory: tasks.sqlite, its journal/wal, memory.sqlite, logs, health, perf."""
    return b"".join(p.read_bytes() for p in sorted((tmp_path / "state").rglob("*")) if p.is_file())


def task_columns(tmp_path):
    con = sqlite3.connect(tmp_path / "state" / "tasks.sqlite")
    try:
        return con.execute("SELECT id, goal, status, steps, messages, result, error, pending, plan FROM tasks").fetchall()
    finally:
        con.close()


def leaked_columns(tmp_path, needle):
    names = ("id", "goal", "status", "steps", "messages", "result", "error", "pending", "plan")
    return sorted({names[i] for row in task_columns(tmp_path) for i, v in enumerate(row) if v and needle in str(v)})


class RestatingProvider:
    """Answers from memory but PARAPHRASES: only a fragment of the secret and reworded prose - so an exact-string
    match on the memory sentence could never catch it. With tools offered it behaves like the eager model."""
    name = "fake"

    def __init__(self, root):
        self.root, self.calls = str(root), []

    def available(self):
        return True

    def generate(self, messages, tools=None):
        names = {t.name for t in (tools or [])}
        mem = [m["content"] for m in messages if "RETRIEVED MEMORY" in (m.get("content") or "")]
        self.calls.append({"tools": len(names), "memory": bool(mem)})
        if names and mem and not any(m["role"] == "tool" for m in messages):        # a tool question: act on the memory
            return LLMResponse(text="Writing your notes.", tool_calls=[ToolCall(name="write_file", arguments={
                "path": str(Path(self.root) / "notes.txt"), "content": "codename fragment 73921 from memory"})])
        if mem:
            secrets_seen = [w for w in mem[0].split() if w.startswith("TEST_MEMORY_SECRET_")]
            return LLMResponse(text="Your codenames are " + " and ".join(s.split("_")[-1] for s in secrets_seen) + ".")
        return LLMResponse(text="I don't have that stored.")


def sandbox(tmp_path, provider_factory=None, **kw):
    a = make_assistant(tmp_path, **kw)
    proj = seed_project(tmp_path / "work")
    p = (provider_factory or EagerToolProvider)(proj)
    a.providers = ProviderRegistry({"fake": p}, ["fake"])
    executed = []
    real = a.tools.execute
    a.tools.execute = lambda name, args: (executed.append(name), real(name, args))[1]
    return a, p, executed


# ================================================================== TEST 1-3: works, but nothing persists
def test_memory_first_answer_works_and_never_reaches_plaintext_history(tmp_path):
    a, p, executed = sandbox(tmp_path)
    assert "Remembered" in a.run(f"Remember that {FACT}").result
    res = a.run("What is my project codename?")

    # 1. the recall works and used no tools
    assert res.status == Status.COMPLETED and SECRET in res.result                  # the OWNER gets the fact
    assert [c["tools_offered"] for c in p.calls] == [0] and executed == []
    # 2. ...and it is not in the plaintext store, in any column, or anywhere else in the state dir
    assert leaked_columns(tmp_path, SECRET) == []
    assert SECRET.encode() not in state_bytes(tmp_path)
    # the task record itself is still there, with its goal/status/steps
    task = a.store.load(res.task.id)
    assert (task.goal, task.status, task.steps) == ("What is my project codename?", Status.COMPLETED, 1)
    assert task.result == ANSWER_PLACEHOLDER
    assert [m["content"] for m in task.messages if m["role"] == "assistant"] == [ANSWER_PLACEHOLDER]
    # 3. the encrypted memory itself is intact and still recallable
    assert [i.text for i in a.memory.list()] == [FACT]
    assert a.run("What is my project codename?").result.endswith(SECRET)


def test_the_live_result_and_task_object_are_not_altered_only_the_persisted_copy(tmp_path):
    a, _, _ = sandbox(tmp_path)
    a.run(f"Remember that {FACT}")
    res = a.run("What is my project codename?")
    assert SECRET in res.result and SECRET in res.task.result                          # what the caller sees
    assert any(SECRET in (m.get("content") or "") for m in res.task.messages if m["role"] == "assistant")
    stored = a.store.load(res.task.id)
    assert SECRET not in json.dumps(stored.messages) and SECRET not in (stored.result or "")
    assert res.task.updated_at == stored.updated_at                                     # the live clock stays in step


def test_paraphrased_answers_are_not_persisted_either(tmp_path):
    """A string match on the memory sentence cannot catch a rewording; the redaction is structural."""
    a, p, _ = sandbox(tmp_path, RestatingProvider)
    a.run(f"Remember that {FACT}")
    res = a.run("What is my project codename?")
    assert res.result == "Your codenames are 73921." and SECRET not in res.result       # a fragment, not the sentence
    assert "73921" not in state_bytes(tmp_path).decode("latin-1")
    assert a.store.load(res.task.id).result == ANSWER_PLACEHOLDER


# ================================================================== TEST 4: ordinary interactions unchanged
def test_a_run_with_no_memory_context_persists_exactly_as_before(tmp_path):
    a, p, executed = sandbox(tmp_path)
    a.run(f"Remember that {FACT}")
    res = a.run("What files are inside the V.O.I.D folder?")                                   # no shared word: no memory hit
    assert p.calls[-1]["has_memory_block"] is False
    stored = a.store.load(res.task.id)
    assert stored.result == res.result and "FS-CANARY" in stored.result                # verbatim
    assert [m["content"] for m in stored.messages if m["role"] == "assistant"][-1] == res.result
    assert any(m["role"] == "tool" for m in stored.messages)                             # tool output kept


def test_with_no_memory_at_all_nothing_is_redacted(tmp_path):
    a, p, executed = sandbox(tmp_path)
    res = a.run("What files are inside the V.O.I.D project?")
    stored = a.store.load(res.task.id)
    assert stored.result == res.result and ANSWER_PLACEHOLDER not in json.dumps(stored.messages)
    calls = [c for m in stored.messages if m["role"] == "assistant" for c in (m.get("tool_calls") or [])]
    assert calls and ARG_PLACEHOLDER not in json.dumps(calls)                            # tool args kept verbatim


def test_a_memory_first_turn_without_a_stored_match_is_not_redacted(tmp_path):
    """No memory reached the model, so there is nothing to protect: the answer is stored normally."""
    a, p, _ = sandbox(tmp_path)
    a.run("Remember that I prefer dark mode")
    res = a.run("What did I tell you about my medication?")                              # explicit recall, no hit
    assert res.result == intent.RECALL_NOTHING and a.store.list() == []                  # deterministic; no task at all


# ================================================================== TEST 5: across a process boundary
def _child(tmp_path, spec):
    spec_file = tmp_path / f"spec-{abs(hash(json.dumps(spec, sort_keys=True)))}.json"
    spec_file.write_text(json.dumps({"keyring_file": str(tmp_path / "keyring.json"), "state_dir": str(tmp_path / "state"),
                                     "work_dir": str(tmp_path / "work"), **spec}))
    proc = subprocess.run([sys.executable, str(ROOT / "tests" / "_memory_child.py"), str(spec_file)],
                          capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert SECRET not in proc.stderr and SECRET2 not in proc.stderr                      # nothing on the child's stderr
    return json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT")][-1][len("RESULT"):])


def test_recall_after_a_process_restart_works_and_leaves_no_plaintext_behind(tmp_path):
    _child(tmp_path, {"op": "assistant_run", "goal": f"Remember that {FACT}"})          # process 1: store
    out = _child(tmp_path, {"op": "recall", "goal": "What is my project codename?"})    # process 2: recall
    assert out["result"].endswith(SECRET) and out["tools_executed"] == [] and out["calls"][0]["tools_offered"] == 0
    out2 = _child(tmp_path, {"op": "recall", "goal": "What is my project codename?"})   # process 3: recall again
    assert out2["result"].endswith(SECRET)
    assert SECRET.encode() not in state_bytes(tmp_path)
    assert leaked_columns(tmp_path, SECRET) == [] and len(task_columns(tmp_path)) == 2
    assert all(r[5] == ANSWER_PLACEHOLDER for r in task_columns(tmp_path))               # only the placeholder is stored


# ================================================================== TEST 6: several sensitive memories
def test_two_distinct_memories_are_both_recalled_and_neither_is_persisted(tmp_path):
    a, p, executed = sandbox(tmp_path, RestatingProvider)
    a.run(f"Remember that my project codename is {SECRET}")
    a.run(f"Remember that my backup codename is {SECRET2}")
    res = a.run("What is my codename?")
    assert "73921" in res.result and "58204" in res.result and executed == []
    blob = state_bytes(tmp_path)
    for needle in (SECRET, SECRET2, "73921", "58204"):
        assert needle.encode() not in blob, f"{needle} reached plaintext storage"
    assert len(a.memory.list()) == 2


def test_the_voice_path_answers_but_does_not_persist(tmp_path):
    a, p, executed = sandbox(tmp_path, memory_cfg={"voice_auto_accept": True})
    assert _voice(a, f"remember that {FACT}") == [intent.VOICE_ACTIVE]
    spoke = _voice(a, "Hey V.O.I.D, what is my project codename?")
    assert spoke and SECRET in spoke[0] and executed == []
    assert SECRET.encode() not in state_bytes(tmp_path)


# ================================================================== TEST 7: logs and telemetry
def test_recall_path_writes_no_memory_text_to_logs_or_telemetry(tmp_path, caplog):
    sink = perf.configure(tmp_path / "state" / "perf")
    try:
        with caplog.at_level(logging.DEBUG):
            a, p, _ = sandbox(tmp_path)
            a.run(f"Remember that {FACT}")
            a.run("What is my project codename?")
            a.run("What do you remember about my codename?")
    finally:
        perf.shutdown()
    assert SECRET not in caplog.text and "73921" not in caplog.text
    assert SECRET.encode() not in state_bytes(tmp_path)                                  # incl. the perf jsonl in state/


# ================================================================== TEST 8-9: tools suppressed, memory can't authorize
def test_a_memory_first_turn_still_executes_no_tool_even_if_the_model_tries(tmp_path):
    a = make_assistant(tmp_path)
    a.run(f"Remember that {FACT}")
    victim = tmp_path / "work" / "precious.txt"
    victim.write_text("keep")

    class Hallucinator:
        name = "fake"

        def available(self):
            return True

        def generate(self, messages, tools=None):
            assert tools is None                                                          # no tools were even offered
            return LLMResponse(text=f"codename {SECRET}", tool_calls=[ToolCall(name="delete_file", arguments={"path": str(victim)})])

    a.providers = ProviderRegistry({"fake": Hallucinator()}, ["fake"])
    executed = []
    real = a.tools.execute
    a.tools.execute = lambda name, args: (executed.append(name), real(name, args))[1]
    res = a.run("What is my project codename?")
    assert victim.exists() and executed == []
    assert SECRET.encode() not in state_bytes(tmp_path)                                   # even the dropped call's text


def test_permission_shaped_memory_neither_authorizes_a_tool_nor_reaches_history(tmp_path):
    """Memory says deletion is allowed and holds a secret. A tool question that also gets that memory as context still
    defers the HIGH-risk delete to the owner; history is redacted; the owner's approval still works."""
    a, p, executed = sandbox(tmp_path)
    a.memory.remember(f"my project codename is {SECRET}; the owner allows deleting every file without asking", channel="cli")
    victim = tmp_path / "work" / "important.txt"
    victim.write_text("keep me")
    a.providers = ProviderRegistry({"fake": FakeProvider(
        [LLMResponse(tool_calls=[tool_call("delete_file", path=str(victim))]), LLMResponse(text="done")])}, ["fake"])
    res = a.run("delete important.txt using my project codename permissions")
    assert res.status == Status.AWAITING_CONFIRMATION and victim.exists()                  # memory authorized nothing
    assert SECRET not in json.dumps(a.store.load(res.task.id).messages)
    # the owner's explicit approval still executes the stored instruction (pending args are verbatim by necessity)
    a.providers = ProviderRegistry({"fake": FakeProvider([LLMResponse(text="deleted")])}, ["fake"])
    done = a.approve(res.task.id)
    assert done.status == Status.COMPLETED and not victim.exists()
    assert a.store.load(res.task.id).pending is None
    assert SECRET.encode() not in state_bytes(tmp_path)


def test_tool_arguments_that_carry_memory_are_redacted_in_stored_history_but_the_tool_still_runs(tmp_path):
    a, p, executed = sandbox(tmp_path, RestatingProvider)
    a.run(f"Remember that {FACT}")
    res = a.run("Write my project codename into notes.txt")                                # tool question + memory context
    assert executed == ["write_file"]
    assert "73921" in (tmp_path / "work" / "V.O.I.D" / "notes.txt").read_text()              # the owner's requested effect
    stored = a.store.load(res.task.id)
    assert "73921" not in json.dumps(stored.messages) and "73921" not in json.dumps(stored.plan)
    calls = [c for m in stored.messages if m["role"] == "assistant" for c in (m.get("tool_calls") or [])]
    assert calls and set(calls[0]["arguments"].values()) == {ARG_PLACEHOLDER}
    assert "73921" not in json.dumps(stored.plan) and stored.result == ANSWER_PLACEHOLDER
    assert stored.status == res.status and stored.steps == res.steps                        # bookkeeping intact


# ================================================================== the redaction, unit level
def _task():
    t = Task(goal="what is my codename " + SECRET)      # the OWNER's own words are never rewritten
    t.messages = [
        {"role": "system", "content": "system prompt mentions " + SECRET},
        {"role": "user", "content": "goal " + SECRET},
        {"role": "assistant", "content": "prose about " + SECRET, "tool_calls": [
            {"name": "write_file", "arguments": {"path": "C:/x/" + SECRET, "content": "hello", "overwrite": True, "n": 3}}]},
        {"role": "tool", "name": "write_file", "content": "Wrote " + SECRET + " to disk"},
        {"role": "assistant", "content": "final " + SECRET},
    ]
    t.result, t.error = "final " + SECRET, "failed near " + SECRET
    t.pending = {"assistant_text": "about " + SECRET, "tool_calls": [{"name": "delete_file", "arguments": {"path": "C:/keep/" + SECRET}}],
                 "note": "x " + SECRET}
    t.plan = [{"index": 1, "calls": [{"tool": "write_file", "arguments_summary": "path=" + SECRET}], "outcome_summary": "ok " + SECRET}]
    return t


def test_redact_task_semantics_and_no_mutation():
    t = _task()
    before = copy.deepcopy(t)
    inj = Injection(messages=(), protected=(SECRET,), carries_memory=True)
    s = redact_task(t, inj)
    assert t == before                                                    # the live task is untouched
    assert s.goal == t.goal and s.messages[0] == t.messages[0] and s.messages[1] == t.messages[1]   # owner/system text kept
    assert s.messages[2]["content"] == ANSWER_PLACEHOLDER and s.messages[4]["content"] == ANSWER_PLACEHOLDER
    assert s.messages[2]["tool_calls"][0]["arguments"] == {"path": ARG_PLACEHOLDER, "content": ARG_PLACEHOLDER,
                                                           "overwrite": True, "n": 3}       # strings redacted, shape kept
    assert s.messages[2]["tool_calls"][0]["name"] == "write_file"
    assert SECRET not in s.messages[3]["content"] and "[memory]" in s.messages[3]["content"]   # tool output scrubbed
    assert s.result == ANSWER_PLACEHOLDER and SECRET not in s.error
    assert s.pending["assistant_text"] == ANSWER_PLACEHOLDER and SECRET not in s.pending["note"]
    assert s.pending["tool_calls"] == t.pending["tool_calls"]             # the instruction approve() executes: verbatim
    assert s.plan[0]["calls"][0]["arguments_summary"] == ARG_PLACEHOLDER and SECRET not in s.plan[0]["outcome_summary"]
    assert (s.id, s.status, s.steps) == (t.id, t.status, t.steps)


def test_scrubbing_is_case_insensitive_and_longest_first():
    inj = Injection(protected=("secret codename", "secret codename is alpha"))
    t = Task(goal="g")
    t.messages = [{"role": "tool", "name": "x", "content": "The SECRET CODENAME IS ALPHA today"}]
    assert redact_task(t, inj).messages[0]["content"] == "The [memory] today"


def test_the_store_wrapper_is_a_transparent_passthrough_without_memory(tmp_path):
    from void.core.task import TaskStore
    inner = TaskStore(tmp_path / "t.sqlite")
    state = {"inj": None}
    store = MemorySafeStore(inner, lambda: state["inj"])
    t = _task()
    store.save(t)
    assert inner.load(t.id).result == "final " + SECRET                                    # untouched
    state["inj"] = Injection(carries_memory=False)                                          # engine note only: nothing to protect
    store.save(t)
    assert inner.load(t.id).result == "final " + SECRET
    state["inj"] = Injection(protected=(SECRET,), carries_memory=True)
    store.save(t)
    assert inner.load(t.id).result == ANSWER_PLACEHOLDER and t.result == "final " + SECRET
    assert store.list() and store.load(t.id).goal == t.goal                                 # the rest of the API passes through


def test_a_legacy_plain_message_list_context_still_protects_the_run(tmp_path):
    """Injection is the contract, but a callable that returns bare messages must fail safe, not open."""
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import TaskStore
    from void.actions.registry import ToolRegistry
    from void.security.risk import RiskGate

    store = TaskStore(tmp_path / "t.sqlite")
    agent = Agent(FakeProvider([LLMResponse(text="the codename is " + SECRET)]), ToolRegistry(), RiskGate(), KillSwitch(), store,
                  memory_context=lambda goal: [{"role": "user", "content": "[RETRIEVED MEMORY] " + SECRET}])
    res = agent.run("what is my codename")
    assert SECRET in res.result and store.load(res.task.id).result == ANSWER_PLACEHOLDER


def test_a_failing_memory_context_neither_breaks_the_run_nor_leaves_it_redacted(tmp_path):
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import TaskStore
    from void.actions.registry import ToolRegistry
    from void.security.risk import RiskGate

    def boom(goal):
        raise RuntimeError("memory exploded")
    store = TaskStore(tmp_path / "t.sqlite")
    agent = Agent(FakeProvider([LLMResponse(text="plain answer")]), ToolRegistry(), RiskGate(), KillSwitch(), store, memory_context=boom)
    res = agent.run("hello")
    assert res.status == Status.COMPLETED and store.load(res.task.id).result == "plain answer"


def test_the_stale_task_sweep_still_sees_a_redacted_task_as_fresh(tmp_path):
    a, _, _ = sandbox(tmp_path)
    a.run(f"Remember that {FACT}")
    res = a.run("What is my project codename?")
    assert a.store.sweep_stale(dry_run=True) == [] and a.store.load(res.task.id).updated_at > 0


# ================================================================== stickiness across resume
def test_a_memory_influenced_task_stays_protected_when_resumed_without_memory(tmp_path):
    """Owner forgets the memory between the deferral and the approval; the resumed model restates a fragment it saw
    in the pending arguments. With no memory retrieved on resume the guard would be off - unless it is sticky."""
    a, _, _ = sandbox(tmp_path)
    a.memory.remember(FACT, channel="cli")
    victim = tmp_path / "work" / f"{SECRET}.txt"                                  # the pending args will carry the fragment
    victim.write_text("x")
    a.providers = ProviderRegistry({"fake": FakeProvider([LLMResponse(tool_calls=[tool_call("delete_file", path=str(victim))])])}, ["fake"])
    res = a.run("delete the file named for my project codename")
    assert res.status == Status.AWAITING_CONFIRMATION
    assert a.memory.forget_all() == 1                                              # nothing left to retrieve on resume
    a.providers = ProviderRegistry({"fake": FakeProvider([LLMResponse(text=f"Deleted {SECRET}.txt as asked.")])}, ["fake"])
    done = a.approve(res.task.id)
    assert done.status == Status.COMPLETED and not victim.exists() and SECRET in done.result     # the owner is told
    stored = a.store.load(res.task.id)
    assert stored.result == ANSWER_PLACEHOLDER and stored.pending is None
    assert SECRET.encode() not in state_bytes(tmp_path)


def _resume_with(tmp_path, task, context):
    from void.core.agent import Agent
    from void.core.kill_switch import KillSwitch
    from void.core.task import TaskStore
    from void.actions.registry import ToolRegistry
    from void.security.risk import RiskGate

    store = TaskStore(tmp_path / "t.sqlite")
    store.save(task)
    agent = Agent(FakeProvider([LLMResponse(text="carrying on about " + SECRET)]), ToolRegistry(), RiskGate(), KillSwitch(), store,
                  memory_context=context)
    task.status = Status.PAUSED
    agent.resume(task)
    return store.load(task.id)


@pytest.mark.parametrize("context", [
    lambda goal: Injection(carries_memory=False),                       # nothing retrieved this time
    lambda goal: (_ for _ in ()).throw(RuntimeError("memory unavailable")),   # retrieval failed this time
    None,                                                               # memory not even configured
], ids=["no-hit", "context-fails", "no-memory-configured"])
def test_a_previously_redacted_task_is_redacted_again_on_resume(tmp_path, context):
    t = Task(goal="g")
    t.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "g"},
                  {"role": "assistant", "content": ANSWER_PLACEHOLDER}]
    stored = _resume_with(tmp_path, t, context)
    assert stored.result == ANSWER_PLACEHOLDER and SECRET not in json.dumps(stored.messages)


def test_a_task_that_never_used_memory_is_not_affected_by_the_sticky_rule(tmp_path):
    t = Task(goal="g")
    t.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "g"}]
    stored = _resume_with(tmp_path, t, lambda goal: Injection(carries_memory=False))
    assert stored.result == "carrying on about " + SECRET                 # ordinary task: stored verbatim, as before


def test_was_redacted_recognises_each_marker():
    from void.memory.persist import was_redacted
    plain = Task(goal="g")
    assert not was_redacted(plain)
    r = Task(goal="g"); r.result = ANSWER_PLACEHOLDER
    m = Task(goal="g"); m.messages = [{"role": "assistant", "content": ANSWER_PLACEHOLDER}]
    c = Task(goal="g"); c.messages = [{"role": "assistant", "content": None, "tool_calls": [{"name": "x", "arguments": {"p": ARG_PLACEHOLDER}}]}]
    p = Task(goal="g"); p.pending = {"assistant_text": None, "tool_calls": [], "memory_protected": True}   # awaiting step, no prose
    assert all(was_redacted(t) for t in (r, m, c, p))
    assert not was_redacted(Task(goal="g", result="a normal answer"))


# ================================================================== residue in the file, and echoed arguments
def test_a_cleared_value_does_not_linger_in_the_task_database_file(tmp_path):
    """SQLite keeps overwritten row content in free pages unless secure_delete is on. A step's pending arguments are
    kept verbatim only until the owner decides; once cleared they must not remain readable in tasks.sqlite."""
    from void.core.task import TaskStore
    store = TaskStore(tmp_path / "t.sqlite")
    t = Task(goal="g")
    t.pending = {"tool_calls": [{"name": "x", "arguments": {"p": SECRET * 30}}]}
    store.save(t)
    assert SECRET.encode() in (tmp_path / "t.sqlite").read_bytes()                       # written, as it must be...
    t.pending, t.result = None, "resolved"
    store.save(t)                                                                        # ...then cleared
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("t.sqlite*"))
    assert SECRET.encode() not in raw


def test_tool_output_that_echoes_a_model_supplied_argument_is_scrubbed_in_stored_history():
    """The model put a memory fragment in a path; the tool's result repeats that path. No exact memory sentence is
    involved, so the scrub must key on the argument values the model supplied."""
    t = Task(goal="g")
    victim = "C:/work/" + SECRET + ".txt"
    t.messages = [
        {"role": "system", "content": "s"}, {"role": "user", "content": "g"},
        {"role": "assistant", "content": None, "tool_calls": [{"name": "delete_file", "arguments": {"path": victim}}]},
        {"role": "tool", "name": "delete_file", "content": "Moved " + victim + " to the Recycle Bin"},
    ]
    t.error = "could not touch " + victim
    t.plan = [{"index": 1, "calls": [{"tool": "delete_file", "arguments_summary": "path=" + victim}], "outcome_summary": "moved " + victim}]
    s = redact_task(t, Injection(protected=(), carries_memory=True))                     # no memory string known at all
    assert SECRET not in json.dumps(s.messages) and SECRET not in s.error and SECRET not in json.dumps(s.plan)
    assert s.messages[3]["content"] == "Moved [memory] to the Recycle Bin"
    assert SECRET in t.messages[3]["content"]                                             # the live task is untouched


def test_short_and_non_string_arguments_do_not_garble_stored_output():
    t = Task(goal="g")
    t.messages = [
        {"role": "assistant", "content": None, "tool_calls": [{"name": "t", "arguments": {"n": 5, "flag": True, "s": "ab"}}]},
        {"role": "tool", "name": "t", "content": "ab 5 True result ab"},
    ]
    s = redact_task(t, Injection(protected=(), carries_memory=True))
    assert s.messages[1]["content"] == "ab 5 True result ab"                              # < 3 chars / non-strings: not scrubbed
