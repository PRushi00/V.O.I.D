"""Stop must land, and a task must always leave "working".

The reported failure: the owner said "Open my chat", V.O.I.D showed *working* for over two minutes,
WhatsApp never opened, pressing Stop did not stop it, and no further command could be issued. Three
separate defects, each reproduced on the real machine before being fixed, and each pinned here.

**1. Stop could not interrupt a model call.** The kill switch is cooperative - it stops the *next*
step from starting - and a model call is a blocking HTTP request carrying the provider's own
deadline, up to 120 seconds for the local model. So a Stop pressed one second into that call went
unnoticed until it finished. Measured before the fix: 5.7s on a fast cloud answer, and bounded only
by the provider timeout in the bad case. Now bounded by ``_STOP_POLL_S``, measured at 0.17s.

**2. The retry backoff was uninterruptible.** ``time.sleep`` between attempts ignored the switch, so
a Stop during a backoff waited the backoff out. The same class of delay, just as avoidable.

**3. Once stopped, every command was a silent dead end.** Every path in ``Assistant.run`` was gated
on the switch, and ``task.result`` is only ever set on COMPLETED, so each command created a task,
paused it, and returned a result of ``None``. The owner saw and heard nothing, and "resume",
"continue" and "rearm" behaved identically because control commands were skipped while engaged.
Only the tray's Rearm item or the CLI could recover. The fix reports; it deliberately does **not**
clear the stop, because that is a security control.

The late-result rule is the subtle one and has its own tests: a call that finishes *after* the owner
stopped must not be able to resurrect the run.
"""
from __future__ import annotations

import threading
import time

import pytest

from void.actions.base import Tool, ToolResult
from void.actions.registry import ToolRegistry
from void.core.agent import _STOP_POLL_S, Agent
from void.core.kill_switch import KillSwitch, StopRequested
from void.core.task import Status, TaskStore
from void.providers.base import LLMProvider, LLMResponse
from void.security.risk import RiskGate

from tests.helpers import FakeProvider, tool_call


class BlockingProvider(LLMProvider):
    """A provider whose call blocks, the way a real HTTP request does.

    ``started`` lets a test wait until the call is genuinely in flight before stopping, so the test
    exercises interruption rather than the pre-call check that already worked.
    """

    name = "blocking"

    def available(self) -> bool:
        return True

    def __init__(self, block_s: float = 30.0, answer: LLMResponse | None = None):
        self.block_s = block_s
        self.answer = answer or LLMResponse(text="done")
        self.started = threading.Event()
        self.finished = threading.Event()
        self.calls = 0

    def generate(self, messages, tools=None):
        self.calls += 1
        self.started.set()
        time.sleep(self.block_s)
        self.finished.set()
        return self.answer


class FailingThenBlockingProvider(LLMProvider):
    """Fails once with a retryable error, then blocks - so the retry BACKOFF is what is in flight."""

    name = "flaky"

    def available(self) -> bool:
        return True

    def __init__(self, block_s: float = 30.0):
        self.block_s = block_s
        self.calls = 0
        self.first_failed = threading.Event()

    def generate(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.first_failed.set()
            raise RuntimeError("503 Service Unavailable")
        time.sleep(self.block_s)
        return LLMResponse(text="done")


def build(tmp_path, provider, *, max_steps=12, kill_switch=None):
    return Agent(provider=provider, tools=ToolRegistry(),
                 risk_gate=RiskGate(confirm_at_or_above="high"),
                 kill_switch=kill_switch or KillSwitch(),
                 store=TaskStore(tmp_path / "tasks.sqlite"), max_steps=max_steps)


def run_in_thread(fn):
    box: dict = {}

    def work():
        try:
            box["result"] = fn()
        except BaseException as exc:                            # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, box


# =========================================================================== stop reaches the call

def test_stop_interrupts_a_model_call_in_flight(tmp_path):
    """The headline fix. Before it, this waited out the provider's whole blocking call."""
    provider = BlockingProvider(block_s=30.0)
    switch = KillSwitch()
    agent = build(tmp_path, provider, kill_switch=switch)

    thread, box = run_in_thread(lambda: agent.run("open my chat"))
    assert provider.started.wait(timeout=10), "the provider call never started"
    stopped_at = time.monotonic()
    switch.engage("test stop")
    thread.join(timeout=10)

    assert not thread.is_alive(), "the run did not return after Stop"
    latency = time.monotonic() - stopped_at
    assert latency < 5.0, f"Stop took {latency:.1f}s to land"
    assert box["result"].status == Status.PAUSED


def test_stop_during_a_retry_backoff_lands_promptly(tmp_path):
    """A Stop arriving while the agent is sleeping between attempts must not wait out the sleep."""
    provider = FailingThenBlockingProvider(block_s=30.0)
    switch = KillSwitch()
    agent = build(tmp_path, provider, kill_switch=switch)

    thread, box = run_in_thread(lambda: agent.run("open my chat"))
    assert provider.first_failed.wait(timeout=10)
    stopped_at = time.monotonic()
    switch.engage("test stop")
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert (time.monotonic() - stopped_at) < 5.0
    assert box["result"].status == Status.PAUSED


def test_the_poll_interval_is_short_enough_to_feel_immediate():
    """What bounds Stop is this interval, not the provider's timeout. Guarded so it stays small."""
    assert 0 < _STOP_POLL_S <= 0.5


def test_a_task_paused_by_stop_is_recorded_as_paused_not_left_running(tmp_path):
    """The UI reads task status. A task left `running` by a Stop is a task stuck in "working"."""
    provider = BlockingProvider(block_s=30.0)
    switch = KillSwitch()
    agent = build(tmp_path, provider, kill_switch=switch)
    thread, box = run_in_thread(lambda: agent.run("open my chat"))
    assert provider.started.wait(timeout=10)
    switch.engage("test stop")
    thread.join(timeout=10)

    stored = agent.store.load(box["result"].task.id)
    assert stored.status == Status.PAUSED
    assert stored.status != Status.RUNNING


# =========================================================================== late results

def test_a_late_answer_cannot_resurrect_a_stopped_run(tmp_path):
    """The call keeps running on its daemon thread - nothing can abort another thread's socket read.
    What must hold is that its answer is DISCARDED and no further step executes."""
    ran = []
    tools = ToolRegistry()
    tools.register_all([Tool(name="touch", description="d",
                             parameters={"type": "object", "properties": {}, "required": []},
                             handler=lambda: (ran.append(1), ToolResult.success("ok"))[1])])
    provider = BlockingProvider(
        block_s=1.5, answer=LLMResponse(tool_calls=[tool_call("touch")]))
    switch = KillSwitch()
    agent = Agent(provider=provider, tools=tools,
                  risk_gate=RiskGate(confirm_at_or_above="high"), kill_switch=switch,
                  store=TaskStore(tmp_path / "tasks.sqlite"))

    thread, box = run_in_thread(lambda: agent.run("open my chat"))
    assert provider.started.wait(timeout=10)
    switch.engage("test stop")
    thread.join(timeout=10)
    assert box["result"].status == Status.PAUSED

    # Give the orphaned call time to finish into the dict nobody reads.
    assert provider.finished.wait(timeout=10), "the abandoned call never completed"
    time.sleep(0.3)
    assert ran == [], "a tool ran from an answer that arrived after the owner stopped"
    assert agent.store.load(box["result"].task.id).status == Status.PAUSED


def test_a_stopped_run_does_not_keep_calling_the_provider(tmp_path):
    """No hidden continuation: one call was in flight, and no further call is made."""
    provider = BlockingProvider(block_s=1.0)
    switch = KillSwitch()
    agent = build(tmp_path, provider, kill_switch=switch)
    thread, _box = run_in_thread(lambda: agent.run("open my chat"))
    assert provider.started.wait(timeout=10)
    switch.engage("test stop")
    thread.join(timeout=10)
    time.sleep(1.5)
    assert provider.calls == 1, f"the provider was called {provider.calls} times after a Stop"


def test_a_worker_that_records_nothing_becomes_a_provider_failure(tmp_path):
    """Defensive: the call must never return None upwards and be mistaken for an answer."""
    from void.providers.base import ProviderUnavailable

    class Silent(LLMProvider):
        name = "silent"

        def available(self) -> bool:
            return True

        def generate(self, messages, tools=None):
            raise SystemExit("thread died oddly")

    agent = build(tmp_path, Silent())
    with pytest.raises((ProviderUnavailable, SystemExit)):
        agent._generate_interruptibly([{"role": "user", "content": "x"}], None)


# =========================================================================== interruptible sleep

def test_the_backoff_sleep_returns_normally_when_nothing_is_stopped(tmp_path):
    agent = build(tmp_path, FakeProvider([LLMResponse(text="ok")]))
    started = time.monotonic()
    agent._sleep_unless_stopped(0.3)
    assert 0.25 <= (time.monotonic() - started) < 2.0


def test_the_backoff_sleep_raises_at_once_when_already_stopped(tmp_path):
    switch = KillSwitch()
    switch.engage("test")
    agent = build(tmp_path, FakeProvider([LLMResponse(text="ok")]), kill_switch=switch)
    started = time.monotonic()
    with pytest.raises(StopRequested):
        agent._sleep_unless_stopped(30.0)
    assert (time.monotonic() - started) < 1.0


def test_a_negative_or_zero_backoff_is_harmless(tmp_path):
    agent = build(tmp_path, FakeProvider([LLMResponse(text="ok")]))
    agent._sleep_unless_stopped(0)
    agent._sleep_unless_stopped(-5)


# =========================================================================== every task terminates

def test_an_ordinary_run_reaches_a_terminal_state(tmp_path):
    agent = build(tmp_path, FakeProvider([LLMResponse(text="here you go")]))
    assert agent.run("do something").status == Status.COMPLETED


def test_the_step_cap_is_a_terminal_failure_not_an_endless_loop(tmp_path):
    """A model that only ever calls tools must still stop, and stop as FAILED rather than running."""
    agent = build(tmp_path, FakeProvider(
        [LLMResponse(tool_calls=[tool_call("nope")])] * 20), max_steps=3)
    result = agent.run("loop forever")
    assert result.status == Status.FAILED
    assert "max_steps" in (result.task.error or "")
    assert agent.store.load(result.task.id).status == Status.FAILED


def test_a_provider_that_never_answers_ends_as_failed_not_running(tmp_path):
    class Broken(LLMProvider):
        name = "broken"

        def available(self) -> bool:
            return True

        def generate(self, messages, tools=None):
            raise RuntimeError("401 invalid api key")

    agent = build(tmp_path, Broken())
    result = agent.run("do something")
    assert result.status == Status.FAILED
    assert agent.store.load(result.task.id).status != Status.RUNNING


def test_no_terminal_status_is_left_as_running(tmp_path):
    """Whatever happens, the stored task must not still say `running` - that IS "stuck working"."""
    for script in ([LLMResponse(text="ok")],
                   [LLMResponse(tool_calls=[tool_call("nope")]), LLMResponse(text="ok")],
                   [LLMResponse(tool_calls=[tool_call("nope")])] * 20):
        agent = build(tmp_path, FakeProvider(script), max_steps=3)
        result = agent.run("x")
        assert agent.store.load(result.task.id).status != Status.RUNNING
