"""A2UI: dynamic surfaces V.O.I.D can describe, rendered by something that does not trust it.

The requirement is a UI that adapts to the task - a progress view, an approval request, a research result
list, a comparison table - without the agent being able to invent privileged behaviour. The way that goes
wrong is well known: let a model emit markup or code, render it, and the model now owns the interface.

So the architecture here is deliberately the other way round:

    V.O.I.D agent -> structured A2UI representation -> trusted renderer -> interface

The representation is **data describing intent**, never code. The renderer owns everything that could
matter: which components exist, what their properties may be, what an interaction means, and what - if
anything - an interaction is allowed to authorize. A surface is a *request to display*, and the renderer is
free to refuse it.

Four rules, each enforced by code below rather than by convention:

**A closed component catalog.** :data:`CATALOG` lists every component and the exact properties it accepts,
with types. An unknown component is refused; an unknown property is dropped. There is no passthrough, no
``html``, no ``script``, no ``style``, no ``onclick``, and no component whose contents are interpreted as
anything but text.

**Interactions are allowlisted intents, not operations.** A component may only offer an intent from
:data:`INTENTS`. It cannot name a tool, a path, a URL to call, or a capability. "Approve" is an intent;
"run ``delete_file``" is not expressible.

**An inbound event carries a token, not a decision about what to do.** When V.O.I.D asks for approval it
mints a token and remembers what that token was for. The UI sends the token back. It cannot describe what
it is approving, so a tampered or replayed message cannot approve something other than what was shown.
Tokens are single-use and expire. See :class:`A2uiSession`.

**An A2UI message is not authority.** A returned approval is evidence that a human clicked, which the
RiskGate may then use as the owner's decision for *that specific pending action*. It is not permission for
anything else, it cannot create a capability, and it cannot bypass the consequential check - the gate still
runs, with this as its input.

**Version.** A2UI has no published Python package, so there is nothing to pin: this is the smallest
V.O.I.D-native representation that satisfies the requirement, isolated behind this module so that adopting
an upstream library later means rewriting one file. :data:`SCHEMA_VERSION` identifies the shape V.O.I.D
emits so a renderer can refuse one it does not understand.
"""
from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field

from void.perception import clean_text

_log = logging.getLogger(__name__)

#: The representation version V.O.I.D emits. A renderer that does not know this must refuse the surface
#: rather than guess at it.
SCHEMA_VERSION = "void.a2ui/1"

#: How deep a surface may nest. Flat is the normal case; this exists so a malformed or hostile surface
#: cannot be a recursion attack on the renderer.
MAX_DEPTH = 4

#: How many components one surface may contain, at any depth.
MAX_COMPONENTS = 120

#: Longest text any single property may carry.
MAX_TEXT = 2000

#: How long an approval token is valid. An approval the owner never answered must not sit valid forever.
TOKEN_TTL_S = 600.0


class A2uiError(ValueError):
    """A surface or an inbound message was not acceptable. The message is safe to show."""


#: Interactions a component may offer. A closed set: this is the complete vocabulary of things a UI
#: interaction can mean to V.O.I.D.
#:
#: Note what is absent. There is no ``run``, no ``execute``, no ``call``, no ``open_url``, no
#: ``set_config``. The most powerful thing a click can mean is "the owner approved the pending action you
#: already described to them", and even that is only an input to the RiskGate.
INTENTS = frozenset({
    "approve",          # the owner approves the pending action this surface described
    "reject",           # the owner declines it
    "dismiss",          # close the surface, change nothing
    "choose",           # pick one of the options the surface offered, by index
    "request_detail",   # ask V.O.I.D to say more; becomes an ordinary request
})

#: Every component V.O.I.D may ask for, and the properties it accepts.
#:
#: Types are checked, not coerced from arbitrary input: a property of the wrong type is dropped rather than
#: stringified, so a renderer never receives something structurally surprising.
CATALOG: dict[str, dict[str, type]] = {
    "heading":          {"text": str, "level": int},
    "text":             {"text": str, "tone": str},
    "key_value":        {"label": str, "value": str},
    "badge":            {"text": str, "tone": str},
    "divider":          {},
    "list":             {"items": list, "ordered": bool},
    "table":            {"columns": list, "rows": list, "caption": str},
    "progress":         {"label": str, "step": int, "total": int, "status": str},
    "task_history":     {"items": list},
    "approval_request": {"summary": str, "risk": str, "token": str},
    "artifact_preview": {"name": str, "kind": str, "detail": str},
    "source_list":      {"items": list},
    "device_status":    {"items": list},
    "provider_status":  {"items": list},
    "form":             {"fields": list, "submit_label": str},
    "choice":           {"label": str, "options": list},
    "group":            {"title": str, "children": list},
}

#: Property names refused outright wherever they appear, whatever the component.
#:
#: None of these is in :data:`CATALOG`, so they would be dropped anyway. They are named explicitly because
#: a reader needs to see that the executable-content question was considered and answered, and because a
#: future edit that adds one to the catalog should look obviously wrong.
FORBIDDEN_PROPS = frozenset({
    "html", "innerhtml", "script", "javascript", "src", "href", "url", "style", "css",
    "onclick", "onload", "onerror", "handler", "callback", "eval", "code", "template",
    "component", "render", "import", "module", "action", "tool", "command", "exec",
})

#: Tones a renderer is expected to understand. Anything else is dropped rather than passed through, so a
#: tone cannot become a style injection point.
TONES = frozenset({"neutral", "good", "warning", "danger", "muted"})


@dataclass(frozen=True)
class Component:
    """One validated component. Built only through :func:`component`, never from raw input."""

    kind: str
    props: dict = field(default_factory=dict)
    children: tuple = ()
    #: The intent a click on this component means, when it is interactive at all.
    intent: str = ""

    def as_dict(self) -> dict:
        out: dict = {"kind": self.kind, "props": dict(self.props)}
        if self.intent:
            out["intent"] = self.intent
        if self.children:
            out["children"] = [child.as_dict() for child in self.children]
        return out


@dataclass
class Surface:
    """A validated surface: what V.O.I.D is asking to be displayed."""

    title: str = ""
    components: tuple = ()
    #: The task this surface belongs to, so a renderer can route and a reply can be correlated.
    task_id: str = ""
    version: str = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return {"version": self.version, "title": self.title, "task_id": self.task_id,
                "components": [component.as_dict() for component in self.components]}

    @property
    def count(self) -> int:
        def walk(components) -> int:
            return sum(1 + walk(component.children) for component in components)
        return walk(self.components)


def component(kind: str, intent: str = "", children=(), **props) -> Component:
    """Build one component, validated against the catalog. Raises :class:`A2uiError` if it is not allowed.

    This is the only constructor. Everything a surface contains has been through here, which is what makes
    "the renderer owns the component set" true rather than aspirational.
    """
    name = clean_text(kind, 40).lower().replace(" ", "_")
    allowed = CATALOG.get(name)
    if allowed is None:
        raise A2uiError(f"'{kind}' is not a component V.O.I.D can ask for.")

    wanted_intent = clean_text(intent, 30).lower()
    if wanted_intent and wanted_intent not in INTENTS:
        raise A2uiError(f"'{intent}' is not an interaction a surface may offer.")

    clean: dict = {}
    for key, value in (props or {}).items():
        prop = str(key).strip().lower()
        if prop in FORBIDDEN_PROPS:
            # Refused loudly rather than dropped: an attempt to put executable content in a surface is a
            # bug worth seeing, not something to quietly tidy away.
            raise A2uiError(f"'{prop}' is never allowed in a surface.")
        expected = allowed.get(prop)
        if expected is None:
            continue
        if expected is bool:
            if not isinstance(value, bool):
                continue
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
        elif expected is str:
            if not isinstance(value, str):
                continue
            value = clean_text(value, MAX_TEXT)
            if not value:
                continue
            if prop == "tone" and value not in TONES:
                continue
        elif expected is list:
            if not isinstance(value, (list, tuple)):
                continue
            value = _clean_rows(value)
        clean[prop] = value

    kids = tuple(child for child in (children or ()) if isinstance(child, Component))
    if kids and name != "group":
        raise A2uiError("only a group may contain other components.")
    return Component(kind=name, props=clean, children=kids, intent=wanted_intent)


def _clean_rows(value, depth: int = 0) -> list:
    """Flatten a list property to text, one level of nesting allowed for table rows.

    Everything ends up as a bounded string. A list property is for display, so there is no reason for it to
    carry a dict, an object or a callable, and allowing one would be a way to smuggle structure past the
    property type check.
    """
    out: list = []
    for entry in list(value)[:60]:
        if isinstance(entry, (list, tuple)) and depth == 0:
            out.append(_clean_rows(entry, depth + 1))
        elif isinstance(entry, bool):
            out.append("yes" if entry else "no")
        elif isinstance(entry, (int, float)):
            out.append(str(entry))
        elif isinstance(entry, str):
            cleaned = clean_text(entry, 300)
            if cleaned:
                out.append(cleaned)
        # Anything else - dict, object, callable - is dropped.
    return out


def surface(title: str = "", components=(), task_id: str = "") -> Surface:
    """Assemble and validate a surface. Raises :class:`A2uiError` if it is not acceptable."""
    built = tuple(entry for entry in (components or ()) if isinstance(entry, Component))
    probe = Surface(title=clean_text(title, 200), components=built,
                    task_id=clean_text(task_id, 64))
    if probe.count > MAX_COMPONENTS:
        raise A2uiError("that surface is too large to display.")
    if _depth(built) > MAX_DEPTH:
        raise A2uiError("that surface is nested too deeply.")
    return probe


def _depth(components, level: int = 1) -> int:
    deepest = level if components else 0
    for entry in components:
        if entry.children:
            deepest = max(deepest, _depth(entry.children, level + 1))
    return deepest


def validate_incoming(message) -> dict:
    """Validate a message arriving FROM a renderer. Raises :class:`A2uiError` if it is not acceptable.

    Deliberately minimal. A valid inbound message is a token, an intent, and optionally a chosen index -
    nothing else is read, so there is nothing else to tamper with. In particular the message does not say
    what is being approved: V.O.I.D looks that up from the token it minted.
    """
    if not isinstance(message, dict):
        raise A2uiError("that is not a UI message.")
    intent = clean_text(message.get("intent"), 30).lower()
    if intent not in INTENTS:
        raise A2uiError("that is not an interaction V.O.I.D understands.")
    token = clean_text(message.get("token"), 64)
    if intent in ("approve", "reject") and not token:
        raise A2uiError("that approval does not refer to anything.")
    out: dict = {"intent": intent, "token": token}
    choice = message.get("choice")
    if isinstance(choice, int) and not isinstance(choice, bool) and 0 <= choice < 100:
        out["choice"] = choice
    text = message.get("text")
    if intent == "request_detail" and isinstance(text, str):
        # Treated as ORDINARY USER TEXT, to be handled by the normal request path. It is not a command and
        # grants nothing; it is quoted here only so the caller can pass it to Assistant.run.
        out["text"] = clean_text(text, 500)
    return out


@dataclass
class Pending:
    """An approval V.O.I.D is waiting on, and what it was actually for."""

    token: str
    task_id: str
    summary: str
    risk: str = "high"
    at: float = field(default_factory=time.time)
    used: bool = False

    def expired(self, now: float | None = None) -> bool:
        return ((time.time() if now is None else now) - self.at) > TOKEN_TTL_S


class A2uiSession:
    """Mints approval tokens and resolves them. The reason a UI reply cannot lie.

    V.O.I.D describes an action, mints a token for it, and remembers the pairing. The renderer shows the
    description and sends the token back. Because the reply carries no description of its own, a tampered,
    replayed or fabricated message cannot approve something other than what the owner was shown:

    * an unknown token resolves to nothing;
    * a token resolves exactly once, so a captured reply cannot be replayed;
    * a token expires, so an approval nobody answered does not stay valid;
    * a token belongs to one task, so a reply cannot be moved to another.
    """

    def __init__(self, max_pending: int = 32):
        self._pending: dict[str, Pending] = {}
        self._max = max(1, int(max_pending))

    def request_approval(self, task_id: str, summary: str, risk: str = "high") -> Pending:
        """Mint a token for an action awaiting the owner's decision, and the surface to show."""
        if len(self._pending) >= self._max:
            # Drop the oldest rather than refuse: a stale unanswered approval is less important than
            # being able to ask about the current one.
            oldest = min(self._pending.values(), key=lambda entry: entry.at)
            self._pending.pop(oldest.token, None)
        token = secrets.token_urlsafe(16)
        pending = Pending(token=token, task_id=clean_text(task_id, 64),
                          summary=clean_text(summary, 300), risk=clean_text(risk, 16) or "high")
        self._pending[token] = pending
        return pending

    def approval_surface(self, pending: Pending) -> Surface:
        """The surface that asks the owner about ``pending``."""
        return surface(title="Confirm", task_id=pending.task_id, components=(
            component("approval_request", summary=pending.summary, risk=pending.risk,
                      token=pending.token),
            component("text", text="This needs your go-ahead.", tone="warning"),
        ))

    def resolve(self, message, now: float | None = None) -> tuple[Pending | None, str]:
        """``(pending, intent)`` for a validated inbound message, or ``(None, intent)``.

        ``None`` means the reply refers to nothing V.O.I.D is waiting on - unknown, already used, expired.
        The caller must treat that as no decision at all, never as approval.
        """
        checked = validate_incoming(message)
        intent = checked["intent"]
        token = checked.get("token") or ""
        if intent not in ("approve", "reject"):
            return None, intent
        pending = self._pending.get(token)
        if pending is None:
            _log.info("A2UI_UNKNOWN_TOKEN")
            return None, intent
        if pending.used or pending.expired(now):
            _log.info("A2UI_STALE_TOKEN used=%s", pending.used)
            self._pending.pop(token, None)
            return None, intent
        pending.used = True
        self._pending.pop(token, None)
        return pending, intent

    def waiting(self) -> int:
        return len(self._pending)


class A2uiRenderer:
    """The trusted side: decides whether a surface may be displayed at all.

    Separate from surface construction on purpose. ``component()`` validates what V.O.I.D builds, and this
    validates what a renderer is handed - including a surface that arrived as plain data from somewhere
    else, which is the case where none of the construction-time checks ran.
    """

    def __init__(self, allowed=None, max_components: int = MAX_COMPONENTS):
        #: The components THIS renderer supports, which may be narrower than the catalog. A renderer is
        #: entitled to support less; it can never support more.
        self._allowed = frozenset(allowed) if allowed else frozenset(CATALOG)
        self._max = max(1, int(max_components))

    def accepts(self, kind: str) -> bool:
        return kind in self._allowed and kind in CATALOG

    def validate(self, payload) -> Surface:
        """Rebuild a surface from untrusted data, or raise :class:`A2uiError`.

        Every component is reconstructed through :func:`component`, so data that arrived from outside gets
        exactly the same checks as data V.O.I.D built itself. Nothing is trusted because of where it came
        from.
        """
        if not isinstance(payload, dict):
            raise A2uiError("that is not a surface.")
        version = clean_text(payload.get("version"), 40)
        if version != SCHEMA_VERSION:
            raise A2uiError(f"that surface uses '{version or 'no'}' format, which I cannot render.")
        raw = payload.get("components")
        if not isinstance(raw, (list, tuple)):
            raise A2uiError("that surface has no components.")
        rebuilt = self._rebuild(raw, depth=1)
        built = surface(title=payload.get("title") or "", components=rebuilt,
                        task_id=payload.get("task_id") or "")
        if built.count > self._max:
            raise A2uiError("that surface is too large to display.")
        return built

    def _rebuild(self, raw, depth: int) -> list:
        if depth > MAX_DEPTH:
            raise A2uiError("that surface is nested too deeply.")
        out = []
        for entry in list(raw)[:MAX_COMPONENTS]:
            if not isinstance(entry, dict):
                continue
            kind = clean_text(entry.get("kind"), 40).lower()
            if not self.accepts(kind):
                raise A2uiError(f"'{kind or 'that'}' is not a component I can render.")
            props = entry.get("props")
            props = props if isinstance(props, dict) else {}
            kids = entry.get("children")
            children = self._rebuild(kids, depth + 1) if isinstance(kids, (list, tuple)) else []
            out.append(component(kind, intent=entry.get("intent") or "",
                                 children=children, **props))
        return out
