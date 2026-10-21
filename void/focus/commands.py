"""What the owner says to a focus session, classified before any model sees it.

Same discipline as ``void/orchestration/commands.py`` and for the same reason: whether "pause" means
pause the session or pause something else is not a semantic judgement a model should make on the
owner's behalf, and getting it wrong is expensive in both directions - a session abandoned when
someone wanted quiet, or a callout that keeps firing because "it's fine, I'm reading the docs" went
to a model and came back as a conversation.

Every pattern here matches a whole utterance, so "how long is the pause in the audio" is a question
and "stop the session" is a command. The only one that takes a free argument is the excuse, and it
takes it as the owner's own words with nothing read out of them.
"""
from __future__ import annotations

import re

#: Spoken numbers worth understanding. A session length is a small round number said out loud; past
#: ninety, people say the digits.
WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
    "twenty five": 25, "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50, "sixty": 60,
    "ninety": 90, "an hour": 60, "a hour": 60, "half an hour": 30, "two hours": 120,
}


class FocusIntent:
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    EXTEND = "extend"
    SNOOZE = "snooze"
    EXCUSE = "excuse"
    RELOCK = "relock"
    NAG = "nag"
    STATUS = "status"
    INTENT = "intent"
    NONE = "none"

    ALL = frozenset({START, STOP, PAUSE, RESUME, EXTEND, SNOOZE, EXCUSE, RELOCK, NAG, STATUS,
                     INTENT, NONE})


class Request:
    """A classified focus utterance. ``minutes`` / ``seconds`` are set only where they were said."""

    __slots__ = ("intent", "minutes", "seconds", "text")

    def __init__(self, intent: str, *, minutes: int = 0, seconds: float = 0.0, text: str = ""):
        self.intent, self.minutes, self.seconds, self.text = intent, minutes, seconds, text

    def __repr__(self) -> str:                        # pragma: no cover - diagnostics only
        return (f"Request({self.intent!r}, minutes={self.minutes}, seconds={self.seconds}, "
                f"text={self.text!r})")

    def __eq__(self, other) -> bool:
        return (isinstance(other, Request) and other.intent == self.intent
                and other.minutes == self.minutes and other.seconds == self.seconds)


_LEAD = re.compile(r"\A(?:void|hey\s+void|ok(?:ay)?|alright|right|so|now|um|uh|please|and)[,\s]+",
                   re.IGNORECASE)

_NUM = r"(?P<n>\d{1,3}|" + "|".join(sorted((re.escape(w) for w in WORD_NUMBERS), key=len,
                                           reverse=True)) + r")"

#: "thirty minutes on this", "focus for 25 minutes", "let's do an hour on this"
_START = (
    re.compile(rf"\A{_NUM}\s*(?:minutes?|mins?)\s+(?:on|of)\s+(?:this|that|it|work)\s*\Z",
               re.IGNORECASE),
    re.compile(rf"\A(?:focus|lock\s+in|deep\s+work|work)\s+(?:for\s+)?{_NUM}\s*"
               rf"(?:minutes?|mins?)?\s*(?:on\s+(?:this|that|it))?\s*\Z", re.IGNORECASE),
    re.compile(rf"\A(?:let'?s\s+do|give\s+me|start)\s+(?:a\s+)?{_NUM}\s*"
               rf"(?:minute|min)?\s*(?:session|sprint|focus)?\s*(?:on\s+(?:this|that|it))?\s*\Z",
               re.IGNORECASE),
    re.compile(rf"\A(?:start|begin)\s+(?:a\s+)?(?:focus\s+)?session\s+(?:for\s+)?{_NUM}\s*"
               rf"(?:minutes?|mins?)?\s*\Z", re.IGNORECASE),
)

_STOP = re.compile(r"\A(?:end|stop|finish|abort|cancel|kill|abandon)\s+(?:the\s+|my\s+|this\s+)?"
                   r"(?:focus\s+)?(?:session|timer|focus|sprint)\s*\Z", re.IGNORECASE)
_PAUSE = re.compile(r"\A(?:pause|hold|freeze)\s+(?:the\s+|my\s+|this\s+)?"
                    r"(?:focus\s+)?(?:session|timer|focus|clock)\s*\Z", re.IGNORECASE)
_RESUME = re.compile(r"\A(?:resume|unpause|continue|restart)\s+(?:the\s+|my\s+|this\s+)?"
                     r"(?:focus\s+)?(?:session|timer|focus|clock)\s*\Z", re.IGNORECASE)
_EXTEND = re.compile(rf"\A(?:extend|add|give\s+me)\s+(?:the\s+session\s+)?(?:another\s+|a\s+further\s+)?"
                     rf"{_NUM}\s*(?:more\s+)?(?:minutes?|mins?)\s*(?:more)?\s*\Z", re.IGNORECASE)
_SNOOZE = re.compile(rf"\A(?:give\s+me|just\s+give\s+me|hold\s+on\s+for|quiet\s+for|"
                     rf"leave\s+me\s+alone\s+for)\s+{_NUM}\s*(?:seconds?|secs?)\s*\Z",
                     re.IGNORECASE)
_RELOCK = re.compile(r"\A(?:lock\s+(?:on\s+)?(?:to\s+)?(?:this|here)(?:\s+tab|\s+window|\s+app)?|"
                     r"keep\s+me\s+(?:in|on)\s+(?:this|here)(?:\s+tab|\s+window|\s+app)?|"
                     r"this\s+is\s+the\s+(?:tab|window|one)|stay\s+on\s+this(?:\s+tab|\s+window)?|"
                     r"lock\s+(?:this\s+)?(?:tab|window|app))\s*\Z", re.IGNORECASE)
_NAG = re.compile(rf"\A(?:call\s+me\s+out|nag\s+me|remind\s+me|tell\s+me\s+off)\s+"
                  rf"(?:every\s+){_NUM}\s*(?:seconds?|secs?)\s*\Z", re.IGNORECASE)
_STATUS = re.compile(r"\A(?:how\s+(?:long|much\s+(?:time|longer))\s+(?:is\s+)?(?:left|to\s+go|"
                     r"remaining)|how\s+(?:is|am\s+i)\s+(?:the\s+session|i|we)\s+doing|"
                     r"(?:what'?s|what\s+is)\s+(?:the\s+)?(?:session|focus)\s+status|"
                     r"time\s+left)\s*\??\s*\Z", re.IGNORECASE)

#: The excuse. Two shapes: a bare "it's fine" acknowledgement, and one that gives a reason. The
#: reason is kept only so the engine can be told an excuse was given; nothing reads it.
_EXCUSE = re.compile(r"\A(?:it'?s\s+(?:ok(?:ay)?|fine|alright|all\s+right)|no\s+void|"
                     r"that'?s\s+(?:ok(?:ay)?|fine)|leave\s+it|allow\s+(?:it|this)|"
                     r"i\s+need\s+(?:to|this)|this\s+is\s+(?:work|for\s+work)|i'?m\s+"
                     r"(?:doing|looking\s+up|reading|researching))\b(?P<why>.*)\Z", re.IGNORECASE)

#: "I'm working on the thumbnail sprint" - the answer to "what are we focusing on?"
_INTENT = re.compile(r"\A(?:i'?m\s+)?(?:working\s+on|focusing\s+on|doing|on)\s+(?P<what>.+)\Z",
                     re.IGNORECASE)


def _minutes(raw: str) -> int:
    text = str(raw or "").strip().lower()
    if text.isdigit():
        return int(text)
    return int(WORD_NUMBERS.get(text, 0))


def parse(utterance: object, *, expecting_intent: bool = False) -> Request:
    """Classify one utterance. ``expecting_intent`` is set only in the answer window.

    The answer window matters: "the thumbnail sprint" is an intent when the session has just asked
    what the owner is working on, and is nothing at all at any other moment. Nothing else in this
    module depends on conversational state, deliberately.
    """
    text = " ".join(str(utterance or "").split())
    while True:
        stripped = _LEAD.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    bare = text.strip(" .!,").rstrip("?")
    if not bare:
        return Request(FocusIntent.NONE)

    for pattern in _START:
        match = pattern.match(bare)
        if match:
            minutes = _minutes(match.group("n"))
            if minutes:
                return Request(FocusIntent.START, minutes=minutes)
    if _STOP.match(bare):
        return Request(FocusIntent.STOP)
    if _PAUSE.match(bare):
        return Request(FocusIntent.PAUSE)
    if _RESUME.match(bare):
        return Request(FocusIntent.RESUME)
    match = _EXTEND.match(bare)
    if match and _minutes(match.group("n")):
        return Request(FocusIntent.EXTEND, minutes=_minutes(match.group("n")))
    match = _SNOOZE.match(bare)
    if match and _minutes(match.group("n")):
        return Request(FocusIntent.SNOOZE, seconds=float(_minutes(match.group("n"))))
    match = _NAG.match(bare)
    if match and _minutes(match.group("n")):
        return Request(FocusIntent.NAG, seconds=float(_minutes(match.group("n"))))
    if _RELOCK.match(bare):
        return Request(FocusIntent.RELOCK)
    if _STATUS.match(bare):
        return Request(FocusIntent.STATUS)
    match = _EXCUSE.match(bare)
    if match:
        return Request(FocusIntent.EXCUSE, text=" ".join(match.group("why").split()))
    if expecting_intent:
        match = _INTENT.match(bare)
        if match:
            return Request(FocusIntent.INTENT, text=" ".join(match.group("what").split()))
        return Request(FocusIntent.INTENT, text=bare)
    return Request(FocusIntent.NONE)
