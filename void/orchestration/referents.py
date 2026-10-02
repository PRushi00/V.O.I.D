"""The providers that make reference resolution concrete.

:mod:`void.orchestration.reference` deliberately knows nothing about tabs, windows or documents. This is
where that knowledge lives: small adapters that turn each capability layer's view of the world into
candidates. Adding a referenceable kind of thing means adding a provider here - the resolver is untouched.

Each provider degrades to an empty list rather than raising, so a layer that is disabled or broken costs
V.O.I.D some candidates and nothing else.

There is also :class:`RecentThings`, a short in-memory record of what V.O.I.D has recently produced or the
owner has recently referred to. It is what lets "the document I was looking at" and "open this chart" work
when the thing is not currently on screen. It is **session memory about references only** - it stores
labels and targets, never content, never permissions, and nothing read from it can authorize anything. It
is memory as data, exactly as the security model requires.
"""
from __future__ import annotations

import time
from collections import deque

from void.orchestration.reference import Candidate

#: How many recent things to remember. Small on purpose: reference resolution is about the last few things,
#: and a long history makes "that one" less decidable, not more.
RECENT_LIMIT = 24


class RecentThings:
    """What V.O.I.D recently made or the owner recently referred to.

    Thread-safe enough for V.O.I.D's use (a ``deque`` with a maxlen has atomic append and iteration under
    CPython), and bounded so it cannot grow.
    """

    def __init__(self, limit: int = RECENT_LIMIT):
        self._items: deque = deque(maxlen=max(1, int(limit)))

    def note(self, kind: str, label: str, target: str, source: str = "recent",
             produced: bool = False, detail: str = "") -> None:
        """Record that something was produced or referred to.

        Re-noting a target moves it to the front rather than duplicating it, so "the last chart" means the
        most recent mention of that chart, not its first.
        """
        if not target:
            return
        self._items = deque(
            (item for item in self._items if item.get("target") != target),
            maxlen=self._items.maxlen)
        self._items.append({"kind": kind, "label": label, "target": target, "source": source,
                            "produced": bool(produced), "detail": detail, "at": time.time()})

    def candidates(self) -> list[Candidate]:
        # Newest first: recency is a scoring input, and ordering the list the same way keeps ties stable.
        return [Candidate(kind=item["kind"], label=item["label"], target=item["target"],
                          source=item["source"], at=item["at"], produced=item["produced"],
                          detail=item["detail"])
                for item in reversed(self._items)]

    def __len__(self) -> int:
        return len(self._items)


def tab_candidates(browser):
    """Open browser tabs, as referenceable things.

    This is what makes "go back to that page" and "open the tab I had" resolvable, and it is the same tab
    list the route resolver uses for reuse - one view of the browser, not two.
    """
    def provide():
        adapter = browser() if callable(browser) else browser
        if adapter is None:
            return []
        try:
            if hasattr(adapter, "available") and not adapter.available():
                return []
            tabs = adapter.tabs()
        except Exception:                                       # noqa: BLE001
            return []
        out = []
        for tab in tabs or ():
            label = getattr(tab, "title", "") or getattr(tab, "url", "")
            handle = getattr(tab, "handle", "") or getattr(tab, "url", "")
            if not handle:
                continue
            out.append(Candidate(kind="tab", label=label, target=str(handle), source="browser",
                                 foreground=bool(getattr(tab, "active", False)),
                                 detail=getattr(tab, "url", "")[:200]))
        return out
    return provide


def window_candidates(desktop):
    """Open application windows, as referenceable things.

    Carries the double duty of covering both "that window" and conversations inside a messaging
    application: a window titled "Rushi - WhatsApp" is offered as a ``conversation`` as well as a
    ``window``, which is how "open Rushi's chat" resolves without anything in V.O.I.D knowing what WhatsApp
    is. The kind is inferred from the application, not from a list of known chat apps' behaviours.
    """
    def provide():
        adapter = desktop() if callable(desktop) else desktop
        if adapter is None:
            return []
        try:
            if hasattr(adapter, "available") and not adapter.available():
                return []
            windows = adapter.windows()
        except Exception:                                       # noqa: BLE001
            return []
        out = []
        for window in windows or ():
            handle = getattr(window, "handle", "")
            if not handle:
                continue
            title = getattr(window, "title", "")
            process = getattr(window, "process", "")
            active = bool(getattr(window, "active", False))
            out.append(Candidate(kind="window", label=title or process, target=str(handle),
                                 source="desktop", foreground=active, detail=process))
            # A messaging window is also a conversation. Offered as a second candidate rather than
            # reclassified, so "that window" and "that chat" can both find it.
            if _looks_like_conversation(title, process):
                out.append(Candidate(kind="conversation", label=title or process, target=str(handle),
                                     source="desktop", foreground=active, detail=process))
        return out
    return provide


#: Window-title shapes that indicate a one-to-one conversation. Structural, not a list of applications:
#: "<name> - <app>" and "Chat with <name>" are how messaging clients title a thread, whichever client it is.
_CONVERSATION_HINTS = (" - chat", "chat with ", " | chat", "(dm)", " - message", "messages")


def _looks_like_conversation(title: str, process: str) -> bool:
    text = f"{title} {process}".lower()
    if any(hint in text for hint in _CONVERSATION_HINTS):
        return True
    # "<something> - <app>" where the app side is a messaging-shaped name. Judged from the title's own
    # structure so no application needs to be hardcoded; a false positive only adds a candidate, which
    # scoring then has to justify anyway.
    return " - " in title and any(word in text for word in ("chat", "message", "whatsapp", "teams",
                                                            "slack", "discord", "telegram", "signal"))


def document_candidates(recent):
    """Documents V.O.I.D created this session.

    The direct answer to "open this chart": a deck or workbook V.O.I.D just produced is the most likely
    referent of "this", and it is marked ``produced`` so scoring can say so.
    """
    def provide():
        store = recent() if callable(recent) else recent
        if store is None:
            return []
        try:
            return [candidate for candidate in store.candidates() if candidate.kind == "document"]
        except Exception:                                       # noqa: BLE001
            return []
    return provide


def recent_candidates(recent):
    """Everything in the recent record, whatever its kind."""
    def provide():
        store = recent() if callable(recent) else recent
        if store is None:
            return []
        try:
            return store.candidates()
        except Exception:                                       # noqa: BLE001
            return []
    return provide
