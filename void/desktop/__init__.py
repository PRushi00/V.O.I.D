"""V.O.I.D's desktop abstraction. Windows UI Automation lives behind it, nowhere else.

    V.O.I.D Desktop  (this module - concepts, handles, policy)
          ↓
    UIA adapter      (void/desktop/uia_adapter.py - the ONLY file that imports uiautomation)
          ↓
    Windows UI Automation

**This is the most dangerous capability in V.O.I.D, and the design says so.** A browser is sandboxed to a
page; UI Automation can read and press anything in any window on the desktop - including a password
manager, a banking app, or Windows' own security dialogs. So the controls here are tighter than anywhere
else in the system:

* **Everything is scoped to one identified window.** There is no "click the button called OK" across the
  desktop. A caller opens a window scope first, and every control lookup and action happens inside it. An
  agent cannot wander.
* **Protected processes are refused outright**, reusing V2's existing list (explorer, the shells, lsass,
  the V.O.I.D process itself) plus the owner's additions. UI Automation against the shell or a security
  process is never worth the risk.
* **Handles are engine-minted index paths**, never model-supplied selectors. A model picks something
  V.O.I.D already found and offered; it cannot describe a control it wants.
* **Consequential controls need the owner**, decided from the control's own accessible name by the shared
  vocabulary in :mod:`void.security.consequential` - the same rule the browser layer uses.
* **No typing into password fields.** A control UIA marks as a password is never filled, because the only
  reason to do so would be to supply a credential.

What this deliberately does NOT provide: global keyboard or mouse injection at coordinates. Raw input is
the bottom of the blueprint's route ladder, it is unverifiable, and every use of it V3 actually needs is
better served by an addressable control.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from void.perception import Element, clean_text

#: Control types worth offering as actionable. UIA exposes hundreds of nodes per window; an agent needs
#: the ones it can act on, and a model cannot usefully choose from hundreds.
ACTIONABLE_TYPES = frozenset({
    "ButtonControl", "HyperlinkControl", "EditControl", "ComboBoxControl", "CheckBoxControl",
    "RadioButtonControl", "MenuItemControl", "TabItemControl", "ListItemControl",
    "TreeItemControl", "SliderControl", "SplitButtonControl",
})

#: Types that carry readable text, for reading a window's content without a screenshot.
TEXT_TYPES = frozenset({"TextControl", "EditControl", "DocumentControl", "ListItemControl",
                        "HeaderItemControl", "StatusBarControl"})

#: How many controls one scope walk collects. Bounded so a huge window cannot stall the read.
MAX_CONTROLS = 150

#: How deep into a window's control tree to walk. Chromium-family windows nest deeply; beyond this the
#: useful controls are already found and the cost grows fast.
MAX_DEPTH = 12

#: A handle is ``path:<dot-separated child indices>`` from the window root. Deterministic, re-resolvable,
#: and impossible to confuse with a selector.
#:
#: ``[0-9]`` rather than ``\d``: Python's ``\d`` matches every Unicode decimal digit, so ``path:١.٢`` in
#: Arabic-Indic digits satisfied a check documented as strict and ``int()`` then happily converted it. The
#: handles V.O.I.D mints are ASCII, so anything else is by definition not one of them.
_HANDLE = re.compile(r"^path:([0-9]+(?:\.[0-9]+)*)$")


class DesktopUnavailable(RuntimeError):
    """The desktop could not be driven. The message is safe to say to the owner."""


class ProtectedWindow(DesktopUnavailable):
    """The requested window belongs to a process V.O.I.D must not automate."""


@dataclass(frozen=True)
class DesktopPolicy:
    """What the owner's configuration permits for desktop automation."""

    enabled: bool = False
    #: Extra process image names never automated, on top of V2's engine defaults.
    protected_processes: tuple[str, ...] = ()
    #: Seconds for any single UIA operation. UIA can block on an unresponsive window.
    timeout_s: float = 5.0
    max_controls: int = MAX_CONTROLS
    max_depth: int = MAX_DEPTH

    @classmethod
    def from_config(cls, config) -> "DesktopPolicy":
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default
        extra = get("security.protected_processes", []) or []
        names = tuple(clean_text(name, 60).lower() for name in extra
                      if isinstance(name, str) and name.strip())

        def number(key, default, low, high):
            try:
                return min(max(type(default)(get(key, default)), low), high)
            except (TypeError, ValueError):
                return default
        return cls(enabled=bool(get("desktop.enabled", False)),
                   protected_processes=names,
                   timeout_s=float(number("desktop.timeout_s", 5.0, 0.5, 30.0)),
                   max_controls=int(number("desktop.max_controls", MAX_CONTROLS, 10, 400)),
                   max_depth=int(number("desktop.max_depth", MAX_DEPTH, 2, 30)))


@dataclass(frozen=True)
class WindowRef:
    """One top-level window V.O.I.D may work inside.

    ``handle`` is the native window handle as a string - stable for the window's lifetime and verifiable,
    which is what makes the TOCTOU re-check in the adapter possible. ``title`` and ``process`` are
    UNTRUSTED: a program chooses its own title.
    """

    handle: str
    title: str = ""
    process: str = ""
    class_name: str = ""
    active: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", clean_text(self.title, 200))
        object.__setattr__(self, "process", clean_text(self.process, 80).lower())
        object.__setattr__(self, "class_name", clean_text(self.class_name, 80))

    def matches(self, needle: str) -> bool:
        probe = clean_text(needle, 200).lower()
        if not probe:
            return False
        return probe in self.title.lower() or probe in self.process

    def as_dict(self) -> dict:
        return {"handle": self.handle, "title": self.title, "process": self.process,
                "class_name": self.class_name, "active": self.active}


@dataclass(frozen=True)
class WindowContent:
    """What is inside a window: its readable text and the controls that can be acted on."""

    window: WindowRef
    text: str = ""
    elements: tuple[Element, ...] = ()
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", clean_text(self.text, 4000))

    def as_dict(self) -> dict:
        return {"window": self.window.as_dict(), "text": self.text,
                "elements": [element.as_dict() for element in self.elements],
                "truncated": self.truncated}


def parse_handle(raw: object) -> tuple[int, ...]:
    """An engine-minted control handle as an index path, or raise.

    Strict by design. A handle that is not exactly ``path:1.2.3`` is refused, so a model cannot pass a UIA
    property condition, an XPath, a window title or a coordinate here and have it evaluated. It can only
    name something V.O.I.D already walked to and offered.
    """
    text = clean_text(raw, 120)
    match = _HANDLE.match(text)
    if not match:
        raise DesktopUnavailable("That is not a control I offered.")
    parts = tuple(int(piece) for piece in match.group(1).split("."))
    if not parts or len(parts) > MAX_DEPTH + 2:
        raise DesktopUnavailable("That is not a control I offered.")
    return parts


def make_handle(path) -> str:
    return "path:" + ".".join(str(int(index)) for index in path)
