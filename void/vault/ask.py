"""Recognising a question the owner has explicitly aimed at their own notes.

Why this is a separate, deliberately narrow thing. The prompt packs' retrieval flow ends with a rule
they call the difference between an assistant and a toy: when the notes do not hold the answer, say
so - do not answer from a note that merely shares a topic, and do not quietly answer from general
knowledge as though the notes had been consulted. :class:`~void.vault.Verdict` makes that outcome
available, but a *tool* result can only reach the owner if a model chose to call the tool, and the
case that matters most is the one where it does not.

So V.O.I.D answers one case itself, before any model: a question whose words **explicitly scope it to
the owner's notes** ("what do my notes say about the retainer", "check my notes for the kickoff
date") that the vault then finds nothing for. That answer is engine-authored and final.

The scoping has to be explicit, and the matcher is a closed set of phrasings for exactly the reason
``void/orchestration/commands.py`` matches whole utterances: "what is the retainer" is an ordinary
question which the notes may or may not be the best source for, and claiming it as a notes question
would turn every unanswerable general question into "that is not in the notes". Only the owner saying
*my notes* makes the scope theirs to assert.

What this module deliberately does NOT claim: the FOUND case. A question the notes can answer goes
through the ordinary agent and the ``search_notes`` tool, whose result already carries the verified
header the packs ask for ("these notes were checked and do contain the answer"). Pre-empting the
model there would buy nothing and would mean a second answering path to keep correct.
"""
from __future__ import annotations

import re

#: Phrasings that scope a question to the owner's own notes. Each must contain a possessive, because
#: "the notes" can mean the notes on a document, in a margin, or in a meeting - "my notes" cannot.
_SCOPED = (
    # "what do my notes say about X", "what do my notes have on X"
    re.compile(r"\A(?:what\s+)?do(?:es)?\s+my\s+(?:notes?|vault)\s+"
               r"(?:say|have|mention|cover|know|contain|tell\s+me)\s*"
               r"(?:about|on|regarding|re|for|anything\s+about)?\s*(?P<q>.*)\Z", re.IGNORECASE),
    # "check / search / look in my notes for X"
    re.compile(r"\A(?:please\s+)?(?:check|search|look\s+(?:in|through)|grep|dig\s+through|read)\s+"
               r"(?:in\s+)?my\s+(?:notes?|vault)\s+(?:for|about|on|regarding)?\s*(?P<q>.*)\Z",
               re.IGNORECASE),
    # "according to my notes, X" / "from my notes, X" / "in my notes, X"
    re.compile(r"\A(?:according\s+to|from|in|per)\s+my\s+(?:notes?|vault)[,:]?\s*(?P<q>.*)\Z",
               re.IGNORECASE),
    # "ask my notes about X" / "ask my vault X"
    re.compile(r"\A(?:ask|query)\s+my\s+(?:notes?|vault)\s*(?:about|for|on)?\s*(?P<q>.*)\Z",
               re.IGNORECASE),
    # "what do I have in my notes about X"
    re.compile(r"\A(?:what|anything)\s+(?:do\s+i\s+have|have\s+i\s+(?:got|written|said))\s+"
               r"(?:in\s+my\s+(?:notes?|vault)\s+)?(?:about|on)\s*(?P<q>.*?)"
               r"(?:\s+in\s+my\s+(?:notes?|vault))?\Z", re.IGNORECASE),
)

#: Leading courtesies and the wake word, removed before matching so "Void, what do my notes say
#: about X" scopes the same way. The same list ``void/orchestration/commands.py`` strips, and for the
#: same reason: the owner's phrasing should not have to be clean.
_LEAD = re.compile(r"\A(?:void|hey\s+void|ok(?:ay)?|alright|right|so|now|um|uh|please)[,\s]+",
                   re.IGNORECASE)

#: A question that scopes itself to the notes and asks nothing is not a question. "What do my notes
#: say" with no subject is the owner thinking out loud, and the vault has nothing to look up.
MIN_QUESTION_WORDS = 1


def scoped_question(utterance: object) -> str | None:
    """The question inside an explicitly notes-scoped utterance, or None.

    Returns the question with the scoping phrase removed, so that retrieval scores against what the
    owner actually asked about rather than against the words "my notes", which every note in a vault
    matches equally badly.
    """
    text = " ".join(str(utterance or "").split()).strip()
    while True:
        stripped = _LEAD.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    if not text:
        return None
    bare = text.rstrip(" ?.!,")
    for pattern in _SCOPED:
        match = pattern.match(bare)
        if not match:
            continue
        question = " ".join(match.group("q").split()).strip(" ?.!,")
        if len(question.split()) < MIN_QUESTION_WORDS:
            return None
        return question
    return None
