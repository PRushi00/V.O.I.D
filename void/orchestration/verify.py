"""Did the intended state actually happen?

The assumption this layer exists to break is ``action succeeded == task succeeded``. A tool returning ok
means the call did not raise - not that the application is in front of the owner, not that the file is
readable, not that the right chat is open. V2 reports tool outcomes faithfully; V3 has to be able to check
the *world*, because an autonomous agent that cannot tell success from apparent success will confidently
build on a broken step.

Two rules shape the design.

**Structured first, pixels last - and often not at all.** A check reads the window list, or asks the
filesystem whether a file exists and has a plausible size. It does not screenshot the desktop to find out
whether Notepad opened. Screenshots are expensive, they capture whatever else is on the owner's screen, and
they answer less reliably than the structured state that was already available. ``VerificationMethod``
records which way an answer came, so "how do you know?" is answerable.

**Honest when it cannot check.** A verifier that returns "probably fine" when it has no evidence is worse
than one that admits it. :attr:`Verdict.UNVERIFIED` is a first-class outcome and is deliberately distinct
from :attr:`Verdict.FAILED`: "I could not confirm this" and "this did not work" lead to different
decisions - the first may be acceptable, the second triggers replanning.

Verification is an observation, never an authorization. A PASSED verdict does not entitle the next step to
run; that step is authorized when it runs, by its own risk level and RiskGate, exactly as before.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

#: How much of any detail line is kept. Verification detail reaches logs and a model's context.
MAX_DETAIL = 200

#: A file that exists but is this small is almost certainly not a real document, so a "created a PDF" claim
#: with a 12-byte file is reported as suspect rather than passed.
MIN_PLAUSIBLE_ARTIFACT_BYTES = 64


class Verdict:
    """The outcome of a check."""

    PASSED = "passed"
    FAILED = "failed"
    #: No evidence either way. NOT a failure - see the module docstring.
    UNVERIFIED = "unverified"

    ALL = frozenset({PASSED, FAILED, UNVERIFIED})


class VerificationMethod:
    """How an answer was obtained, cheapest and most reliable first.

    Recorded on every result so the provenance of a claim is inspectable, and so it is visible when
    something expensive was used where something structured would have done.
    """

    WINDOW_STATE = "window_state"        # the OS window list - cheap, structured, reliable
    FILESYSTEM = "filesystem"            # the file exists, with a plausible size
    PROCESS_STATE = "process_state"      # the process is running
    BROWSER_STATE = "browser_state"      # the browser's own tab/DOM state
    TOOL_RESULT = "tool_result"          # only what the tool reported; weak, and labelled as such
    NONE = "none"                        # nothing could be checked

    ALL = frozenset({WINDOW_STATE, FILESYSTEM, PROCESS_STATE, BROWSER_STATE, TOOL_RESULT, NONE})


def _short(text: object, limit: int = MAX_DETAIL) -> str:
    if text is None:
        return ""
    return " ".join(str(text).split())[:limit]


@dataclass(frozen=True)
class VerificationResult:
    """What was checked, how, and what was found."""

    verdict: str
    method: str
    #: What was being checked, in words safe to speak.
    expectation: str = ""
    detail: str = ""
    at: float = field(default_factory=time.time)
    duration_s: float = 0.0

    def __post_init__(self) -> None:
        if self.verdict not in Verdict.ALL:
            raise ValueError(f"unknown verdict: {self.verdict!r}")
        if self.method not in VerificationMethod.ALL:
            raise ValueError(f"unknown verification method: {self.method!r}")
        object.__setattr__(self, "detail", _short(self.detail))
        object.__setattr__(self, "expectation", _short(self.expectation))

    @property
    def ok(self) -> bool:
        """True only for a positive confirmation. UNVERIFIED is not ok, and is not a failure either."""
        return self.verdict == Verdict.PASSED

    @property
    def failed(self) -> bool:
        """True only when there is positive evidence the intended state did NOT happen."""
        return self.verdict == Verdict.FAILED

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "method": self.method, "expectation": self.expectation,
                "detail": self.detail, "duration_s": round(self.duration_s, 3)}


class Verifier:
    """Capability-aware checks over observed state.

    Every observation source is injected, so this module holds no capability of its own and is testable
    without a desktop. In the running system these are bound to the existing V2 tools - the window list
    from ``ComputerActions``, process names from the observation layer, file facts from the confined
    ``FileActions`` - which is why verification cannot see anything the owner has not already allowed
    V.O.I.D to see. A verifier is not a way around confinement.

    ``list_windows`` returns ``[{"title": str, "app": str}, ...]``;
    ``running_processes`` returns a set of lower-cased names;
    ``file_facts`` returns ``{"exists": bool, "size_bytes": int}`` or None when the path is not readable.
    """

    def __init__(self, *, list_windows: Callable[[], list[dict]] | None = None,
                 running_processes: Callable[[], set[str]] | None = None,
                 file_facts: Callable[[str], dict | None] | None = None,
                 active_window: Callable[[], dict | None] | None = None):
        self._list_windows = list_windows
        self._running = running_processes
        self._file_facts = file_facts
        self._active_window = active_window

    # -- application / window state --
    def application_present(self, name: str) -> VerificationResult:
        """Is an application actually there, by window title, window app name, or process name?

        Three structured sources, tried cheapest first. The window list is preferred over the process list
        because "Notepad is running with no window" is not what "open Notepad" meant.
        """
        started = time.perf_counter()
        needle = (name or "").strip().lower()
        expectation = f"{name} is open"
        if not needle:
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE,
                                      detail="no application was named")
        if self._list_windows is not None:
            try:
                windows = self._list_windows() or []
            except Exception as exc:                            # noqa: BLE001
                windows = []
                detail = f"the window list could not be read ({type(exc).__name__})"
            else:
                detail = ""
            for window in windows:
                haystack = f"{window.get('title', '')} {window.get('app', '')}".lower()
                if needle in haystack:
                    return VerificationResult(
                        Verdict.PASSED, VerificationMethod.WINDOW_STATE, expectation,
                        detail=f"a window belonging to {window.get('app') or name} is open",
                        duration_s=time.perf_counter() - started)
            if windows:
                # A populated window list that does not contain it is positive evidence of absence.
                return VerificationResult(
                    Verdict.FAILED, VerificationMethod.WINDOW_STATE, expectation,
                    detail=f"no open window matches {name}",
                    duration_s=time.perf_counter() - started)
        if self._running is not None:
            try:
                processes = self._running() or set()
            except Exception as exc:                            # noqa: BLE001
                processes = set()
            if any(needle in process for process in processes):
                return VerificationResult(
                    Verdict.PASSED, VerificationMethod.PROCESS_STATE, expectation,
                    detail=f"a process matching {name} is running",
                    duration_s=time.perf_counter() - started)
            if processes:
                return VerificationResult(
                    Verdict.FAILED, VerificationMethod.PROCESS_STATE, expectation,
                    detail=f"no running process matches {name}",
                    duration_s=time.perf_counter() - started)
        return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                  detail="no way to observe application state was available",
                                  duration_s=time.perf_counter() - started)

    def application_focused(self, name: str) -> VerificationResult:
        """Is it actually in front of the owner? Stronger than "present", and what "open X" usually means."""
        started = time.perf_counter()
        needle = (name or "").strip().lower()
        expectation = f"{name} is in the foreground"
        if not needle or self._active_window is None:
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                      detail="the foreground window could not be read")
        try:
            window = self._active_window()
        except Exception as exc:                                # noqa: BLE001
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                      detail=f"the foreground window could not be read "
                                             f"({type(exc).__name__})")
        if not window:
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                      detail="nothing reported as foreground")
        haystack = f"{window.get('title', '')} {window.get('app', '')}".lower()
        passed = needle in haystack
        return VerificationResult(
            Verdict.PASSED if passed else Verdict.FAILED, VerificationMethod.WINDOW_STATE, expectation,
            detail=(f"{window.get('app') or 'it'} is in front" if passed
                    else f"the foreground window is {window.get('app') or 'something else'}"),
            duration_s=time.perf_counter() - started)

    # -- artifacts --
    def artifact_created(self, path: str, *, min_bytes: int = MIN_PLAUSIBLE_ARTIFACT_BYTES
                         ) -> VerificationResult:
        """Does the file exist, and is it big enough to plausibly be the thing that was asked for?

        The size floor matters: a generator that writes a valid-but-empty document "succeeds" at the tool
        level and produces something useless. A file under the floor is reported FAILED with its size, not
        passed because it exists.

        This checks existence and size, not renderability - opening a PDF to confirm it draws is a separate,
        heavier capability, and claiming it here would overstate what was done.
        """
        started = time.perf_counter()
        expectation = f"{path} was created"
        if not path or self._file_facts is None:
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                      detail="no way to inspect the filesystem was available")
        try:
            facts = self._file_facts(path)
        except Exception as exc:                                # noqa: BLE001
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                      detail=f"the file could not be inspected ({type(exc).__name__})")
        if facts is None:
            # Not readable: most often outside the allowed roots. Honest UNVERIFIED, not a failure claim.
            return VerificationResult(Verdict.UNVERIFIED, VerificationMethod.NONE, expectation,
                                      detail="that location is not one V.O.I.D can inspect",
                                      duration_s=time.perf_counter() - started)
        if not facts.get("exists"):
            return VerificationResult(Verdict.FAILED, VerificationMethod.FILESYSTEM, expectation,
                                      detail="no file exists at that path",
                                      duration_s=time.perf_counter() - started)
        size = int(facts.get("size_bytes") or 0)
        if size < max(0, int(min_bytes)):
            return VerificationResult(
                Verdict.FAILED, VerificationMethod.FILESYSTEM, expectation,
                detail=f"the file exists but is only {size} bytes, which is too small to be usable",
                duration_s=time.perf_counter() - started)
        return VerificationResult(Verdict.PASSED, VerificationMethod.FILESYSTEM, expectation,
                                  detail=f"the file exists and is {size} bytes",
                                  duration_s=time.perf_counter() - started)

    # -- the weak fallback, labelled as weak --
    @staticmethod
    def from_tool_result(ok: bool, summary: str = "") -> VerificationResult:
        """Only what the tool said. Recorded with ``TOOL_RESULT`` so its weakness is visible.

        Used when nothing structured can confirm the outcome. A successful tool call is evidence, just not
        evidence about the world - so a True here is PASSED but provenance-marked, and a caller deciding
        whether to build on it can see what it rests on.
        """
        return VerificationResult(Verdict.PASSED if ok else Verdict.FAILED,
                                  VerificationMethod.TOOL_RESULT,
                                  expectation="the action reported success",
                                  detail=_short(summary))


def summarise(results: list[VerificationResult]) -> str:
    """One line for a concise spoken answer, honest about what was not checked."""
    if not results:
        return "nothing was verified"
    passed = sum(1 for result in results if result.ok)
    failed = sum(1 for result in results if result.failed)
    unverified = sum(1 for result in results
                     if result.verdict == Verdict.UNVERIFIED)
    parts = []
    if passed:
        parts.append(f"{passed} confirmed")
    if failed:
        parts.append(f"{failed} did not happen")
    if unverified:
        parts.append(f"{unverified} could not be checked")
    return ", ".join(parts)
