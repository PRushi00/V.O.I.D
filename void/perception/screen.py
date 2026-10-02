"""Reading the screen: structured window state first, pixels only when asked for.

The capability behind "what is this on my screen?". Two sources, in the order the blueprint prefers:

1. **Window state** - the OS's own list of windows, their titles and the foreground one. Exact, ~0 ms, and
   it captures nothing. For a surprising number of questions ("what am I looking at?", "which app is in
   front?") this is the complete answer and no pixel needs to be read at all.
2. **Pixels** - a GDI capture of the screen or one window, downscaled. Only when a question genuinely
   needs what is *drawn*.

**No new dependency.** Capture uses pywin32, already required for window control, plus numpy and cv2 which
the voice and camera stacks already bring. Measured on this machine: 1707x1067 in 24 ms. `mss` and `PIL`
would both have worked; neither was needed.

**A screen is more sensitive than a webcam.** A webcam sees a room; a screen holds passwords, private
messages, documents and bank details. So screen capture gets its own switch (`screen.enabled`) and its own
egress switch (`screen.allow_cloud_analysis`), both default false and both separate from the camera's. An
owner who is happy to send a webcam frame to a cloud model has not thereby agreed to send their screen.

**Nothing is persisted.** There is no code path from a capture to a file. A frame exists in memory for one
answer. The redaction helpers below reduce what a capture contains *before* anything looks at it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from void.perception import Observation, ObservationKind, clean_text, unavailable

_log = logging.getLogger(__name__)

#: Captures are downscaled to at most this width before anything reads them. Smaller images carry less
#: incidental detail, cost less to send, and are still legible to a vision model for layout questions.
DEFAULT_MAX_WIDTH = 1280

#: A capture may never be requested larger than this, whatever a caller asks.
HARD_MAX_WIDTH = 2560

#: JPEG quality for an encoded capture. Low enough that fine print is lossy, which is a mild and welcome
#: privacy property, high enough that layout and headings read clearly.
JPEG_QUALITY = 75


class ScreenUnavailable(RuntimeError):
    """The screen could not be read. The message is safe to say out loud."""


@dataclass(frozen=True)
class ScreenPolicy:
    """What the owner's configuration permits for the SCREEN, separately from the camera."""

    enabled: bool = False
    allow_cloud_analysis: bool = False
    max_width: int = DEFAULT_MAX_WIDTH
    #: Capture only the foreground window rather than the whole desktop, when a window can be identified.
    #: Default true: the owner asking "what is this on my screen?" means the thing they are looking at,
    #: not their other monitor, their notifications and whatever else happens to be visible.
    foreground_only: bool = True

    @classmethod
    def from_config(cls, config) -> "ScreenPolicy":
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default
        width = get("screen.max_width", DEFAULT_MAX_WIDTH)
        try:
            width = max(320, min(int(width), HARD_MAX_WIDTH))
        except (TypeError, ValueError):
            width = DEFAULT_MAX_WIDTH
        return cls(enabled=bool(get("screen.enabled", False)),
                   allow_cloud_analysis=bool(get("screen.allow_cloud_analysis", False)),
                   max_width=width,
                   foreground_only=bool(get("screen.foreground_only", True)))


class Capture:
    """One screen image, in memory only.

    Like ``void.vision.camera.Frame``: no ``save``, no path, nothing that writes. A capture exists for one
    answer. ``__repr__`` never includes pixels.
    """

    __slots__ = ("array", "width", "height", "subject")

    def __init__(self, array, width: int, height: int, subject: str = ""):
        self.array = array
        self.width = width
        self.height = height
        self.subject = subject

    def __repr__(self) -> str:
        return f"<Capture {self.width}x{self.height}>"


def _win32():
    try:
        import win32api
        import win32con
        import win32gui
        import win32ui
        return win32api, win32con, win32gui, win32ui
    except Exception as exc:                                    # noqa: BLE001
        raise ScreenUnavailable("Screen reading needs pywin32, which is not available here.") from exc


# --- source 1: structured window state (free, exact, captures nothing) ----------------------------

def window_state() -> Observation:
    """What windows are open and which is in front, from the OS. No pixels read.

    The cheapest useful answer to "what am I looking at?" and the one to try first. Titles are UNTRUSTED -
    a window title is whatever the program chose to display - which is why they go through ``clean_text``
    and are labelled as untrusted wherever they surface.
    """
    try:
        _api, _con, gui, _ui = _win32()
    except ScreenUnavailable as exc:
        return unavailable(ObservationKind.WINDOW_STATE, str(exc), provenance="win32")
    from void.perception import Element
    windows: list[Element] = []
    foreground_title = ""
    try:
        foreground = gui.GetForegroundWindow()
    except Exception:                                          # noqa: BLE001
        foreground = None

    def collect(hwnd, _unused):
        try:
            if not gui.IsWindowVisible(hwnd):
                return True
            title = gui.GetWindowText(hwnd)
            if not title:
                return True
            try:
                left, top, right, bottom = gui.GetWindowRect(hwnd)
                bounds = (left, top, right - left, bottom - top)
            except Exception:                                  # noqa: BLE001
                bounds = None
            windows.append(Element(role="window", name=title, handle=str(int(hwnd)),
                                   bounds=bounds, visible=True))
        except Exception:                                      # noqa: BLE001
            pass
        return True

    try:
        gui.EnumWindows(collect, None)
    except Exception as exc:                                    # noqa: BLE001
        return unavailable(ObservationKind.WINDOW_STATE,
                           f"the window list could not be read ({type(exc).__name__})",
                           provenance="win32")
    if foreground:
        for element in windows:
            if element.handle == str(int(foreground)):
                foreground_title = element.name
                break
    return Observation(kind=ObservationKind.WINDOW_STATE,
                       subject=foreground_title or "(no foreground window)",
                       text="\n".join(element.name for element in windows[:40]),
                       elements=tuple(windows), confidence=1.0, provenance="win32")


def foreground_window() -> tuple[int, str, tuple[int, int, int, int]] | None:
    """``(hwnd, title, (x, y, w, h))`` for the window in front, or None."""
    try:
        _api, _con, gui, _ui = _win32()
        hwnd = gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = clean_text(gui.GetWindowText(hwnd), 200)
        left, top, right, bottom = gui.GetWindowRect(hwnd)
        width, height = max(1, right - left), max(1, bottom - top)
        return int(hwnd), title, (left, top, width, height)
    except Exception:                                          # noqa: BLE001
        return None


# --- source 2: pixels (only when a question needs what is drawn) ----------------------------------

def capture(*, max_width: int = DEFAULT_MAX_WIDTH, foreground_only: bool = True) -> Capture:
    """Capture the foreground window, or the whole virtual desktop.

    Raises :class:`ScreenUnavailable` rather than returning something empty, so a caller cannot mistake a
    failed capture for a blank screen.

    ``foreground_only`` narrows what is read to the window the owner is actually looking at. That is both
    the better answer to "what is this on my screen?" and the smaller privacy footprint - a second monitor
    showing something private is not captured at all.
    """
    api, con, gui, ui = _win32()
    try:
        import numpy
    except Exception as exc:                                    # noqa: BLE001
        raise ScreenUnavailable("Screen reading needs numpy, which is not available here.") from exc

    subject = ""
    region = None
    if foreground_only:
        found = foreground_window()
        if found is not None:
            _hwnd, subject, region = found
    if region is None:
        left = api.GetSystemMetrics(con.SM_XVIRTUALSCREEN)
        top = api.GetSystemMetrics(con.SM_YVIRTUALSCREEN)
        width = api.GetSystemMetrics(con.SM_CXVIRTUALSCREEN)
        height = api.GetSystemMetrics(con.SM_CYVIRTUALSCREEN)
        region = (left, top, max(1, width), max(1, height))
        subject = subject or "(whole screen)"
    left, top, width, height = region
    # Clamp: a minimised or off-screen window can report absurd geometry, and a huge BitBlt is a
    # denial-of-service against ourselves.
    width = max(1, min(int(width), HARD_MAX_WIDTH * 2))
    height = max(1, min(int(height), HARD_MAX_WIDTH * 2))

    desktop = gui.GetDesktopWindow()
    source_dc = handle_dc = mem_dc = bitmap = None
    try:
        source_dc = gui.GetWindowDC(desktop)
        handle_dc = ui.CreateDCFromHandle(source_dc)
        mem_dc = handle_dc.CreateCompatibleDC()
        bitmap = ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(handle_dc, width, height)
        mem_dc.SelectObject(bitmap)
        mem_dc.BitBlt((0, 0), (width, height), handle_dc, (left, top), con.SRCCOPY)
        raw = bitmap.GetBitmapBits(True)
        array = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(height, width, 4)[:, :, :3]
        array = array.copy()                                    # detach from the DC's buffer
    except Exception as exc:                                    # noqa: BLE001
        raise ScreenUnavailable(
            f"The screen could not be captured ({type(exc).__name__}).") from exc
    finally:
        # Every GDI object is released on every path. Leaking a DC or a bitmap exhausts a per-process
        # handle quota and eventually makes the desktop itself misbehave.
        for release in (
                lambda: mem_dc and mem_dc.DeleteDC(),
                lambda: handle_dc and handle_dc.DeleteDC(),
                lambda: source_dc and gui.ReleaseDC(desktop, source_dc),
                lambda: bitmap and gui.DeleteObject(bitmap.GetHandle())):
            try:
                release()
            except Exception:                                  # noqa: BLE001
                pass
    return _shrink(array, max_width, subject)


def _shrink(array, max_width: int, subject: str) -> Capture:
    """Downscale to ``max_width``. Less incidental detail, less to send, still legible for layout."""
    height, width = array.shape[:2]
    target = max(320, min(int(max_width or DEFAULT_MAX_WIDTH), HARD_MAX_WIDTH))
    if width > target:
        try:
            import cv2
            scale = target / float(width)
            array = cv2.resize(array, (target, max(1, int(round(height * scale)))),
                               interpolation=cv2.INTER_AREA)
            height, width = array.shape[:2]
        except Exception:                                      # noqa: BLE001 - full size is still usable
            pass
    return Capture(array, int(width), int(height), clean_text(subject, 200))


def encode_jpeg(image: Capture, quality: int = JPEG_QUALITY) -> bytes:
    """A capture as JPEG bytes, for the one case that needs them: sending to a vision model.

    Returns bytes to a caller. It does not write a file and does not send anything, so the decision to
    transmit stays in one place where it is gated and reported.
    """
    try:
        import cv2
    except Exception as exc:                                    # noqa: BLE001
        raise ScreenUnavailable("Encoding a screen capture needs OpenCV, which is not installed.") from exc
    ok, buffer = cv2.imencode(".jpg", image.array,
                              [int(cv2.IMWRITE_JPEG_QUALITY), max(30, min(int(quality), 90))])
    if not ok:
        raise ScreenUnavailable("The screen capture could not be encoded.")
    return bytes(buffer)


def local_facts(image: Capture) -> dict:
    """What can be said about a capture without sending it anywhere.

    Geometry and light, the same honest minimum the camera path reports. It is not scene understanding and
    does not pretend to be - but it does answer "is the screen blank/asleep?" without any egress at all.
    """
    facts: dict = {"width": image.width, "height": image.height, "subject": image.subject}
    try:
        import numpy
        facts["mean_brightness"] = round(float(numpy.mean(image.array)), 1)
        facts["contrast"] = round(float(numpy.std(image.array)), 1)
        facts["looks_blank"] = facts["contrast"] < 5.0
    except Exception as exc:                                    # noqa: BLE001
        facts["analysis_unavailable"] = type(exc).__name__
    return facts


def screenshot_observation(image: Capture) -> Observation:
    """A capture as an Observation, carrying facts rather than pixels.

    The pixels stay in the ``Capture``; the Observation describes it. That separation is deliberate - an
    Observation is the thing that gets logged, summarised and handed around, and it must not be a carrier
    for image data.
    """
    facts = local_facts(image)
    blank = " The screen appears blank." if facts.get("looks_blank") else ""
    return Observation(
        kind=ObservationKind.SCREENSHOT,
        subject=image.subject,
        text=f"A {image.width}x{image.height} capture of {image.subject or 'the screen'}.{blank}",
        confidence=1.0, provenance="gdi")
