"""Reaching a person, not an application: "open Rushi's chat".

The blueprint's example names a person and a kind of thing, and never names the program. That is the
whole difficulty, and the reason this is not a feature of WhatsApp support:

    "open Rushi's chat"  ->  which application?  ->  which conversation inside it?  ->  is it open?

**There is no special case for any application, and no special case for any person.** What this
module holds is a *vocabulary* of messaging surfaces (:data:`MESSAGING_APPS`), in exactly the sense
:data:`void.browser.playwright_adapter._BROWSERS` is a vocabulary of browsers. A messaging
application is a row: a canonical name, how the owner might say it, and the process image names the
window layer reports for it. Teaching V.O.I.D another messenger is a row, never a branch. Nothing
anywhere asks "is this WhatsApp?" in order to decide what to do.

**The contact is matched, never executed.** ``contact`` is the one place a word from the owner's
sentence travels into a route, and it has to: there is no way to open Rushi's chat without the string
"Rushi". It is cleaned, length-bounded, and used only as a needle against accessible names that the
application itself published - the same thing :mod:`void.orchestration.reference` already does with a
qualifier. It is never interpolated into a command, a path or a shell.

**Choosing the application is the existing precedence chain, reused.** No new policy was invented:

1. the application the owner named **in this utterance** - via the existing
   :mod:`void.orchestration.overrides`, with ``messaging`` as just another overridable role;
2. **current observed state** - a conversation with that contact already open in exactly one
   application. Evidence beats a default;
3. the owner's stored ``messaging`` **preference**, when that application is installed;
4. the only installed messaging application, when there is exactly one;
5. otherwise **ask**. More than one plausible application and no signal is genuine ambiguity, and
   opening the wrong person's chat in the wrong place is not a small error.

Security properties this module is responsible for:

* **Opening a conversation is not sending a message.** The two are different actions with different
  consequences, and a route produced here can only reach a conversation. The send guard lives in
  :mod:`void.actions.messaging`, which refuses to click a control that looks like it sends, calls or
  records anything - see :data:`NEVER_CLICK`.
* **A plan is not an authorization.** Everything here returns a description. Execution still goes
  through the tool funnel, the kill switch and ``RiskGate``, unchanged.
* **Every label is untrusted.** Window titles and control names are chosen by the application, not by
  the owner. They are cleaned and bounded, used for matching and display, and never trusted as
  identity.
* **Nothing is enumerated behind the owner's back.** This module reads what the window layer already
  sees. It does not open applications to inventory contacts, and it reads no message content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from void.orchestration.reference import KIND_WORDS, parse_reference
from void.orchestration.routes import DirectCall, Route, RouteKind, WorldState
from void.perception import clean_text

_log = logging.getLogger(__name__)

#: Longest contact needle kept. A name, not a sentence: anything longer is a parse accident rather
#: than a person, and an unbounded needle would be a way to push a large string into a match loop.
MAX_CONTACT = 60

#: The nouns that mean "a one-to-one conversation", taken from the reference resolver's own
#: vocabulary so there is one list rather than two drifting apart.
CONVERSATION_NOUNS = frozenset(word for word, kind in KIND_WORDS.items() if kind == "conversation")


def spoken(name: str) -> str:
    """A contact name as it should be said back to the owner.

    The parser lower-cases, because matching must be case-insensitive; "Which app do you mean for
    rushi" is the wrong thing to say to a person about a person. Only an all-lower-case name is
    capitalised, so a label the application itself supplied ("McDonald", "iPhone") keeps its own
    shape. Display only - matching never uses this.
    """
    text = clean_text(name, MAX_CONTACT)
    if not text or text != text.lower():
        return text
    return " ".join(word[:1].upper() + word[1:] for word in text.split())


@dataclass(frozen=True)
class MessagingApp:
    """One messaging surface V.O.I.D can recognise and reach.

    A row of data. Deliberately holds no behaviour and no selectors: how a conversation is reached is
    the same for every row (find the window, find a control named like the contact, activate it), so a
    per-application method would be the special-casing this design exists to avoid.
    """

    #: Canonical lower-case name. The key everything else uses, and the spelling a preference or an
    #: override must resolve to.
    name: str
    #: How to refer to it when speaking to the owner.
    display: str
    #: Spoken forms that mean this application.
    aliases: tuple[str, ...] = ()
    #: Process image names the window layer reports, lower-case. Several because installers differ
    #: ("whatsapp.exe" from the Store, "whatsapp.root.exe" from the desktop build).
    processes: tuple[str, ...] = ()

    @property
    def vocabulary(self) -> frozenset[str]:
        return frozenset({self.name, *self.aliases})

    def matches_process(self, process: str) -> bool:
        probe = (process or "").strip().lower()
        if not probe:
            return False
        return any(probe == known or probe.startswith(known.rsplit(".", 1)[0])
                   for known in self.processes)

    def matches_name(self, text: str) -> bool:
        """Does a catalog entry's display name or a window title refer to this application?"""
        probe = " ".join((text or "").strip().lower().split())
        if not probe:
            return False
        return any(word == probe or word in probe.split() or probe.startswith(word + " ")
                   for word in self.vocabulary)


#: The messaging surfaces this project knows. Ordered only for stable output; no row is privileged,
#: and none of them has code of its own anywhere in V.O.I.D.
#:
#: What makes a row legitimate is that the window layer can see the application and Windows UI
#: Automation can read its contact list - not that V.O.I.D has special support for it. A row for an
#: application that is not installed simply never matches.
MESSAGING_APPS: tuple[MessagingApp, ...] = (
    MessagingApp("whatsapp", "WhatsApp", ("whats app", "whatsapp desktop"),
                 ("whatsapp.exe", "whatsapp.root.exe")),
    MessagingApp("telegram", "Telegram", ("telegram desktop",), ("telegram.exe",)),
    MessagingApp("signal", "Signal", (), ("signal.exe",)),
    MessagingApp("slack", "Slack", (), ("slack.exe",)),
    MessagingApp("discord", "Discord", (), ("discord.exe",)),
    MessagingApp("teams", "Microsoft Teams", ("microsoft teams", "ms teams"),
                 ("ms-teams.exe", "teams.exe", "msteams.exe")),
)


def messaging_vocabulary() -> frozenset[str]:
    """Every spoken form that names a messaging application in the table."""
    names: set[str] = set()
    for app in MESSAGING_APPS:
        names |= app.vocabulary
    return frozenset(names)


def app_by_name(name: str) -> MessagingApp | None:
    """The row a canonical name or alias refers to, or None."""
    probe = " ".join((name or "").strip().lower().split())
    if not probe:
        return None
    for app in MESSAGING_APPS:
        if probe == app.name or probe in app.aliases:
            return app
    return None


def installed_apps(catalog_names) -> tuple[MessagingApp, ...]:
    """Which messaging applications a list of installed application names covers.

    The name-list form, for callers that already hold one (and for tests). Reads names rather than
    looking for executables itself: the catalog owns discovery, and a second discovery mechanism
    could disagree with it.
    """
    present: list[MessagingApp] = []
    names = [clean_text(name, 80) for name in (catalog_names or ())]
    for app in MESSAGING_APPS:
        if any(app.matches_name(name) for name in names):
            present.append(app)
    return tuple(present)


def installed_via_catalog(catalog) -> tuple[MessagingApp, ...]:
    """Which messaging applications the catalog can actually resolve to something launchable.

    Preferred over :func:`installed_apps` wherever a catalog is in hand, because it asks the question
    the launcher will ask. ``resolve_name`` is the existing deterministic matcher - the one with the
    exact / spacing / prefix / publisher / sound tiers - so "teams" finds "Microsoft Teams" the same
    way it does for every other application, and an application that is merely *mentioned* somewhere
    does not count as installed.

    A row that resolves ambiguously is left out: if V.O.I.D cannot say which installed application a
    messaging name means, it cannot launch it either, and claiming it is installed would turn that
    into a failure later instead of a clear answer now.
    """
    if catalog is None:
        return ()
    present: list[MessagingApp] = []
    for app in MESSAGING_APPS:
        try:
            match = catalog.resolve_name(app.name)
        except Exception:                                      # noqa: BLE001 - absent beats crashing
            _log.debug("MESSAGING_CATALOG_UNAVAILABLE", exc_info=True)
            continue
        if getattr(match, "entry", None) is not None:
            present.append(app)
    return tuple(present)


# --------------------------------------------------------------------------- the request

@dataclass(frozen=True)
class ConversationRequest:
    """What the owner asked for, parsed deterministically.

    ``wants_conversation`` is false for every sentence that is not about a conversation, which is the
    common case and the one in which this module proposes nothing at all.
    """

    phrase: str = ""
    contact: str = ""
    #: The messaging application named in this utterance, canonical, or "" when none was.
    app: str = ""
    wants_conversation: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.wants_conversation and self.contact)

    def as_dict(self) -> dict:
        return {"contact": self.contact, "app": self.app,
                "wants_conversation": self.wants_conversation}


def _attributive_app(text: str, vocabulary) -> str:
    """The application in "Rushi's **WhatsApp** chat" - a name used as an adjective.

    Needed because this form is not a choice phrase: :func:`extract_overrides` recognises "in X" and
    "with X", and "WhatsApp chat" is neither. Kept deliberately tight - the name has to sit
    *immediately* before a conversation noun - so that "WhatsApp is slow, open Rushi's chat" selects
    nothing, exactly as "Edge is slow, open Gmail" selects no browser.
    """
    words = re.findall(r"[\w.+-]+", (text or "").lower())
    for index, word in enumerate(words[:-1]):
        if words[index + 1] not in CONVERSATION_NOUNS:
            continue
        # Try the two-word form first ("microsoft teams chat") so a multi-word name is not lost.
        if index and " ".join(words[index - 1:index + 1]) in vocabulary:
            return " ".join(words[index - 1:index + 1])
        if word in vocabulary:
            return word
    return ""


def parse_request(goal: str, *, vocabulary=None, overrides: dict | None = None
                  ) -> ConversationRequest:
    """Turn an utterance into a conversation request, or into one that wants nothing.

    The conversation/contact parse is :func:`void.orchestration.reference.parse_reference` - the same
    parser that resolves "this chart", with no messaging-specific grammar added. What this adds is
    which application was named, from the two forms the owner actually uses.
    """
    phrase = clean_text(goal, 300)
    if not phrase:
        return ConversationRequest()
    reference = parse_reference(phrase)
    if reference.kind != "conversation":
        return ConversationRequest(phrase=phrase)

    vocab = messaging_vocabulary() if vocabulary is None else frozenset(vocabulary)
    # "in WhatsApp" / "with Telegram" comes from the existing override extractor; the owner is
    # contradicting or supplying a default for the `messaging` role exactly as they would for a
    # browser. The attributive form is the only thing parsed here.
    named = str((overrides or {}).get("messaging") or "").strip().lower()
    if not named:
        named = _attributive_app(phrase, vocab)
    resolved = app_by_name(named)

    contact = _contact_from(reference.qualifier, vocab)
    return ConversationRequest(phrase=phrase, contact=contact,
                               app=resolved.name if resolved else "",
                               wants_conversation=True)


def _contact_from(qualifier: str, vocabulary) -> str:
    """The person's name, with any application name removed.

    "open whatsapp chat with rushi" leaves "whatsapp rushi" as the qualifier when there is no
    possessive to latch onto. Searching a contact list for "whatsapp rushi" finds nobody, so the
    application words come out - they named the surface, not the person.
    """
    words = [word for word in re.findall(r"[\w.'+-]+", (qualifier or "").lower())
             if word not in CONVERSATION_NOUNS]
    kept: list[str] = []
    index = 0
    while index < len(words):
        pair = " ".join(words[index:index + 2])
        if pair in vocabulary:
            index += 2
            continue
        if words[index] in vocabulary:
            index += 1
            continue
        kept.append(words[index])
        index += 1
    return clean_text(" ".join(kept), MAX_CONTACT)


# --------------------------------------------------------------------------- the plan

class MessagingAction:
    """What should happen. One of these, decided before anything is executed."""

    #: A window already showing this conversation - activate it, open nothing.
    REUSE = "reuse"
    #: The application is running; find the contact inside it.
    NAVIGATE = "navigate"
    #: The application is installed but not running; start it, then find the contact.
    LAUNCH = "launch"
    #: More than one plausible application and nothing to choose by. Ask the owner.
    ASK = "ask"
    #: Nothing can be done, and ``reason`` says why in words fit to speak.
    NONE = "none"


@dataclass(frozen=True)
class MessagingPlan:
    """The decision, with its evidence. Inspectable, and not yet executed."""

    action: str = MessagingAction.NONE
    contact: str = ""
    #: Canonical application name, when one was chosen.
    app: str = ""
    display: str = ""
    #: For :attr:`MessagingAction.REUSE`: the window handle to activate.
    window: str = ""
    #: Applications the owner should choose between, when ``action`` is ASK.
    options: tuple[str, ...] = ()
    reason: str = ""
    why: str = ""
    #: True when the request was never about a conversation. Distinguishes "not my business" from
    #: "I tried and could not", which are different answers to the owner.
    irrelevant: bool = False

    @property
    def actionable(self) -> bool:
        return self.action in (MessagingAction.REUSE, MessagingAction.NAVIGATE,
                               MessagingAction.LAUNCH)

    def question(self) -> str:
        """What to ask when the application is genuinely ambiguous."""
        if self.action != MessagingAction.ASK or not self.options:
            return ""
        names = [app_by_name(name).display if app_by_name(name) else name for name in self.options]
        listed = ", ".join(names[:-1]) + f" or {names[-1]}" if len(names) > 1 else names[0]
        who = spoken(self.contact) or "that person"
        return f"Which app do you mean for {who} - {listed}?"

    def as_dict(self) -> dict:
        return {"action": self.action, "contact": self.contact, "app": self.app,
                "options": list(self.options), "reason": self.reason, "why": self.why,
                "actionable": self.actionable}


@dataclass
class OpenConversation:
    """An observed window that is already showing a conversation.

    ``participant`` is read out of a window title the application wrote, so it is untrusted and only
    ever compared, never believed as identity.
    """

    app: str
    participant: str
    window: str
    title: str = ""
    foreground: bool = False


def observed_conversations(windows) -> tuple[OpenConversation, ...]:
    """Conversation windows among the observed windows, with who they appear to be with.

    Derived from the window's own process and title. A title like "Rushi - WhatsApp" names the
    participant; a title of just "WhatsApp" does not, and yields a conversation with an empty
    participant - which is the truth, and is why a bare application window never matches a contact.
    """
    found: list[OpenConversation] = []
    for window in windows or ():
        process = clean_text(getattr(window, "process", "") or "", 80).lower()
        title = clean_text(getattr(window, "title", "") or "", 200)
        handle = str(getattr(window, "handle", "") or "")
        if not handle:
            continue
        app = next((row for row in MESSAGING_APPS
                    if row.matches_process(process) or row.matches_name(title)), None)
        if app is None:
            continue
        found.append(OpenConversation(app=app.name, participant=_participant(title, app),
                                      window=handle, title=title,
                                      foreground=bool(getattr(window, "active", False))))
    return tuple(found)


def _participant(title: str, app: MessagingApp) -> str:
    """Who a conversation window's title says it is with, or "".

    Structural: messaging clients title a thread "<participant> - <app>" or "Chat with <participant>".
    Judged from the title's shape, so no application needs a rule of its own, and a title that is
    only the application name correctly yields nobody.
    """
    text = clean_text(title, 200)
    lowered = text.lower()
    for marker in ("chat with ", "conversation with ", "message to "):
        if marker in lowered:
            return clean_text(text[lowered.index(marker) + len(marker):], MAX_CONTACT)
    for separator in (" - ", " | ", " – "):
        if separator in text:
            head, _, tail = text.partition(separator)
            # Either side may be the application; the other side is then the participant.
            if app.matches_name(tail) and not app.matches_name(head):
                return clean_text(head, MAX_CONTACT)
            if app.matches_name(head) and not app.matches_name(tail):
                return clean_text(tail, MAX_CONTACT)
    return ""


def mentions(haystack: str, needle: str) -> bool:
    """Does a label name this contact? Every word of the needle must appear as a whole word.

    Whole-word rather than substring on purpose: "Ann" must not match "Joanna", because clicking the
    wrong person's conversation is the error this whole module is arranged to avoid.

    The cost is that initials do not match - "Rushi K" does not find "Rushi Kumar", because "k"
    is not a word in it. That is the right trade: the failure is an honest "I could not find them",
    whereas prefix matching would make "Ann" find both "Annabel" and "Ann" and turn a clear answer
    into a guess. A full name, or any single whole word of it, matches.
    """
    label = (haystack or "").lower()
    wanted = [word for word in re.findall(r"[\w'+-]+", (needle or "").lower()) if word]
    if not wanted or not label:
        return False
    present = set(re.findall(r"[\w'+-]+", label))
    return all(word in present for word in wanted)


def plan_conversation(request: ConversationRequest, *, installed=(), state: WorldState | None = None,
                      conversations=()) -> MessagingPlan:
    """Decide how to reach a conversation, or decide to ask.

    Pure: observed state in, a description out. Nothing here opens, launches or clicks anything, so
    the decision can be inspected and tested without a machine in a particular condition.
    """
    if not request.wants_conversation:
        return MessagingPlan(reason="that is not a request for a conversation", irrelevant=True)
    if not request.contact:
        return MessagingPlan(contact="", reason="tell me whose conversation to open")

    state = state or WorldState()
    available = tuple(installed)
    by_name = {app.name: app for app in available}

    # (1) The owner named an application. If it is not installed, say so rather than silently using
    # another one - substituting a different messenger would send their message-reading attention to
    # the wrong place, and is exactly the guess this module refuses to make.
    if request.app:
        named = by_name.get(request.app)
        if named is None:
            display = app_by_name(request.app)
            return MessagingPlan(
                contact=request.contact, app=request.app,
                display=display.display if display else request.app,
                reason=f"{display.display if display else request.app} is not installed here")
        return _reach(named, request.contact, state, conversations,
                      why="you asked for " + named.display)

    if not available:
        return MessagingPlan(contact=request.contact,
                             reason="no messaging application I can reach is installed")

    # (2) Observed state beats any default: a conversation with this contact already open is
    # evidence, not a guess. Two of them is ambiguity the owner has to settle.
    matching = tuple({conversation.app for conversation in conversations
                      if conversation.app in by_name
                      and mentions(conversation.participant, request.contact)})
    if len(matching) == 1:
        return _reach(by_name[matching[0]], request.contact, state, conversations,
                      why="that conversation is already open")
    if len(matching) > 1:
        return MessagingPlan(action=MessagingAction.ASK, contact=request.contact,
                             options=tuple(sorted(matching)),
                             reason="that conversation is open in more than one application")

    # (3) The owner's standing choice, through the same WorldState the browser preference uses.
    preferred = state.preferred("messaging")
    if preferred:
        chosen = by_name.get(preferred) or (app_by_name(preferred) and
                                            by_name.get(app_by_name(preferred).name))
        if chosen is not None:
            return _reach(chosen, request.contact, state, conversations,
                          why=f"{chosen.display} is your messaging app")

    # (4) One installed messenger is not a choice, so do not manufacture a question about it.
    if len(available) == 1:
        return _reach(available[0], request.contact, state, conversations,
                      why=f"{available[0].display} is the only messaging app installed")

    # (5) Several plausible applications and nothing to choose by.
    return MessagingPlan(action=MessagingAction.ASK, contact=request.contact,
                         options=tuple(sorted(app.name for app in available)),
                         reason="more than one messaging application is installed")


def _reach(app: MessagingApp, contact: str, state: WorldState,
           conversations, *, why: str) -> MessagingPlan:
    """Given the application, how far away is the conversation?"""
    for conversation in conversations:
        if conversation.app == app.name and mentions(conversation.participant, contact):
            return MessagingPlan(action=MessagingAction.REUSE, contact=contact, app=app.name,
                                 display=app.display, window=conversation.window,
                                 reason=f"that conversation is already open in {app.display}",
                                 why=why)
    running = any(state.is_running(process) for process in app.processes) or state.is_running(app.name)
    if running:
        return MessagingPlan(action=MessagingAction.NAVIGATE, contact=contact, app=app.name,
                             display=app.display,
                             reason=f"{app.display} is open; finding {contact}", why=why)
    return MessagingPlan(action=MessagingAction.LAUNCH, contact=contact, app=app.name,
                         display=app.display,
                         reason=f"starting {app.display} and finding {contact}", why=why)


# --------------------------------------------------------------------------- the route provider

class ConversationRoutes:
    """Conversations, as routes the existing resolver can compare against everything else.

    A provider, not a second routing engine: it answers ``propose`` and the existing
    :class:`void.orchestration.routes.RouteResolver` does the rest - dropping routes whose capability
    is absent, scoring, and reporting ambiguity. A reuse route is ``EXISTING_STATE`` so it wins
    against a launch for exactly the same reason an already-open Gmail tab does.

    Ambiguity *between messaging applications* is settled before any route is proposed, because the
    resolver's own tie-break deliberately treats two same-kind routes as not worth a question - true
    for two ways of opening a web page, false for WhatsApp versus Discord. When the plan says ASK,
    this proposes nothing and the question travels with the plan instead.
    """

    name = "conversations"

    #: Tool the desktop layer registers to bring a window forward. Named, not imported.
    FOCUS_TOOL = "focus_window"
    #: Tool that observes a messaging application and navigates to a contact inside it.
    OPEN_TOOL = "open_conversation"

    def __init__(self, *, installed=None, conversations=None):
        self._installed = installed
        self._conversations = conversations

    def _resolve(self, holder, default):
        if holder is None:
            return default
        try:
            value = holder() if callable(holder) else holder
        except Exception:                                      # noqa: BLE001 - a layer may be absent
            _log.debug("MESSAGING_SOURCE_UNAVAILABLE", exc_info=True)
            return default
        return default if value is None else value

    def plan(self, goal: str, state: WorldState) -> MessagingPlan:
        """The decision for this goal, with the observed state folded in."""
        request = parse_request(goal, overrides=getattr(state, "overrides", None))
        if not request.wants_conversation:
            return MessagingPlan(reason="not a conversation request", irrelevant=True)
        return plan_conversation(
            request,
            installed=self._resolve(self._installed, ()),
            state=state,
            conversations=self._resolve(self._conversations, ()))

    def propose(self, goal: str, state: WorldState):
        plan = self.plan(goal, state)
        if not plan.actionable:
            return ()
        if plan.action == MessagingAction.REUSE:
            return (Route(
                kind=RouteKind.EXISTING_STATE,
                describes=f"switch to the {plan.contact} conversation already open in {plan.display}",
                calls=(DirectCall(name=self.FOCUS_TOOL,
                                  arguments={"window": plan.window},
                                  reply=f"Switched to {plan.contact}."),),
                reuses_existing=True,
                requires=frozenset({"desktop_ui"}),
                reliability=0.95,
                latency_s=0.3,
                why=plan.reason),)
        # Navigating inside an application is UI automation whatever the application is, so it is
        # DESKTOP_UI and scores accordingly: below reusing a window, above reading the screen.
        launching = plan.action == MessagingAction.LAUNCH
        return (Route(
            kind=RouteKind.DESKTOP_UI,
            describes=f"open the {plan.contact} conversation in {plan.display}",
            calls=(DirectCall(name=self.OPEN_TOOL,
                              # The contact is a bounded match needle, never a command. See the
                              # module docstring - this is the one place a word from the sentence
                              # travels into a route, and it has to.
                              arguments={"contact": plan.contact, "app": plan.app},
                              reply=f"Opened {plan.contact} in {plan.display}."),),
            reuses_existing=not launching,
            requires=frozenset({"desktop_ui"}),
            # Honest estimates: reading a contact list and clicking a row is less certain than
            # activating a window that already exists, and starting an application is slower still.
            reliability=0.6 if launching else 0.75,
            latency_s=6.0 if launching else 2.0,
            why=plan.reason),)
