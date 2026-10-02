"""The Windows UI Automation adapter: the only module in V.O.I.D that imports ``uiautomation``.

Measured on this machine: the UIA root resolves in 156 ms, top-level windows enumerate in 56 ms, and
walking 30 controls costs 4 ms. Fast enough to be the preferred route for native applications, which is
why it sits above OCR and vision on the blueprint's ladder.

Three implementation facts worth stating, because each one prevented a bug:

**COM is per-thread, and UIA objects have thread affinity.** Like Playwright's sync API, ``uiautomation``
must be used from the thread that initialised COM for it. V.O.I.D drives the voice chain from a serial
worker and the CLI from the main thread, so a shared adapter would eventually be called from the wrong one.
:class:`_UiaThread` gives the adapter one thread of its own and marshals every call onto it - the same
shape as ``void.voice.runtime._SerialVoiceWorker`` and the browser adapter's ``_Driver``.

**A window handle is not trustworthy forever.** Windows recycles handles. Every operation re-resolves the
window from its handle and re-checks its process identity before touching anything, which is the same
TOCTOU discipline ``void.actions.computer`` already applies to its window tokens.

**Index paths, not RuntimeId.** ``uiautomation``'s controls on this machine expose ``Name``,
``ControlTypeName``, ``AutomationId``, ``ClassName``, ``IsEnabled``, ``IsOffscreen`` and
``BoundingRectangle`` - but not a usable ``RuntimeId``. So a control's handle is its index path from the
window root, which is deterministic, cheap to re-resolve and impossible to confuse with a selector.
"""
from __future__ import annotations

import logging
import queue
import threading

from void.desktop import (ACTIONABLE_TYPES, TEXT_TYPES, DesktopPolicy, DesktopUnavailable,
                          ProtectedWindow, WindowContent, WindowRef, make_handle, parse_handle)
from void.perception import Element, clean_text

_log = logging.getLogger(__name__)


def _prop(control, name: str, default=""):
    """Read a UIA property, or return ``default``.

    Every property on a UIA element is a live COM call against another process, so any of them can raise
    ``_ctypes.COMError`` the instant that window closes - including a read whose only purpose is to fill in
    a return value. A real failure found this: ``activate()`` succeeded, then raised while reading
    ``control.Name`` for its WindowRef because the window had gone. A property read must never be the thing
    that fails an operation that already worked.
    """
    try:
        value = getattr(control, name)
        return value if value is not None else default
    except Exception:                                          # noqa: BLE001 - incl. _ctypes.COMError
        return default


def _protected_names(policy: DesktopPolicy) -> frozenset[str]:
    """Process names never automated: V2's engine defaults plus the owner's additions.

    Reuses ``void.actions.computer._DEFAULT_PROTECTED`` rather than keeping a second list, so a process the
    owner protected from being closed is also protected from being driven - which is the same intent.
    """
    try:
        from void.actions.computer import _DEFAULT_PROTECTED
        base = set(_DEFAULT_PROTECTED)
    except Exception:                                          # noqa: BLE001 - fail closed, not open
        base = {"explorer.exe", "lsass.exe", "winlogon.exe", "csrss.exe", "services.exe",
                "python.exe", "pythonw.exe"}
    base.update(policy.protected_processes)
    return frozenset(base)


class _UiaThread:
    """Owns the UIA/COM objects on one dedicated thread and runs callables there."""

    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="void-desktop", daemon=True)
        self._started = False
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._thread.start()
                self._started = True

    def _run(self) -> None:
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:                                      # noqa: BLE001 - uiautomation also does this
            pass
        while True:
            job = self._jobs.get()
            if job is None:
                return
            function, reply = job
            try:
                reply.put((True, function()))
            except BaseException as exc:                        # noqa: BLE001 - relayed to the caller
                reply.put((False, exc))

    def call(self, function, timeout: float = 30.0):
        self.start()
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._jobs.put((function, reply))
        try:
            ok, value = reply.get(timeout=timeout)
        except queue.Empty as exc:
            raise DesktopUnavailable("The desktop did not respond in time.") from exc
        if ok:
            return value
        raise value

    def stop(self) -> None:
        with self._lock:
            if self._started:
                self._jobs.put(None)
                self._started = False


class UiaDesktop:
    """V.O.I.D's desktop, implemented with Windows UI Automation."""

    name = "uia"

    def __init__(self, policy: DesktopPolicy | None = None):
        self._policy = policy or DesktopPolicy()
        self._thread = _UiaThread()
        self._protected = _protected_names(self._policy)
        self._auto = None

    @property
    def policy(self) -> DesktopPolicy:
        return self._policy

    def available(self) -> bool:
        if not self._policy.enabled:
            return False
        try:
            import uiautomation  # noqa: F401
            return True
        except Exception:                                      # noqa: BLE001
            return False

    def _uia(self):
        """The uiautomation module, configured. Runs on the desktop thread."""
        if self._auto is None:
            if not self._policy.enabled:
                raise DesktopUnavailable(
                    "Desktop automation is switched off in V.O.I.D's configuration.")
            try:
                import uiautomation as auto
            except Exception as exc:                            # noqa: BLE001
                raise DesktopUnavailable(
                    "Desktop automation needs the uiautomation package, which is not installed."
                ) from exc
            auto.SetGlobalSearchTimeout(max(0.5, float(self._policy.timeout_s)))
            self._auto = auto
        return self._auto

    # -- windows ------------------------------------------------------------------------------------
    def windows(self) -> tuple[WindowRef, ...]:
        """Top-level windows with titles, with their owning process names."""
        def work():
            auto = self._uia()
            try:
                import psutil
            except Exception:                                  # noqa: BLE001
                psutil = None
            try:
                foreground = auto.GetForegroundControl()
                active_handle = str(int(foreground.NativeWindowHandle)) if foreground else ""
            except Exception:                                  # noqa: BLE001
                active_handle = ""
            found: list[WindowRef] = []
            for control in auto.GetRootControl().GetChildren():
                try:
                    if _prop(control, "ControlTypeName") != "WindowControl":
                        continue
                    title = _prop(control, "Name")
                    if not title:
                        continue
                    handle = int(_prop(control, "NativeWindowHandle", 0) or 0)
                    if not handle:
                        continue
                    process = ""
                    if psutil is not None:
                        try:
                            process = psutil.Process(control.ProcessId).name().lower()
                        except Exception:                      # noqa: BLE001
                            process = ""
                    found.append(WindowRef(handle=str(handle), title=title,
                                           process=process, class_name=_prop(control, "ClassName"),
                                           active=str(handle) == active_handle))
                except Exception:                              # noqa: BLE001 - a window closing mid-walk
                    continue
            return tuple(found)
        return self._thread.call(work, timeout=self._policy.timeout_s + 25)

    def find_window(self, needle: str) -> WindowRef | None:
        for window in self.windows():
            if window.matches(needle):
                return window
        return None

    def _resolve(self, auto, handle: str):
        """A live control for a window handle, with its process re-checked. Runs on the desktop thread.

        Re-resolved on every operation rather than cached: Windows recycles handles, so a handle that was
        Notepad a minute ago may be something else now. The protected-process check happens here, which
        means it cannot be skipped by any caller.
        """
        try:
            wanted = int(clean_text(handle, 20))
        except (TypeError, ValueError) as exc:
            raise DesktopUnavailable("That is not a window I offered.") from exc
        try:
            import psutil
        except Exception:                                      # noqa: BLE001
            psutil = None
        for control in auto.GetRootControl().GetChildren():
            try:
                if int(_prop(control, "NativeWindowHandle", 0) or 0) != wanted:
                    continue
            except Exception:                                  # noqa: BLE001
                continue
            process = ""
            if psutil is not None:
                try:
                    process = psutil.Process(control.ProcessId).name().lower()
                except Exception:                              # noqa: BLE001
                    process = ""
            if process and process in self._protected:
                raise ProtectedWindow(
                    f"I will not automate '{process}' - it is a protected process.")
            return control, process
        raise DesktopUnavailable("That window is no longer open.")

    def activate(self, handle: str) -> WindowRef:
        def work():
            auto = self._uia()
            control, process = self._resolve(auto, handle)
            try:
                control.SetActive()
            except Exception:                                  # noqa: BLE001
                try:
                    control.SetFocus()
                except Exception as exc:                        # noqa: BLE001
                    raise DesktopUnavailable(
                        "Windows would not bring that window to the front.") from exc
            return WindowRef(handle=handle, title=_prop(control, "Name"), process=process,
                             class_name=_prop(control, "ClassName"), active=True)
        return self._thread.call(work, timeout=self._policy.timeout_s + 10)

    # -- controls -----------------------------------------------------------------------------------
    def read_window(self, handle: str) -> WindowContent:
        """Walk a window's control tree: its readable text and its actionable controls.

        Breadth-first and bounded by ``max_controls`` / ``max_depth``. Breadth-first on purpose: the
        controls a person would use are usually near the top of the tree, and a depth-first walk of a
        Chromium window spends its whole budget in one deeply nested pane.
        """
        def work():
            auto = self._uia()
            control, process = self._resolve(auto, handle)
            window = WindowRef(handle=handle, title=_prop(control, "Name"), process=process,
                               class_name=_prop(control, "ClassName"), active=True)
            elements: list[Element] = []
            texts: list[str] = []
            truncated = False
            frontier = [((index,), child)
                        for index, child in enumerate(self._children(control))]
            depth = 0
            while frontier and depth <= self._policy.max_depth:
                nxt = []
                for path, node in frontier:
                    if len(elements) >= self._policy.max_controls:
                        truncated = True
                        break
                    kind = _prop(node, "ControlTypeName")
                    name = clean_text(_prop(node, "Name"), 200)
                    if not kind:
                        continue
                    if kind in TEXT_TYPES and name:
                        texts.append(name)
                    if kind in ACTIONABLE_TYPES:
                        elements.append(self._element(node, path, kind, name))
                    if len(path) <= self._policy.max_depth:
                        for index, child in enumerate(self._children(node)):
                            nxt.append((path + (index,), child))
                if len(elements) >= self._policy.max_controls:
                    truncated = True
                    break
                frontier = nxt
                depth += 1
            return WindowContent(window=window, text="\n".join(texts[:200]),
                                 elements=tuple(elements), truncated=truncated)
        return self._thread.call(work, timeout=self._policy.timeout_s + 40)

    @staticmethod
    def _children(control):
        try:
            return control.GetChildren() or []
        except Exception:                                      # noqa: BLE001 - a node disappearing
            return []

    @staticmethod
    def _element(node, path, kind: str, name: str) -> Element:
        enabled = bool(_prop(node, "IsEnabled", True))
        visible = not bool(_prop(node, "IsOffscreen", False))
        bounds = None
        try:
            rect = node.BoundingRectangle
            bounds = (int(rect.left), int(rect.top),
                      int(rect.right - rect.left), int(rect.bottom - rect.top))
        except Exception:                                      # noqa: BLE001
            bounds = None
        value = ""
        try:
            if kind == "EditControl" and not UiaDesktop._is_password(node):
                value = clean_text(node.GetValuePattern().Value, 200)
        except Exception:                                      # noqa: BLE001
            value = ""
        # UIA's own type name, lower-cased and de-suffixed, as the role.
        role = kind[:-7].lower() if kind.endswith("Control") else kind.lower()
        return Element(role=role, name=name, handle=make_handle(path),
                       enabled=enabled, visible=visible, bounds=bounds, value=value)

    @staticmethod
    def _is_password(node) -> bool:
        """Whether a control is a password field. Never read, never filled."""
        try:
            return bool(node.IsPassword)
        except Exception:                                      # noqa: BLE001 - unknown means assume yes
            return True

    def _locate(self, auto, handle: str, control_handle: str):
        """Resolve an engine-minted control handle inside a window. Runs on the desktop thread."""
        path = parse_handle(control_handle)
        control, _process = self._resolve(auto, handle)
        node = control
        for index in path:
            children = self._children(node)
            if index >= len(children):
                raise DesktopUnavailable("That control is no longer in the window.")
            node = children[index]
        return node

    def click_control(self, handle: str, control_handle: str) -> WindowContent:
        def work():
            auto = self._uia()
            node = self._locate(auto, handle, control_handle)
            try:
                if not _prop(node, "IsEnabled", True):
                    raise DesktopUnavailable("That control is disabled.")
                node.Click(simulateMove=False, waitTime=0.05)
            except DesktopUnavailable:
                raise
            except Exception as exc:                            # noqa: BLE001
                raise DesktopUnavailable(
                    f"That control could not be clicked ({type(exc).__name__}).") from exc
            return None
        self._thread.call(work, timeout=self._policy.timeout_s + 20)
        return self.read_window(handle)

    def set_value(self, handle: str, control_handle: str, text: str) -> WindowContent:
        """Type into a field. Refuses password fields."""
        value = clean_text(text, 2000)

        def work():
            auto = self._uia()
            node = self._locate(auto, handle, control_handle)
            if self._is_password(node):
                # The only reason to fill a password field is to supply a credential, which V.O.I.D does
                # not do. Refused regardless of who asked.
                raise DesktopUnavailable("I will not type into a password field.")
            try:
                node.GetValuePattern().SetValue(value)
            except Exception:
                try:
                    node.SetFocus()
                    self._auto.SendKeys(value, waitTime=0.02)
                except Exception as exc:                        # noqa: BLE001
                    raise DesktopUnavailable(
                        f"That field could not be filled ({type(exc).__name__}).") from exc
            return None
        self._thread.call(work, timeout=self._policy.timeout_s + 20)
        return self.read_window(handle)

    def close(self) -> None:
        self._thread.stop()
        self._auto = None
