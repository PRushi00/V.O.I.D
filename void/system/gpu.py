"""Graphics-processor observation, for machines that have a readable one.

V.O.I.D cares about the GPU for a concrete reason: speech-to-text runs on it (``voice.stt_device: cuda``),
so "why was that transcription slow?" and "why is my laptop slow?" both have a real answer that lives here.

This is the only probe in the package that runs another program, so it is the one with the strictest rules:

* The argument list is the frozen module constant :data:`_QUERY_ARGV`. It is passed to
  :func:`subprocess.run` as a *list* with ``shell=False``, so there is no command string for anything to
  be injected into - and no caller of this module passes an argument at all.
* The call is bounded by :data:`~void.system.PROBE_TIMEOUT_S` and killed on expiry.
* Output is parsed defensively: a field that is not a number stays absent rather than becoming zero.
* Absence is normal. An AMD or Intel machine, or one with no driver, simply has no reading - that is
  recorded as unavailable, not as an error the owner needs to hear about.

Vendor support is deliberately narrow (NVIDIA only). Covering every vendor would mean either a new
dependency or several more subprocesses, and the one GPU V.O.I.D actually uses for inference on this
machine is reachable through the tool the driver already installs.
"""
from __future__ import annotations

import subprocess

from void.system import PROBE_TIMEOUT_S, Reading, describe_error, timed

#: The frozen, complete argument list. Never built from a caller's input, never joined into a string.
_QUERY_ARGV: tuple[str, ...] = (
    "nvidia-smi",
    "--query-gpu=name,utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
)

#: Column order in :data:`_QUERY_ARGV`, as (key, converter).
_COLUMNS: tuple[tuple[str, type], ...] = (
    ("gpu_name", str),
    ("gpu_utilisation_percent", float),
    ("gpu_memory_utilisation_percent", float),
    ("gpu_memory_used_mb", float),
    ("gpu_memory_total_mb", float),
    ("gpu_temperature_celsius", float),
)

#: A machine can hold several cards; only this many are described.
MAX_GPUS = 4


def _number(text: str) -> float | None:
    """A float, or None. nvidia-smi prints ``[N/A]`` for a metric a card does not report."""
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None


def _run() -> tuple[str | None, str | None]:
    """``(stdout, failure_reason)``. Exactly one of the two is set."""
    try:
        proc = subprocess.run(_QUERY_ARGV, capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_S, shell=False, check=False)
    except FileNotFoundError:
        return None, "no NVIDIA driver tooling is installed on this machine"
    except subprocess.TimeoutExpired:
        return None, f"the graphics driver did not answer within {PROBE_TIMEOUT_S:.0f}s"
    except Exception as exc:                                    # noqa: BLE001
        return None, describe_error(exc)
    if proc.returncode != 0:
        return None, "the graphics driver reported no usable device"
    return proc.stdout or "", None


def graphics() -> Reading:
    """Per-card utilisation, memory and temperature, plus the first card promoted to top level.

    The first card's figures are also published unprefixed (``gpu_utilisation_percent`` and friends) so
    the common single-GPU case reads naturally in a status answer and in :func:`void.system.host.pressure`,
    without a caller having to index into a list.
    """
    with timed() as r:
        stdout, reason = _run()
        if stdout is None:
            return r.miss("gpu", reason or "unavailable")
        cards: list[dict] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            fields = [part.strip() for part in line.split(",")]
            if len(fields) < len(_COLUMNS):
                continue
            card: dict = {}
            for (key, kind), raw in zip(_COLUMNS, fields):
                value = raw if kind is str else _number(raw)
                if kind is str:
                    value = value or None
                if value is not None:
                    card[key] = round(value, 1) if kind is float else value
            if card:
                cards.append(card)
            if len(cards) >= MAX_GPUS:
                break
        if not cards:
            return r.miss("gpu", "the graphics driver returned nothing readable")
        r.set("gpus", cards)
        for key, value in cards[0].items():
            r.set(key, value)
        return r
