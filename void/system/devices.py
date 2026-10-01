"""What is attached to this machine: speakers, microphones, cameras, Bluetooth peers, USB hardware.

This is the observation half of connectivity. It answers "is my headset connected?", "does this machine
have a camera?", "is my phone paired?" - by reading, never by connecting. Nothing here pairs, unpairs,
trusts, opens or configures a device; the only thing V.O.I.D does with a device *name* is compare it to
what the owner asked about.

Two sources, each chosen because it is already present:

**sounddevice** (a declared voice dependency) enumerates audio endpoints as the audio stack sees them, so
a USB headset appears exactly as it will when the microphone is opened - the same view the voice pipeline
gets. It reports host APIs; Windows exposes the same physical device through several (MME, WASAPI, ...), so
:func:`audio` deduplicates by name and keeps the preferred one.

**WMI** (through pywin32, already a dependency) enumerates Plug-and-Play hardware for cameras, Bluetooth
and USB, from the closed query table in :mod:`void.system.wmi`. No shell, no new dependency, and a
caller's string never reaches a query - see that module for why that is structural rather than escaped.

A connected device's *name* is attacker-influenced data. A Bluetooth peer broadcasts whatever name it
likes, and that name reaches the owner and, potentially, a model's context. Names are therefore length-
capped and stripped of control characters here, at the source, before anything downstream sees them.
"""
from __future__ import annotations

import re
import unicodedata

from void.system import Reading, describe_error, timed
from void.system import wmi

#: How many devices of one kind are reported. A workstation can present 200 USB nodes.
MAX_DEVICES = 40

#: A device name is truncated to this. Bluetooth peers choose their own names and a long one is either a
#: mistake or an attempt to flood whatever is reading it.
MAX_NAME = 80

#: Control characters, and the bidirectional-override characters that can make text display as something
#: other than what it is. Removed from every device name before it leaves this module.
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")

#: Host APIs Windows exposes the same audio hardware through, best first. WASAPI is what the modern audio
#: stack uses; MME is the legacy view and truncates device names to 31 characters.
_HOSTAPI_RANK = ("Windows WASAPI", "Windows DirectSound", "Windows WDM-KS", "MME")

#: Words in an audio endpoint name that suggest it is a headset rather than built-in hardware. Used only to
#: *describe* a device, never to decide anything.
_HEADSET_HINTS = ("headset", "headphone", "earphone", "earbud", "airpod", "buds", "hands-free")

#: PnP ``Status`` values that mean the device is present and working. Windows uses "OK"; anything else
#: (``Error``, ``Degraded``, ``Unknown``) is reported verbatim rather than collapsed into "connected".
_STATUS_OK = "OK"


def clean_name(value: object) -> str | None:
    """A device name safe to show the owner, put in a log, or send to a model.

    Strips control and direction-override characters, collapses whitespace, normalises to NFC and truncates.
    Returns None when nothing usable is left.
    """
    if value is None:
        return None
    try:
        text = unicodedata.normalize("NFC", str(value))
    except Exception:                                          # noqa: BLE001
        return None
    text = " ".join(_UNSAFE_NAME.sub("", text).split())
    if not text:
        return None
    return text[:MAX_NAME]


def _sounddevice():
    try:
        import sounddevice
        return sounddevice
    except Exception:                                          # noqa: BLE001 - optional voice dependency
        return None


def _hostapi_rank(name: str | None) -> int:
    try:
        return _HOSTAPI_RANK.index(name or "")
    except ValueError:
        return len(_HOSTAPI_RANK)


def audio() -> Reading:
    """Audio inputs and outputs as the audio stack sees them, with the defaults marked.

    Returns ``audio_inputs`` and ``audio_outputs``, each a list of ``{name, channels, is_default,
    looks_like_headset, host_api}``. Deduplicated by name across host APIs.
    """
    with timed() as r:
        sd = _sounddevice()
        if sd is None:
            return r.miss("audio_devices", "the sounddevice library is not installed")
        try:
            devices = list(sd.query_devices())
            hostapis = [api.get("name") for api in sd.query_hostapis()]
        except Exception as exc:                                # noqa: BLE001 - no audio backend at all
            return r.miss("audio_devices", describe_error(exc))
        try:
            default_in, default_out = sd.default.device
        except Exception:                                      # noqa: BLE001
            default_in = default_out = None
        inputs: dict[str, dict] = {}
        outputs: dict[str, dict] = {}
        for index, device in enumerate(devices):
            name = clean_name(device.get("name"))
            if not name:
                continue
            api_index = device.get("hostapi")
            api = hostapis[api_index] if isinstance(api_index, int) and api_index < len(hostapis) else None
            low = name.lower()
            for channels_key, bucket, default_index in (("max_input_channels", inputs, default_in),
                                                        ("max_output_channels", outputs, default_out)):
                channels = device.get(channels_key) or 0
                if channels <= 0:
                    continue
                row = {"name": name,
                       "channels": int(channels),
                       "is_default": index == default_index,
                       "looks_like_headset": any(hint in low for hint in _HEADSET_HINTS),
                       "host_api": api}
                existing = bucket.get(name)
                # Same physical device seen through several host APIs: keep the better view, but never lose
                # the fact that one of them is the system default.
                if existing is None or _hostapi_rank(api) < _hostapi_rank(existing.get("host_api")):
                    if existing is not None and existing.get("is_default"):
                        row["is_default"] = True
                    bucket[name] = row
                elif row["is_default"]:
                    existing["is_default"] = True
        r.set("audio_inputs", list(inputs.values())[:MAX_DEVICES])
        r.set("audio_outputs", list(outputs.values())[:MAX_DEVICES])
        return r


def _pnp(kind: str, query_name: str, label: str) -> Reading:
    """PnP hardware of one class, from the closed WMI query table."""
    with timed() as r:
        try:
            rows = wmi.query(query_name)
        except wmi.WmiUnavailable as exc:
            return r.miss(label, f"Windows device information is unreadable ({exc})")
        except Exception as exc:                                # noqa: BLE001 - no pywin32, not Windows
            return r.miss(label, describe_error(exc))
        out: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            name = clean_name(row.get("Name"))
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            status = wmi.text(row.get("Status"))
            out.append({"name": name,
                        "kind": kind,
                        "status": status,
                        "working": status == _STATUS_OK,
                        "category": wmi.text(row.get("PNPClass"))})
            if len(out) >= MAX_DEVICES:
                break
        r.set(label, out)
        return r


def cameras() -> Reading:
    """Camera and imaging hardware present on this machine.

    This reports that a camera *exists*; it does not open one. Acquiring an image is a separate,
    owner-gated capability (:mod:`void.actions.vision`), and this probe is how that capability learns
    whether there is anything to acquire from.
    """
    return _pnp("camera", "cameras", "cameras")


def bluetooth() -> Reading:
    """Bluetooth radios, services and paired peers that Windows currently reports as present.

    Windows exposes Bluetooth *profiles* as devices too ("Phonebook Access Pse Service"), so the list
    mixes peers with services. :func:`bluetooth_peers` filters it down to things that look like actual
    peripherals, which is what "is my headset paired?" is about.
    """
    return _pnp("bluetooth", "bluetooth", "bluetooth")


#: Bluetooth rows that are Windows' own stack or a profile, not a peer. Matched case-insensitively as
#: substrings, so a peer named "Mouse" is kept while "Personal Area Network Service" is not.
_BLUETOOTH_PLUMBING = ("service", "enumerator", "protocol tdi", "generic attribute", "generic access",
                       "profile", "avrcp transport", "bluetooth device", "rfcomm")


def bluetooth_peers() -> Reading:
    """Bluetooth entries that look like real peripherals, with the stack's own plumbing filtered out."""
    with timed() as r:
        probe = bluetooth()
        if not probe.ok:
            return r.merge(probe)
        peers = [row for row in probe.get("bluetooth") or []
                 if not any(word in row["name"].lower() for word in _BLUETOOTH_PLUMBING)]
        r.set("bluetooth_peers", peers)
        return r


def usb() -> Reading:
    """USB controllers and hubs Windows reports. Peripheral function devices appear under their own class."""
    return _pnp("usb", "usb", "usb")


def snapshot() -> Reading:
    """Everything attached, in one reading: audio in/out, cameras, Bluetooth peers, USB.

    One failing source does not take the others down; it lands in ``unavailable`` so the gap is visible.
    """
    with timed() as r:
        for probe in (audio, cameras, bluetooth_peers, usb):
            try:
                r.merge(probe())
            except Exception as exc:                            # noqa: BLE001
                r.miss(probe.__name__, describe_error(exc))
        return r


def _tokens(text: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9]+", text.lower()) if word}


#: Words that name a *kind* of device rather than a particular one, mapped to what counts as a match.
#:
#: This exists because of a real failure. Asking "is my headset connected?" found nothing, while the
#: machine was reporting two endpoints flagged ``looks_like_headset`` - because no device is literally
#: *named* "headset". People ask for device categories, and Windows names hardware after its chipset, so a
#: name-substring search alone answers the wrong question. ``kinds`` restricts which lists are searched,
#: and ``flag`` requires that marker on the row.
_CATEGORY_WORDS: dict[str, dict] = {
    "headset": {"kinds": ("audio_input", "audio_output"), "flag": "looks_like_headset"},
    "headsets": {"kinds": ("audio_input", "audio_output"), "flag": "looks_like_headset"},
    "headphone": {"kinds": ("audio_output",), "flag": "looks_like_headset"},
    "headphones": {"kinds": ("audio_output",), "flag": "looks_like_headset"},
    "earphone": {"kinds": ("audio_input", "audio_output"), "flag": "looks_like_headset"},
    "earphones": {"kinds": ("audio_input", "audio_output"), "flag": "looks_like_headset"},
    "earbud": {"kinds": ("audio_input", "audio_output"), "flag": "looks_like_headset"},
    "earbuds": {"kinds": ("audio_input", "audio_output"), "flag": "looks_like_headset"},
    "mic": {"kinds": ("audio_input",)},
    "microphone": {"kinds": ("audio_input",)},
    "microphones": {"kinds": ("audio_input",)},
    "speaker": {"kinds": ("audio_output",)},
    "speakers": {"kinds": ("audio_output",)},
    "camera": {"kinds": ("camera",)},
    "cameras": {"kinds": ("camera",)},
    "webcam": {"kinds": ("camera",)},
    "bluetooth": {"kinds": ("bluetooth",)},
    "usb": {"kinds": ("usb",)},
}


def _partial(wanted: set[str], found: set[str]) -> bool:
    """Whether a name shares enough words with the query to be worth offering.

    One shared word is enough for a one-word query, but not for a longer one: asking for "ASUS MD100 Mouse"
    on a machine full of ASUS hardware matched both cameras on the brand alone, which buries the device the
    owner actually named. A multi-word query needs at least two of its words.
    """
    if not wanted or not found:
        return False
    shared = wanted & found
    return bool(shared) if len(wanted) == 1 else len(shared) >= 2


def _category(query_low: str) -> dict | None:
    """The category rule for a query that names a kind of device, or None.

    Matches the whole query ("headset") or its last word ("my bluetooth headset"), so a qualifier does not
    defeat it - but never a word buried in the middle, which would make "headset cable adapter" a headset.
    """
    rule = _CATEGORY_WORDS.get(query_low)
    if rule is not None:
        return rule
    words = query_low.split()
    return _CATEGORY_WORDS.get(words[-1]) if words else None


def find(query: str, reading: Reading | None = None) -> list[dict]:
    """Devices matching ``query``, best first - by name, or by the kind of device it names.

    Matching happens here in Python over names already read from the OS; a caller's string never reaches
    WMI or the audio stack. Ranking is deliberately simple: an exact name, then a substring, then all the
    query's words, then any of them. A query that names a *category* instead ("headset", "microphone",
    "webcam") is answered from :data:`_CATEGORY_WORDS` - see there for why. Every hit carries the kind it
    came from, so "is my headset connected?" answers with what was actually found rather than with a yes.
    """
    wanted = clean_name(query)
    if not wanted:
        return []
    low = wanted.lower()
    words = _tokens(low)
    reading = reading if reading is not None else snapshot()
    rule = _category(low)
    rows: list[tuple[int, dict]] = []
    for key, kind in (("audio_inputs", "audio_input"), ("audio_outputs", "audio_output"),
                      ("cameras", "camera"), ("bluetooth_peers", "bluetooth"), ("usb", "usb")):
        for device in reading.get(key) or []:
            name = device.get("name") or ""
            name_low = name.lower()
            if name_low == low:
                rank = 0
            elif low in name_low:
                rank = 1
            elif words and words <= _tokens(name_low):
                rank = 2
            elif rule is not None and kind in rule["kinds"] and (
                    "flag" not in rule or device.get(rule["flag"])):
                # A category hit ranks below every name hit: when the owner names a device, that device
                # wins over everything merely of the same kind.
                rank = 4
            elif _partial(words, _tokens(name_low)):
                rank = 3
            else:
                continue
            row = dict(device)
            row["kind"] = device.get("kind") or kind
            row["matched_category"] = rank == 4
            rows.append((rank, row))
    rows.sort(key=lambda pair: pair[0])
    out: list[dict] = []
    seen: set[tuple] = set()
    for _rank, row in rows:
        key = (row.get("kind"), (row.get("name") or "").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out[:MAX_DEVICES]
