"""Interruption: stopping speech, and keeping that distinct from stopping the work.

The playback mechanics - supersede, purge, the start/stop race, no audio after close - are already covered
by ``tests/test_voice.py``. What is tested here is the part that spans layers, which is where interruption
goes wrong in practice:

* a spoken "stop" reaching the TTS backend through the deterministic control chain, with no model involved;
* "stop talking", "stop the task" and "pause" staying three different things;
* a bare "stop" never being able to abandon work, from any state;
* a completion callback that arrives after an interruption not resuming anything;
* cancelling asking which job, when more than one could be meant.

No microphone is required: the audio input path is not under test here, the state machine and the wiring
are. Hardware validation of the microphone itself is not possible on this machine and is reported as such
rather than simulated.
"""
from __future__ import annotations

import threading
import time

import pytest

from void.orchestration.commands import ControlContext, ControlIntent, classify
from void.voice.adapters import SapiTTS


# --------------------------------------------------------------------------- a deterministic SAPI voice

class _FakeVoice:
    """Enough of SAPI's SpVoice to drive the real adapter without a sound card.

    Mirrors the seam ``tests/test_voice.py`` already uses: ``Speak`` records the call, a purge flag cuts
    the current utterance, and ``WaitUntilDone`` blocks until the test says the utterance finished.
    """

    _PURGE = 2

    def __init__(self, interruptible=True):
        self._lock = threading.Condition()
        self._finish = threading.Event()
        self._purged = threading.Event()
        self.calls: list[tuple] = []
        self.Rate = 0
        self._interruptible = interruptible

    def Speak(self, text, flags):                              # noqa: N802 - SAPI's name
        with self._lock:
            self.calls.append((text, flags))
            self._lock.notify_all()
        if flags & self._PURGE:
            self._purged.set()
            if self._interruptible:
                self._finish.set()
            return 0
        self._finish.clear()
        return 0

    def WaitUntilDone(self, ms):                               # noqa: N802 - SAPI's name
        return self._finish.wait(ms / 1000.0)

    @property
    def AudioOutput(self):                                     # noqa: N802 - SAPI's name
        return type("Out", (), {"GetDescription": staticmethod(lambda: "fake device")})()

    def finish_utterance(self):
        self._finish.set()

    def wait_purged(self, timeout=2.0):
        return self._purged.wait(timeout)

    @property
    def utterances(self):
        return [text for (text, _flags) in self.calls if text]

    def wait_utterances(self, count, timeout=2.0):
        deadline = time.time() + timeout
        with self._lock:
            while len(self.utterances) < count:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._lock.wait(left)
        return True


@pytest.fixture
def tts():
    voice = _FakeVoice()
    engine = SapiTTS(_voice_factory=lambda: voice, _com_setup=lambda: None,
                     _com_teardown=lambda: None, _poll_ms=10)
    try:
        yield engine, voice
    finally:
        engine.close()


# --------------------------------------------------------------------------- the control chain

def _speaking(**kwargs):
    base = {"speaking": True, "task_running": False, "task_paused": False}
    base.update(kwargs)
    return ControlContext(**base)


def _working(**kwargs):
    base = {"speaking": False, "task_running": True, "task_paused": False}
    base.update(kwargs)
    return ControlContext(**base)


@pytest.mark.parametrize("phrase", ["stop", "stop it", "quiet", "be quiet", "shush",
                                    "enough", "ok stop", "shut up"])
def test_a_bare_stop_while_speaking_means_be_quiet(phrase):
    """The common case, and the one that must never be read as abandoning work."""
    assert classify(phrase, _speaking()).intent == ControlIntent.STOP_SPEAKING


@pytest.mark.parametrize("phrase", ["stop talking", "stop speaking", "be quiet now", "mute",
                                    "stop narrating", "stop reading"])
def test_stop_talking_is_about_speech_from_any_state(phrase):
    """Explicitly about speech, so it is never read as a task command whatever is happening."""
    for context in (_speaking(), _working(), _working(speaking=True),
                    ControlContext(speaking=False, task_running=False, task_paused=False)):
        assert classify(phrase, context).intent == ControlIntent.STOP_SPEAKING


@pytest.mark.parametrize("phrase", ["stop", "stop it", "quiet", "enough", "that's enough"])
def test_a_bare_stop_can_never_cancel_the_work(phrase):
    """Cancelling is not recoverable, so it needs saying. A vague word must not reach it."""
    for context in (_speaking(), _working(), _working(speaking=True),
                    ControlContext(speaking=False, task_running=False, task_paused=False),
                    ControlContext(speaking=False, task_running=False, task_paused=True)):
        assert classify(phrase, context).intent != ControlIntent.CANCEL_TASK


def test_pause_stop_and_cancel_stay_three_different_things():
    assert classify("pause that", _working()).intent == ControlIntent.PAUSE_TASK
    assert classify("stop talking", _working(speaking=True)).intent == ControlIntent.STOP_SPEAKING
    assert classify("cancel that", _working()).intent == ControlIntent.CANCEL_TASK
    assert classify("carry on", ControlContext(speaking=False, task_running=False,
                                               task_paused=True)).intent == ControlIntent.RESUME_TASK


def test_an_ordinary_request_is_not_a_control_command():
    for phrase in ["open notepad", "what time is it", "stop by the shop on the way",
                   "research the EU AI Act"]:
        assert not classify(phrase, _working()).is_control


# --------------------------------------------------------------------------- speech really stops

def test_a_spoken_stop_reaches_the_tts_backend(tts):
    """End to end through the deterministic chain: no model, no fast path, just the control router."""
    engine, voice = tts
    engine.speak("here is a long explanation you did not want")
    assert voice.wait_utterances(1)
    assert engine.is_speaking is True

    command = classify("stop", _speaking())
    assert command.intent == ControlIntent.STOP_SPEAKING
    engine.stop()                                   # what Assistant._apply_control calls

    assert voice.wait_purged(), "the current utterance must be cut, not left to finish"
    assert engine._idle.wait(2.0)
    assert engine.is_speaking is False


def test_is_speaking_converges_immediately_on_request(tts):
    """The owner asked for silence; the answer to "are you speaking?" must not lag the request."""
    engine, voice = tts
    engine.speak("something")
    assert voice.wait_utterances(1)
    engine.stop()
    assert engine.is_speaking is False, "convergence must not wait for the worker thread"


def test_queued_speech_is_discarded_rather_than_spoken_after_a_stop(tts):
    """Anything already queued must not survive the interruption."""
    engine, voice = tts
    engine.speak("first")
    assert voice.wait_utterances(1)
    engine.speak("second")
    engine.speak("third")
    engine.stop()
    assert engine._idle.wait(2.0)
    spoken = voice.utterances
    assert "third" not in spoken, f"speech continued after a stop: {spoken}"


def test_a_completion_arriving_after_an_interruption_does_not_resume_speech(tts):
    """The stale-callback case: the backend finishes the old utterance after it was cut."""
    engine, voice = tts
    engine.speak("the utterance being interrupted")
    assert voice.wait_utterances(1)
    engine.stop()
    assert engine._idle.wait(2.0)
    before = list(voice.utterances)

    voice.finish_utterance()                        # the old completion lands late
    time.sleep(0.1)
    assert engine.is_speaking is False, "a late completion must not put V.O.I.D back into speaking"
    assert voice.utterances == before, "a late completion must not start new audio"


def test_repeated_stops_are_idempotent(tts):
    engine, voice = tts
    engine.speak("something")
    assert voice.wait_utterances(1)
    for _ in range(5):
        engine.stop()
    assert engine._idle.wait(2.0)
    assert engine.is_speaking is False


def test_stop_before_anything_was_ever_spoken_is_safe():
    engine = SapiTTS(_voice_factory=lambda: _FakeVoice(), _com_setup=lambda: None,
                     _com_teardown=lambda: None, _poll_ms=10)
    try:
        engine.stop()                               # worker never started
        assert engine.is_speaking is False
    finally:
        engine.close()


def test_nothing_is_spoken_after_close(tts):
    engine, voice = tts
    engine.close()
    engine.speak("this must never be heard")
    time.sleep(0.1)
    assert "this must never be heard" not in voice.utterances


def test_concurrent_speak_and_stop_never_leaves_speech_running(tts):
    """Hammered from several threads: whatever the interleaving, a final stop must win."""
    engine, voice = tts
    stop_requested = threading.Event()

    def speaker():
        for index in range(40):
            engine.speak(f"utterance {index}")
            if stop_requested.is_set():
                return

    def stopper():
        for _ in range(40):
            engine.stop()
        stop_requested.set()

    threads = [threading.Thread(target=speaker), threading.Thread(target=stopper)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.0)
    engine.stop()                                   # the owner's last word
    assert engine._idle.wait(3.0), "the engine never converged to idle"
    assert engine.is_speaking is False


# --------------------------------------------------------------------------- task isolation

def _assistant():
    from void.app import Assistant
    assistant = Assistant()
    for task in assistant.store.list(limit=50):
        if task.status != "completed":
            task.status = "completed"
            assistant.store.save(task)
    return assistant


def _command(intent):
    return type("Command", (), {"intent": intent, "modification": None})()


def test_stopping_speech_never_touches_a_running_task():
    from void.core.task import Status, Task
    assistant = _assistant()
    assistant.store.save(Task(goal="rename the invoices folder", id="iso-1",
                              status=Status.RUNNING))
    calls = []
    assistant.stop_speaking = lambda: calls.append(1)

    reply = assistant._apply_control(_command(ControlIntent.STOP_SPEAKING))

    assert calls == [1], "the TTS backend must actually be asked to stop"
    assert reply == "", "silence is the response"
    still = {task.id: task.status for task in assistant.store.list(limit=10)}
    assert still.get("iso-1") == Status.RUNNING, "interruption abandoned unrelated work"


def test_cancelling_asks_which_job_when_more_than_one_could_be_meant():
    """Cancelling the wrong job is not recoverable, and "cancel that" does not identify one."""
    from void.core.task import Status, Task
    assistant = _assistant()
    assistant.store.save(Task(goal="rename the invoices folder", id="amb-a",
                              status=Status.RUNNING))
    assistant.store.save(Task(goal="research the EU AI Act timeline", id="amb-b",
                              status=Status.RUNNING))

    reply = assistant._apply_control(_command(ControlIntent.CANCEL_TASK))

    assert "Which one" in reply
    assert "invoices" in reply or "EU AI Act" in reply
    statuses = {task.id: task.status for task in assistant.store.list(limit=10)}
    assert statuses.get("amb-a") == Status.RUNNING
    assert statuses.get("amb-b") == Status.RUNNING


def test_cancelling_acts_when_there_is_exactly_one_job():
    from void.core.task import Status, Task
    assistant = _assistant()
    assistant.store.save(Task(goal="rename the invoices folder", id="one-1",
                              status=Status.RUNNING))
    assert assistant._apply_control(_command(ControlIntent.CANCEL_TASK)) == "Cancelled."
    statuses = {task.id: task.status for task in assistant.store.list(limit=10)}
    assert statuses.get("one-1") == Status.CANCELLED


def test_pausing_is_recoverable_so_it_still_just_acts():
    from void.core.task import Status, Task
    assistant = _assistant()
    assistant.store.save(Task(goal="a long job", id="pause-1", status=Status.RUNNING))
    assert assistant._apply_control(_command(ControlIntent.PAUSE_TASK)) == "Paused."
    assert assistant._apply_control(_command(ControlIntent.RESUME_TASK)) == "Resuming."


def test_a_control_command_never_executes_a_capability():
    """It moves task status and stops speech. It cannot run a tool or reverse a side effect."""
    import inspect
    from void.app import Assistant
    source = inspect.getsource(Assistant._apply_control)
    for forbidden in ("tools.execute", "_run_call", "subprocess", "os.system", "authorize"):
        assert forbidden not in source


def test_cancelling_with_nothing_running_says_so():
    assistant = _assistant()
    assert "Nothing" in assistant._apply_control(_command(ControlIntent.CANCEL_TASK))
