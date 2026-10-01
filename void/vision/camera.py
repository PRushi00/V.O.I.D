"""Taking one frame, and the facts that can be read from it without sending it anywhere.

Acquisition is deliberately minimal: open the device, read a frame, close the device. The camera is never
held open between captures. That costs about a second per frame (measured below) and buys the thing that
matters - the hardware indicator light goes out when V.O.I.D is not looking, which is the only
camera-activity signal the owner can trust, because the operating system drives it and V.O.I.D cannot.

Measured on this machine (ASUS FHD webcam, 2026-10-01):

    DirectShow   opens and returns a 640x480 frame in ~1.24 s
    Media Foundation   fails to open at all

So DirectShow is tried first and Media Foundation second, rather than relying on OpenCV's default order.

:func:`local_facts` is the part that needs no cloud: geometry, brightness, and whether the lens looks
covered. It cannot tell the owner *what* is in front of the camera - that needs a vision model, and this
machine has no local one (Ollama holds only a text model, and the headless OpenCV build ships no
classifiers). That gap is reported honestly by the tool layer rather than papered over with a guess.
"""
from __future__ import annotations

import logging

from void.vision import CameraDenied, CameraGate

_log = logging.getLogger(__name__)

#: How long to wait for a device to open and yield a frame before giving up, in seconds.
OPEN_TIMEOUT_S = 8.0

#: Below this mean brightness (0-255) a frame is almost certainly a covered lens or a dark room, which is
#: worth saying rather than describing as an image.
DARK_THRESHOLD = 12.0

#: Below this pixel standard deviation the frame is essentially featureless - a cover, a lens cap, or a
#: blank wall. Separate from brightness because a covered lens in a bright room is bright and blank.
FLAT_THRESHOLD = 6.0


class CameraUnavailable(RuntimeError):
    """No camera could be opened. The message says why, in words the owner can act on."""


def _cv2():
    try:
        import cv2
        return cv2
    except Exception as exc:                                    # noqa: BLE001
        raise CameraUnavailable(
            "Camera support is not installed. Install it with: pip install -r requirements-vision.txt"
        ) from exc


class Frame:
    """One captured image, in memory only.

    There is deliberately no ``save`` method and no path anywhere in this class. A frame exists for the
    length of one answer. If an owner-requested "save this photo" is ever added, it belongs in a separate,
    explicitly confirmed capability, not here.
    """

    __slots__ = ("array", "width", "height")

    def __init__(self, array, width: int, height: int):
        self.array = array
        self.width = width
        self.height = height

    def __repr__(self) -> str:                                  # no pixel data in a repr, ever
        return f"<Frame {self.width}x{self.height}>"


def devices_present() -> bool:
    """Whether this machine reports any camera hardware at all - read from the device list, not by opening.

    Used to tell "there is no camera here" from "the camera would not open", which are different answers
    and have different fixes.
    """
    from void.system import devices as device_probe
    try:
        return bool(device_probe.cameras().get("cameras"))
    except Exception:                                          # noqa: BLE001
        return False


def capture(gate: CameraGate) -> Frame:
    """Take exactly one frame, if and only if the gate permits it right now.

    The gate is checked *here*, immediately before the device is opened, rather than once per session - so
    a lapsed activation stops this frame. Every successful capture is recorded on the gate, which is what
    makes the audit trail complete by construction.
    """
    gate.check()                                                # raises CameraDenied
    cv2 = _cv2()
    policy = gate.policy
    backends = [(getattr(cv2, "CAP_DSHOW", 700), "DirectShow"),
                (getattr(cv2, "CAP_MSMF", 1400), "Media Foundation")]
    failures: list[str] = []
    for backend, label in backends:
        cap = None
        try:
            cap = cv2.VideoCapture(policy.device_index, backend)
            if not cap.isOpened():
                failures.append(f"{label} could not open the camera")
                continue
            ok, array = cap.read()
            if not ok or array is None:
                failures.append(f"{label} opened the camera but returned no image")
                continue
            frame = _shrink(cv2, array, policy.max_width)
            gate.note_capture()
            _log.info("CAMERA_CAPTURE backend=%s size=%dx%d", label, frame.width, frame.height)
            return frame
        except CameraDenied:
            raise
        except Exception as exc:                                # noqa: BLE001
            failures.append(f"{label} failed ({type(exc).__name__})")
        finally:
            if cap is not None:
                try:
                    cap.release()                               # the indicator light goes out here
                except Exception:                              # noqa: BLE001
                    pass
    if not devices_present():
        raise CameraUnavailable("This machine reports no camera.")
    raise CameraUnavailable("I could not get an image from the camera. " + "; ".join(failures) + ".")


def _shrink(cv2, array, max_width: int) -> Frame:
    """Downscale to ``max_width`` if wider. Less incidental detail, and less to send if it is ever sent."""
    height, width = array.shape[:2]
    if width > max_width > 0:
        scale = max_width / float(width)
        array = cv2.resize(array, (max_width, max(1, int(round(height * scale)))),
                           interpolation=cv2.INTER_AREA)
        height, width = array.shape[:2]
    return Frame(array, int(width), int(height))


def local_facts(frame: Frame) -> dict:
    """What can be said about a frame without sending it anywhere.

    Geometry, mean brightness, contrast, and two judgements derived from them: whether the view is dark and
    whether it is featureless. This is not scene understanding and does not pretend to be; it is what an
    honest answer can contain when no vision model is available locally.
    """
    facts: dict = {"width": frame.width, "height": frame.height}
    try:
        import numpy
        array = frame.array
        brightness = float(numpy.mean(array))
        contrast = float(numpy.std(array))
        facts["mean_brightness"] = round(brightness, 1)
        facts["contrast"] = round(contrast, 1)
        facts["looks_dark"] = brightness < DARK_THRESHOLD
        facts["looks_featureless"] = contrast < FLAT_THRESHOLD
    except Exception as exc:                                    # noqa: BLE001
        facts["analysis_unavailable"] = type(exc).__name__
    return facts


def encode_jpeg(frame: Frame, quality: int = 80) -> bytes:
    """A frame as JPEG bytes, for the one case that needs them: sending to a vision model.

    Nothing calls this unless the owner has set ``camera.allow_cloud_analysis``. It returns bytes to a
    caller; it does not write a file and it does not send anything itself, so the decision to transmit
    stays in one place, where it is reported to the owner.
    """
    cv2 = _cv2()
    quality = max(30, min(int(quality), 95))
    ok, buffer = cv2.imencode(".jpg", frame.array, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise CameraUnavailable("The captured image could not be encoded.")
    return bytes(buffer)
