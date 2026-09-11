"""Interactive directory disambiguation (ambiguous find_directory -> ASK -> pick).

Deterministic - FakeProvider only, temp dirs only, no Gemini. Verifies that a
natural-language command resolving to MULTIPLE directories now BLOCKS with a
durable, numbered candidate prompt instead of dead-ending; that the owner's
EXPLICIT numeric choice (never the LLM, never a heuristic) selects the exact
candidate captured at search time; that the original operation then continues
through the UNCHANGED confinement / RiskGate pipeline; and that the safety model
(0 -> NOT_FOUND, 1 -> RESOLVED, >1 -> unresolved-until-chosen) is preserved.

Tests never assume a filesystem-walk order: a candidate is located by its exact
path and selected by *its* index, so ordering is irrelevant.
"""
import json
from pathlib import Path

from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.core.agent import Agent
from void.core.kill_switch import KillSwitch
from void.core.task import Status, Task, TaskStore
from void.providers.base import LLMResponse
from void.security.risk import RiskGate

from tests.helpers import FakeProvider, tool_call


def _agent(tmp_path, script, *, defer=False, confirm_fn=None, max_steps=12):
    files = FileActions(allowed_roots=[tmp_path])
    tools = ToolRegistry()
    tools.register_all(files.tools())
    gate = RiskGate(confirm_at_or_above="high", confirm_fn=confirm_fn)
    agent = Agent(FakeProvider(script), tools, gate, KillSwitch(),
                  TaskStore(tmp_path / "t.sqlite"),
                  max_steps=max_steps, max_retries=0, defer_confirmation=defer)
    return agent


def _mk(*dirs):
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _load(agent, tid):
    return agent.store.load(tid)


def _index_of(pending, path):
    """The 1-based candidate index whose exact path == ``path`` (order-free)."""
    for c in pending["candidates"]:
        if c["path"] == str(path):
            return c["index"]
    raise AssertionError(f"{path} not among candidates {pending['candidates']}")


def _continue_with_write(agent, dir_path, filename, content, final="done"):
    """Re-script the fake model to write <filename> into the resolved dir, then
    finish. Mirrors the model resuming the ORIGINAL operation post-clarification."""
    agent.provider._script = [
        LLMResponse(tool_calls=[tool_call(
            "write_file", path=str(Path(dir_path) / filename), content=content)]),
        LLMResponse(text=final),
    ]
    agent.provider.calls = 0


# --- 1 / 2: 0 and 1 match are UNCHANGED ------------------------------------

def test_one_directory_resolves_normally(tmp_path):
    _mk(tmp_path / "Hackathon")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Hackathon")]),
        LLMResponse(text="found it"),
    ])
    result = agent.run("find Hackathon")
    assert result.status == Status.COMPLETED
    task = _load(agent, result.task.id)
    assert task.pending is None
    assert task.plan[0]["status"] == "succeeded"


def test_zero_directories_still_not_found(tmp_path):
    _mk(tmp_path / "something")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Nope")]),
        LLMResponse(text="No such folder was found."),
    ])
    result = agent.run("find Nope")
    assert result.status == Status.COMPLETED and result.status != Status.BLOCKED
    task = _load(agent, result.task.id)
    assert task.pending is None
    tmsgs = [m for m in task.messages if m["role"] == "tool"]
    assert "No directories matched" in tmsgs[0]["content"]


# --- 3 / 4: >1 blocks, executes nothing, presents ALL candidates --------

def test_multiple_directories_block_without_executing(tmp_path):
    _mk(tmp_path / "x" / "Projects", tmp_path / "y" / "Projects")
    target = tmp_path / "x" / "Projects" / "test.txt"
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
        LLMResponse(tool_calls=[tool_call("write_file", path=str(target),
                                          content="x")]),   # must NOT run
    ])
    result = agent.run("create test.txt in Projects")
    assert result.status == Status.BLOCKED
    assert not target.exists()
    task = _load(agent, result.task.id)
    assert len(task.plan) == 1
    assert task.plan[0]["calls"][0]["tool"] == "find_directory"
    assert agent.provider.calls == 1              # queued write turn never fired


def test_all_candidates_presented_with_exact_paths_and_neutral_prompt(tmp_path):
    a = tmp_path / "x" / "Projects"
    b = tmp_path / "y" / "z" / "Projects"
    c = tmp_path / "Projects"
    _mk(a, b, c)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
        LLMResponse(text="ambiguous"),
    ])
    result = agent.run("open Projects")
    pending = _load(agent, result.task.id).pending
    assert pending["kind"] == "directory_disambiguation"
    assert {x["path"] for x in pending["candidates"]} == {str(a), str(b), str(c)}
    assert [x["index"] for x in pending["candidates"]] == [1, 2, 3]
    assert isinstance(pending.get("created_at"), float)     # lifecycle info

    prompt = pending["prompt"]
    for p in (a, b, c):
        assert str(p) in prompt                   # every candidate shown
    low = prompt.lower()
    assert "which directory should i use" in low
    assert "reply with 1, 2, or 3" in low
    for biased in ("recommend", "suggest", " best ", "most likely",
                   "first one", "i picked", "i'll use", "i will use"):
        assert biased not in low                  # no preference implied


# --- 5 / 6: explicit numeric selection binds the EXACT candidate -------

def test_select_candidate_one_uses_exactly_that_directory(tmp_path):
    a = tmp_path / "aa" / "Projects"
    b = tmp_path / "bb" / "Projects"
    _mk(a, b)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ])
    blocked = agent.run("create note.txt in Projects")
    assert blocked.status == Status.BLOCKED
    pending = _load(agent, blocked.task.id).pending
    pick = _index_of(pending, a)
    _continue_with_write(agent, a, "note.txt", "hi")
    result = agent.resume_clarification(_load(agent, blocked.task.id), str(pick))
    assert result.status == Status.COMPLETED
    assert (a / "note.txt").read_text() == "hi"
    assert not (b / "note.txt").exists()


def test_select_last_candidate_uses_exactly_that_directory(tmp_path):
    a = tmp_path / "aa" / "Projects"
    b = tmp_path / "bb" / "Projects"
    c = tmp_path / "cc" / "Projects"
    _mk(a, b, c)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ])
    blocked = agent.run("create note.txt in Projects")
    pending = _load(agent, blocked.task.id).pending
    assert len(pending["candidates"]) == 3
    pick = _index_of(pending, c)
    _continue_with_write(agent, c, "note.txt", "z")
    result = agent.resume_clarification(_load(agent, blocked.task.id), str(pick))
    assert result.status == Status.COMPLETED
    assert (c / "note.txt").exists()
    assert not (a / "note.txt").exists() and not (b / "note.txt").exists()


# --- 7 / 8: invalid + out-of-range selections leave it unresolved -----

def test_invalid_selection_keeps_task_blocked(tmp_path):
    _mk(tmp_path / "aa" / "Data", tmp_path / "bb" / "Data")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Data")]),
    ])
    blocked = agent.run("open Data")
    for bad in ("banana", "the second one", "1 or 2", "", "  ", "yes", "last"):
        r = agent.resume_clarification(_load(agent, blocked.task.id), bad)
        assert r.status == Status.BLOCKED
        p = _load(agent, blocked.task.id).pending
        assert p and p["kind"] == "directory_disambiguation"
        assert len(p["candidates"]) == 2          # nothing consumed / changed
    assert agent.provider.calls == 1             # never re-entered the LLM loop


def test_out_of_range_selection_keeps_task_blocked(tmp_path):
    _mk(tmp_path / "aa" / "Data", tmp_path / "bb" / "Data")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Data")]),
    ])
    blocked = agent.run("open Data")
    for bad in ("0", "3", "99", "-1"):
        r = agent.resume_clarification(_load(agent, blocked.task.id), bad)
        assert r.status == Status.BLOCKED
        assert _load(agent, blocked.task.id).pending["kind"] == \
            "directory_disambiguation"


# --- 9: a selection can never trigger a different filesystem search ---

def test_selection_binds_captured_candidate_not_a_fresh_search(tmp_path):
    a = tmp_path / "aa" / "Projects"
    b = tmp_path / "bb" / "Projects"
    _mk(a, b)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ])
    blocked = agent.run("create x.txt in Projects")
    pending = _load(agent, blocked.task.id).pending
    captured = [c["path"] for c in pending["candidates"]]

    # The filesystem changes AFTER the prompt: a new same-named directory that
    # any fuzzy re-search would pick up. The bound candidate set must not move.
    _mk(tmp_path / "zz" / "Projects")
    reloaded = _load(agent, blocked.task.id)
    assert [c["path"] for c in reloaded.pending["candidates"]] == captured

    pick = _index_of(reloaded.pending, b)
    _continue_with_write(agent, b, "x.txt", "2")
    result = agent.resume_clarification(reloaded, str(pick))
    assert result.status == Status.COMPLETED
    assert (b / "x.txt").exists()                          # original candidate
    assert not (tmp_path / "zz" / "Projects" / "x.txt").exists()
    task = _load(agent, blocked.task.id)
    n_finds = [c["tool"] for e in task.plan for c in e["calls"]].count(
        "find_directory")
    assert n_finds == 1                                    # no second search


# --- 10: the original operation (and its arguments) survives ---------

def test_original_operation_arguments_survive_clarification(tmp_path):
    a = tmp_path / "one" / "Projects"
    b = tmp_path / "two" / "Projects"
    _mk(a, b)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ])
    blocked = agent.run(
        "Create a file named quarterly_report.txt in the Projects folder "
        "with the text FINAL")
    assert blocked.status == Status.BLOCKED
    task = _load(agent, blocked.task.id)
    assert "quarterly_report.txt" in task.goal and "FINAL" in task.goal
    pick = _index_of(task.pending, a)
    _continue_with_write(agent, a, "quarterly_report.txt", "FINAL",
                         final="Created quarterly_report.txt in Projects.")
    result = agent.resume_clarification(task, str(pick))
    assert result.status == Status.COMPLETED
    out = a / "quarterly_report.txt"
    assert out.exists() and out.read_text() == "FINAL"
    # the owner never re-stated the request; only one user 'clarification' msg
    reloaded = _load(agent, blocked.task.id)
    clar = [m for m in reloaded.messages
            if m.get("role") == "user" and "[owner clarification]" in
            (m.get("content") or "")]
    assert len(clar) == 1 and str(a) in clar[0]["content"]


# --- 11: RiskGate is UNCHANGED after clarification ------------------

def test_riskgate_still_gates_existing_file_overwrite_after_clarification(tmp_path):
    a = tmp_path / "one" / "Projects"
    b = tmp_path / "two" / "Projects"
    _mk(a, b)
    (a / "report.txt").write_text("OLD")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ], defer=True)                                # headless -> HIGH is deferred
    blocked = agent.run("overwrite report.txt in Projects with NEW")
    pending = _load(agent, blocked.task.id).pending
    pick = _index_of(pending, a)
    agent.provider._script = [
        LLMResponse(tool_calls=[tool_call(
            "write_file", path=str(a / "report.txt"),
            content="NEW", overwrite=True)]),
    ]
    agent.provider.calls = 0
    result = agent.resume_clarification(_load(agent, blocked.task.id), str(pick))
    assert result.status == Status.AWAITING_CONFIRMATION    # HIGH still gated
    assert (a / "report.txt").read_text() == "OLD"          # not executed
    pend = _load(agent, blocked.task.id).pending
    assert pend["tool_calls"][0]["name"] == "write_file"
    assert pend["tool_calls"][0]["requires_confirmation"] is True


def test_new_file_after_clarification_proceeds_autonomously(tmp_path):
    a = tmp_path / "one" / "Projects"
    b = tmp_path / "two" / "Projects"
    _mk(a, b)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ], defer=True)
    blocked = agent.run("create fresh.txt in Projects")
    pending = _load(agent, blocked.task.id).pending
    pick = _index_of(pending, b)
    _continue_with_write(agent, b, "fresh.txt", "hi", final="created")
    result = agent.resume_clarification(_load(agent, blocked.task.id), str(pick))
    assert result.status == Status.COMPLETED       # new file = MEDIUM, autonomous
    assert (b / "fresh.txt").read_text() == "hi"


# --- 12: an unrelated request cannot consume an old clarification ---

def test_unrelated_request_does_not_consume_pending_clarification(tmp_path):
    _mk(tmp_path / "aa" / "Projects", tmp_path / "bb" / "Projects")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ])
    blocked = agent.run("open Projects")
    assert blocked.status == Status.BLOCKED
    bid = blocked.task.id

    agent.provider._script = [LLMResponse(text="hello there")]
    agent.provider.calls = 0
    other = agent.run("say hello")
    assert other.status == Status.COMPLETED and other.task.id != bid

    still = _load(agent, bid)
    assert still.status == Status.BLOCKED
    assert still.pending["kind"] == "directory_disambiguation"
    assert len(still.pending["candidates"]) == 2
    assert agent.resume(still).status == Status.BLOCKED     # plain resume: no-op

    a = tmp_path / "aa" / "Projects"
    pick = _index_of(_load(agent, bid).pending, a)
    _continue_with_write(agent, a, "k.txt", "k")
    done = agent.resume_clarification(_load(agent, bid), str(pick))
    assert done.status == Status.COMPLETED
    assert (a / "k.txt").exists()


def test_resume_pending_never_executes_a_disambiguation_pending(tmp_path):
    _mk(tmp_path / "aa" / "Data", tmp_path / "bb" / "Data")
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Data")]),
    ])
    blocked = agent.run("open Data")
    r = agent.resume_pending(_load(agent, blocked.task.id), decision=True)
    assert r.status == Status.BLOCKED
    assert _load(agent, blocked.task.id).pending["kind"] == \
        "directory_disambiguation"


# --- 13: lifecycle across persistence / reload --------------------

def test_pending_clarification_survives_store_reload(tmp_path):
    a = tmp_path / "aa" / "Projects"
    b = tmp_path / "bb" / "Projects"
    _mk(a, b)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
    ])
    blocked = agent.run("create p.txt in Projects")
    tid = blocked.task.id

    store2 = TaskStore(tmp_path / "t.sqlite")     # simulate a fresh process
    reloaded = store2.load(tid)
    assert reloaded.status == Status.BLOCKED
    assert reloaded.pending["kind"] == "directory_disambiguation"
    json.dumps(reloaded.pending)                  # fully JSON-serializable
    assert {c["path"] for c in reloaded.pending["candidates"]} == \
        {str(a), str(b)}                          # exact paths survived the DB

    files = FileActions(allowed_roots=[tmp_path])
    tools = ToolRegistry()
    tools.register_all(files.tools())
    pick = _index_of(reloaded.pending, b)
    agent2 = Agent(
        FakeProvider([
            LLMResponse(tool_calls=[tool_call(
                "write_file", path=str(b / "p.txt"), content="ok")]),
            LLMResponse(text="done"),
        ]),
        tools, RiskGate(confirm_at_or_above="high"), KillSwitch(), store2,
        max_retries=0)
    result = agent2.resume_clarification(reloaded, str(pick))
    assert result.status == Status.COMPLETED
    assert (b / "p.txt").read_text() == "ok"


# --- selection parser (deterministic, numeric-only) -------------

def test_selection_parser_accepts_only_deterministic_numeric_forms():
    f = Agent._parse_directory_selection
    assert f("1", 3) == 1
    assert f("  2 ", 3) == 2
    assert f("3", 3) == 3
    assert f("#2", 3) == 2
    assert f("number 2", 3) == 2
    assert f("choose 3", 3) == 3
    assert f("option 1", 3) == 1
    assert f("select 2", 3) == 2
    for bad in ("two", "second", "1 or 2", "1,2", "", None, "0", "4", "-1",
                "1.5", "the first", "yes", "y", "n", "1st", "pick one",
                "delete everything"):
        assert f(bad, 3) is None
    assert f("1", 0) is None                       # no candidates -> nothing


# --- realistic multi-"Projects" example -----------------------

def test_realistic_multiple_projects_directories(tmp_path):
    onedrive = tmp_path / "Users" / "nanda" / "OneDrive"
    d1 = onedrive / "Projects"
    d2 = onedrive / "Desktop" / "Projects"
    d3 = tmp_path / "Users" / "nanda" / "Documents" / "Projects"
    _mk(d1, d2, d3)
    agent = _agent(tmp_path, [
        LLMResponse(tool_calls=[tool_call("find_directory", query="Projects")]),
        LLMResponse(tool_calls=[tool_call("write_file",
                                          path=str(d1 / "wrong.txt"),
                                          content="no")]),   # must NOT run
    ])
    blocked = agent.run("Create a file named test.txt in the Projects folder.")
    assert blocked.status == Status.BLOCKED
    assert not (d1 / "wrong.txt").exists()
    pending = _load(agent, blocked.task.id).pending
    assert {c["path"] for c in pending["candidates"]} == \
        {str(d1), str(d2), str(d3)}
    assert 'directories named "Projects"' in pending["prompt"]

    pick = _index_of(pending, d2)                  # owner picks the Desktop one
    _continue_with_write(agent, d2, "test.txt", "",
                         final="Created test.txt in your Desktop Projects.")
    result = agent.resume_clarification(_load(agent, blocked.task.id), str(pick))
    assert result.status == Status.COMPLETED
    assert (d2 / "test.txt").exists()
    assert not (d1 / "test.txt").exists() and not (d3 / "test.txt").exists()
    task = _load(agent, blocked.task.id)
    assert task.pending is None and task.error is None


# --- Terminal tasks with a STALE disambiguation pending must never clarify --
#
# Regression for the same class of defect just fixed in resume()/resume_pending():
# a task whose status is already COMPLETED/FAILED/CANCELLED, but which still
# carries a leftover pending{"kind": "directory_disambiguation"} payload, must
# never be clarified - clarify() must never re-enter the loop, generate a new
# LLM turn, execute a tool, or mutate the task's persisted state.

def _stale_disambiguation_task(goal, status, candidate_dir):
    task = Task(goal=goal, status=status)
    task.pending = {
        "kind": "directory_disambiguation",
        "candidates": [{"index": 1, "name": "Projects",
                        "path": str(candidate_dir)}],
        "prompt": "which?", "created_at": 0.0,
    }
    return task


def test_completed_task_with_stale_disambiguation_cannot_clarify(tmp_path):
    projects = tmp_path / "aa" / "Projects"
    _mk(projects)
    # If the guard failed, this scripted call WOULD run.
    agent = _agent(tmp_path, [LLMResponse(tool_calls=[tool_call(
        "write_file", path=str(projects / "wrong.txt"), content="no")])])

    task = _stale_disambiguation_task(
        "create note.txt in Projects", Status.COMPLETED, projects)
    agent.store.save(task)

    result = agent.resume_clarification(_load(agent, task.id), "1")

    assert result.status == Status.COMPLETED           # unchanged, still terminal
    assert agent.provider.calls == 0                    # no new LLM turn
    assert not (projects / "wrong.txt").exists()         # no tool side effect
    final = _load(agent, task.id)
    assert final.status == Status.COMPLETED
    assert final.pending is not None                     # not consumed/cleared
    assert final.pending["kind"] == "directory_disambiguation"


def test_cancelled_task_with_stale_disambiguation_cannot_clarify_or_execute(tmp_path):
    projects = tmp_path / "aa" / "Projects"
    _mk(projects)
    agent = _agent(tmp_path, [LLMResponse(tool_calls=[tool_call(
        "write_file", path=str(projects / "wrong.txt"), content="no")])])

    task = _stale_disambiguation_task(
        "create note.txt in Projects", Status.CANCELLED, projects)
    agent.store.save(task)

    result = agent.resume_clarification(_load(agent, task.id), "1")

    assert result.status == Status.CANCELLED            # unchanged, still terminal
    assert agent.provider.calls == 0
    assert not (projects / "wrong.txt").exists()
    final = _load(agent, task.id)
    assert final.status == Status.CANCELLED
    assert final.pending is not None
    assert final.pending["candidates"][0]["path"] == str(projects)  # untouched


def test_failed_task_with_stale_disambiguation_cannot_clarify_or_execute(tmp_path):
    projects = tmp_path / "aa" / "Projects"
    _mk(projects)
    agent = _agent(tmp_path, [LLMResponse(tool_calls=[tool_call(
        "write_file", path=str(projects / "wrong.txt"), content="no")])])

    task = _stale_disambiguation_task(
        "create note.txt in Projects", Status.FAILED, projects)
    agent.store.save(task)

    result = agent.resume_clarification(_load(agent, task.id), "1")

    assert result.status == Status.FAILED               # unchanged, still terminal
    assert agent.provider.calls == 0
    assert not (projects / "wrong.txt").exists()
    final = _load(agent, task.id)
    assert final.status == Status.FAILED
    assert final.pending is not None
