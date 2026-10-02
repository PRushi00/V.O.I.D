"""Which controls have consequences outside this machine - the deterministic confirmation boundary.

The blueprint's rule: V.O.I.D may prepare autonomously, but pressing *Send* is where it stops and asks, and
that decision must be "deterministic and risk-based rather than decided by the LLM alone".

This module is where that is decided, and it lives in ``void/security`` rather than in a browser or desktop
adapter for one reason: both of them need it, and a security vocabulary that exists in two copies will
eventually exist in two *different* copies. A button labelled "Send" means the same thing whether it is in
Gmail or in WhatsApp.

Two properties make it trustworthy:

**The name comes from the thing itself.** Callers read the control's own accessible name from the live page
or the live control tree and pass that. A tool that accepted the name as a model-supplied argument would let
a model relabel a *Send* button as *Next* and authorize itself, which would make the boundary decorative.

**It errs towards asking.** A false positive costs the owner one confirmation. A false negative sends a
message, spends money or deletes something. The vocabulary is therefore generous, and every caller fails
safe to HIGH when it cannot read a name at all.
"""
from __future__ import annotations

import re

#: Accessible names that mean "acting on this has consequences outside this machine".
#:
#: Matched as whole words, case-insensitively, so "Resend code" is caught and "Sender" is not. Stems are
#: not derived automatically - "resend" and "repost" are listed explicitly, because deriving them would
#: also match "sender" and "poster".
CONSEQUENTIAL_WORDS = frozenset({
    # sending and publishing
    "send", "submit", "post", "publish", "share", "tweet", "reply", "forward", "resend",
    "repost", "retweet", "reshare", "broadcast", "email", "message",
    # money
    "pay", "buy", "purchase", "checkout", "order", "subscribe", "donate", "transfer",
    "withdraw", "deposit", "refund", "tip",
    # destruction and account state
    "delete", "remove", "erase", "wipe", "deactivate", "unsubscribe", "cancel account",
    "close account", "delete account", "reset",
    # commitments and agreements
    "confirm", "accept", "agree", "sign", "authorize", "authorise", "approve", "consent",
    "book", "reserve", "apply", "enroll", "enrol", "register", "finalize", "finalise",
    "commit", "invite",
    # moving data off the machine
    "upload", "export", "sync", "publish now",
})

#: Words that look consequential but are not, and must override a match. Checked first.
#:
#: "Cancel" is the clearest case: in almost every dialog it is the SAFE choice - the one that stops
#: something happening - and treating it as consequential would make V.O.I.D ask permission to back out,
#: which is both annoying and backwards. "Cancel account" stays consequential because it is in the
#: vocabulary above as a phrase.
SAFE_WORDS = frozenset({
    "cancel", "close", "dismiss", "back", "undo", "discard", "reject", "decline", "deny",
    "no", "stop", "pause", "search", "find", "filter", "sort", "refresh", "reload",
    "next", "previous", "more", "less", "expand", "collapse", "settings", "help",
    "copy", "paste", "cut", "select", "zoom", "print preview",
})

_WORD = re.compile(r"[a-z]+")


def is_consequential(name: object) -> bool:
    """Whether acting on a control with this accessible name needs the owner's confirmation.

    Order matters:

    1. A multi-word phrase from the vocabulary ("delete account", "cancel account") wins outright, because
       those phrases exist precisely to override a safe single word inside them.
    2. A safe word that is the *whole* label wins next: a button labelled exactly "Cancel" is the safe
       choice in a dialog, and asking about it would be backwards.
    3. Otherwise any consequential word in the label triggers.
    """
    if not isinstance(name, (str, bytes)):
        return False
    try:
        text = " ".join(str(name).lower().split())
    except Exception:                                          # noqa: BLE001
        return False
    if not text:
        return False
    # 1. Phrases first, so "delete account" is not softened by "account" or by a safe word.
    for phrase in CONSEQUENTIAL_WORDS | SAFE_WORDS:
        if " " in phrase and phrase in text:
            return phrase in CONSEQUENTIAL_WORDS
    words = set(_WORD.findall(text))
    if not words:
        return False
    # 2. A label that is entirely safe words ("Cancel", "Go back") is safe.
    if words and words <= SAFE_WORDS:
        return False
    # 3. Any consequential word.
    return bool(words & CONSEQUENTIAL_WORDS)


def explain(name: object) -> str:
    """Which word made a name consequential, for an audit line. Empty when it is not."""
    if not is_consequential(name):
        return ""
    text = " ".join(str(name).lower().split())
    for phrase in CONSEQUENTIAL_WORDS:
        if " " in phrase and phrase in text:
            return phrase
    hit = sorted(set(_WORD.findall(text)) & CONSEQUENTIAL_WORDS)
    return hit[0] if hit else "consequential"
