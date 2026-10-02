"""Deterministic control-command semantics: what "stop" actually means.

This closes a real gap. Today a bare "stop" spoken while V.O.I.D is talking does not match the kill
switch's full phrase ("VOID, STOP EVERYTHING"), so it falls through to the ordinary command path and the
*model* decides what it meant. That is the wrong layer for that decision: whether the owner wants silence
or wants the work abandoned is not a semantic judgement, it is a control signal, and getting it wrong is
expensive in both directions - cancelling an hour of work because someone wanted quiet, or carrying on
talking over someone who asked for quiet.

So the five control intents the blueprint names are classified **here**, deterministically, before any model
sees the words:

    STOP_SPEAKING   be quiet now; the task is untouched
    PAUSE_TASK      stop working, keep everything, resumable
    RESUME_TASK     carry on from where you were
    CANCEL_TASK     abandon the work  (and only this one is destructive)
    MODIFY_TASK     "instead of that, ..." - handled by void.orchestration.replan

Three properties make this safe:

**Context decides, not just words.** The same word means different things depending on what V.O.I.D is
doing. "Stop" while speaking is STOP_SPEAKING. "Stop" while a task runs and nothing is being spoken is
PAUSE_TASK - the conservative reading, because pausing is recoverable and cancelling is not. Promoting it to
CANCEL_TASK requires the owner to be explicit ("cancel that", "forget the whole thing").

**Whole-utterance matching.** "Stop the music" and "don't stop" are commands and questions, not control
signals. A control phrase is recognised only when the utterance *is* that phrase, which is the same rule
``void.voice.runtime.is_standby_phrase`` already uses for returning to standby.

**Cancelling never happens by accident.** CANCEL_TASK needs an unambiguous cancel phrase. The bare word
"stop" can never produce it, from any state. And none of this touches the kill switch, which keeps its own
deliberate full phrase - this layer is about the task, not about halting V.O.I.D itself.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Characters stripped before matching: whatever punctuation the transcriber chose to add.
_STRIP = ".,!?;:'\"-- \t\n"

#: Leading courtesies people put in front of a command. Removed before matching so "okay, stop" works.
_LEAD_WORDS = ("void", "hey void", "ok", "okay", "alright", "right", "please", "yeah", "yes",
               "um", "uh", "so", "now", "just")


class ControlIntent:
    """What a control utterance asks for. Not a task status - a request about one."""

    STOP_SPEAKING = "stop_speaking"
    PAUSE_TASK = "pause_task"
    RESUME_TASK = "resume_task"
    CANCEL_TASK = "cancel_task"
    MODIFY_TASK = "modify_task"
    #: Not a control command at all - an ordinary request, to be handled normally.
    NONE = "none"

    ALL = frozenset({STOP_SPEAKING, PAUSE_TASK, RESUME_TASK, CANCEL_TASK, MODIFY_TASK, NONE})
    #: Intents that change task state, as opposed to only affecting speech.
    TOUCHES_TASK = frozenset({PAUSE_TASK, RESUME_TASK, CANCEL_TASK, MODIFY_TASK})


#: Words that, alone, mean "be quiet / stop what you are doing" without saying which.
#: AMBIGUOUS BY DESIGN: context decides. See :func:`classify`.
_BARE_STOP = frozenset({
    "stop", "stop it", "stop that", "stop please", "quiet", "be quiet", "shush", "hush",
    "shut up", "silence", "enough", "that's enough", "thats enough", "ok stop", "okay stop",
})

#: Explicitly about speech. These are never read as a task command, whatever the state.
_STOP_SPEAKING = frozenset({
    "stop talking", "stop speaking", "stop reading", "stop saying that", "stop the audio",
    "be quiet now", "don't read it", "dont read it", "no need to read it", "skip the explanation",
    "you can stop talking", "stop narrating", "mute", "mute yourself",
})

#: Explicitly about pausing the work. Recoverable.
_PAUSE_TASK = frozenset({
    "pause", "pause the task", "pause that", "pause it", "hold on", "hold up", "wait",
    "wait a moment", "wait a second", "one moment", "one second", "give me a moment",
    "pause what you're doing", "pause what youre doing", "freeze", "halt that",
})

_RESUME_TASK = frozenset({
    "resume", "resume the task", "continue", "carry on", "keep going", "go on", "go ahead",
    "carry on then", "continue please", "unpause", "resume please", "pick up where you left off",
    "back to it", "continue where you left off",
})

#: Explicitly destructive. ONLY these can cancel; the bare word "stop" never reaches here.
_CANCEL_TASK = frozenset({
    "cancel", "cancel it", "cancel that", "cancel the task", "cancel everything",
    "abort", "abort it", "abort that", "abandon it", "abandon the task", "drop it",
    "forget it", "forget the whole thing", "forget that task", "never mind the task",
    "stop the task", "stop the whole task", "stop everything", "give up on it",
    "scrap it", "scrap that", "discard it", "throw it away", "don't bother", "dont bother",
})

#: Openings that mean "change what you are doing", handled by the replanner.
_MODIFY_PREFIXES = (
    "instead", "instead of that", "actually", "actually no", "change that to", "change it to",
    "make it", "rather than that", "on second thought", "on second thoughts", "scratch that and",
    "no wait", "wait no", "also add", "add this", "add a", "also include", "and also",
    "don't do that, do", "dont do that, do",
)


@dataclass(frozen=True)
class ControlContext:
    """What V.O.I.D is doing right now, which is what makes a bare "stop" decidable.

    Supplied by the caller from real state (the TTS backend's own ``is_speaking``, the task's status) -
    never inferred from the words themselves.
    """

    speaking: bool = False
    task_running: bool = False
    task_paused: bool = False


@dataclass(frozen=True)
class ControlCommand:
    """The classification of one utterance."""

    intent: str
    #: The phrase that matched, for the audit line. Bounded by the caller's transcript length.
    matched: str = ""
    #: For MODIFY_TASK, the remainder of the utterance - the actual change being asked for.
    modification: str = ""
    #: Why this intent rather than another, in one line. Shown in logs, useful when a classification
    #: surprises someone.
    why: str = ""

    @property
    def is_control(self) -> bool:
        return self.intent != ControlIntent.NONE

    @property
    def touches_task(self) -> bool:
        return self.intent in ControlIntent.TOUCHES_TASK


def _normalise(text: object) -> str:
    """Lower-cased, punctuation-stripped, whitespace-collapsed, leading courtesies removed."""
    if not isinstance(text, str):
        return ""
    cleaned = " ".join(str(text).lower().split()).strip(_STRIP)
    cleaned = " ".join(word.strip(_STRIP) for word in cleaned.split()).strip()
    # Peel leading courtesies one at a time so "ok void please stop" reduces to "stop".
    changed = True
    while changed and cleaned:
        changed = False
        for lead in _LEAD_WORDS:
            if cleaned == lead:
                return ""
            if cleaned.startswith(lead + " "):
                cleaned = cleaned[len(lead) + 1:].strip()
                changed = True
                break
    return cleaned


def _modification_after(text: str) -> str | None:
    """The change being requested, if the utterance opens like a modification.

    Requires something *after* the opener: "actually" on its own is a hesitation, not an instruction.
    """
    for prefix in sorted(_MODIFY_PREFIXES, key=len, reverse=True):
        if text == prefix:
            return None
        for separator in (" ", ", "):
            opener = prefix + separator
            if text.startswith(opener):
                rest = text[len(opener):].strip(_STRIP).strip()
                return rest or None
    return None


def classify(transcript: object, context: ControlContext | None = None) -> ControlCommand:
    """What control intent, if any, this utterance expresses.

    Deterministic and side-effect free: it reads words and state and returns a classification. Acting on
    it is the caller's job (see :class:`ControlRouter`).

    The ordering below is the policy, and it is deliberate:

    1. **Explicit phrases win**, in increasing order of consequence, so an explicit "stop talking" can
       never be read as a cancel and an explicit "cancel" is never softened to a pause.
    2. **A bare "stop" is resolved by context**: speaking -> silence; otherwise a running task -> pause.
       Never cancel. The asymmetry is intentional - a wrongly-paused task costs a word to resume, a
       wrongly-cancelled one may cost everything.
    3. **A modification opener** is only a modification when something follows it.
    """
    context = context or ControlContext()
    text = _normalise(transcript)
    if not text:
        return ControlCommand(ControlIntent.NONE, why="nothing was said")

    if text in _STOP_SPEAKING:
        return ControlCommand(ControlIntent.STOP_SPEAKING, matched=text,
                              why="an explicit request about speech")
    if text in _RESUME_TASK:
        return ControlCommand(ControlIntent.RESUME_TASK, matched=text, why="an explicit resume")
    if text in _PAUSE_TASK:
        return ControlCommand(ControlIntent.PAUSE_TASK, matched=text, why="an explicit pause")
    if text in _CANCEL_TASK:
        return ControlCommand(ControlIntent.CANCEL_TASK, matched=text,
                              why="an explicit, unambiguous cancel")

    modification = _modification_after(text)
    if modification is not None:
        return ControlCommand(ControlIntent.MODIFY_TASK, matched=text, modification=modification,
                              why="the owner asked for something different")

    if text in _BARE_STOP:
        # The ambiguous case, and the reason this module exists.
        if context.speaking:
            return ControlCommand(ControlIntent.STOP_SPEAKING, matched=text,
                                  why="said while speaking, so it is about the speech")
        if context.task_running:
            return ControlCommand(ControlIntent.PAUSE_TASK, matched=text,
                                  why="a task is running and 'stop' alone is never a cancel")
        return ControlCommand(ControlIntent.STOP_SPEAKING, matched=text,
                              why="nothing is running, so there is only speech to stop")

    return ControlCommand(ControlIntent.NONE, why="an ordinary request")


class ControlRouter:
    """Turns a classified control command into the right effect, and nothing more.

    Each effect is injected, so this holds no capability of its own and can be tested without a voice
    stack. The point of the indirection is that the *decision* (which effect) is testable separately from
    the *mechanism* (how speech stops), and neither is decided by a model.

    What this deliberately cannot do: engage the kill switch, authorize a tool, or change a risk level.
    Pausing and cancelling act on task state through the callbacks the owner of the task supplied.
    """

    def __init__(self, *, stop_speech=None, pause_task=None, resume_task=None,
                 cancel_task=None, modify_task=None, on_audit=None):
        self._stop_speech = stop_speech
        self._pause_task = pause_task
        self._resume_task = resume_task
        self._cancel_task = cancel_task
        self._modify_task = modify_task
        self._audit = on_audit or (lambda line: None)

    def handle(self, transcript: object, context: ControlContext | None = None) -> ControlCommand:
        """Classify and act. Returns the classification, so a caller can see what was decided.

        A command with intent NONE does nothing at all here and is left to the ordinary path - this
        router never consumes an ordinary request.
        """
        command = classify(transcript, context)
        if not command.is_control:
            return command
        self._audit(f"CONTROL {command.intent} ({command.why})")
        effects = {
            ControlIntent.STOP_SPEAKING: self._stop_speech,
            ControlIntent.PAUSE_TASK: self._pause_task,
            ControlIntent.RESUME_TASK: self._resume_task,
            ControlIntent.CANCEL_TASK: self._cancel_task,
        }
        effect = effects.get(command.intent)
        if command.intent == ControlIntent.MODIFY_TASK:
            if self._modify_task is not None:
                self._safely(lambda: self._modify_task(command.modification), command.intent)
            return command
        if effect is not None:
            self._safely(effect, command.intent)
        return command

    def _safely(self, effect, intent: str) -> None:
        """Run an effect without letting its failure propagate into the voice pipeline.

        A failing "stop speaking" must not crash the capture loop that is trying to listen to the owner;
        the failure is recorded and the pipeline survives.
        """
        try:
            effect()
        except Exception as exc:                                # noqa: BLE001
            self._audit(f"CONTROL_EFFECT_FAILED {intent} ({type(exc).__name__})")
