"""What V.O.I.D's voice launcher tells Windows when it declines to start.

Windows Task Scheduler records only one number: the process exit code, shown as "Last Result". When every
refusal returned ``1``, that column said "it failed" and nothing else - the owner's scheduled task showed
``Last Result: 1`` for days while the actual cause was an engaged stop, which is a deliberate safety
control doing its job rather than a fault. The log held the answer, but the surface Windows puts in front
of you did not.

So each reason gets its own number. The point is diagnosis, not control flow: the codes are stable, few,
and documented here so that reading "Last Result" is enough to know what to do next.

    0  started normally, or another instance already owns the runtime (nothing to fix)
    1  unexpected failure - see ~/.void/void.log
    2  voice is switched off in configuration
    3  a stop is engaged; clear it with `python -m void clear-stop`
    4  the optional voice dependencies are not installed

:data:`ALREADY_RUNNING` is deliberately ``0``. A second launch finding the first one healthy is the
single-instance guard working, and Task Scheduler's 15-minute watchdog trigger fires exactly that case
every quarter of an hour - reporting it as a failure would make a correctly-working system look broken and
would trip ``RestartOnFailure``.

Nothing here changes what is permitted. In particular :data:`STOP_ENGAGED` makes an engaged stop
*legible*; it does not make it bypassable.
"""
from __future__ import annotations

OK = 0
#: Another instance already holds the runtime. Benign: see the module docstring.
ALREADY_RUNNING = 0
FATAL = 1
VOICE_DISABLED = 2
STOP_ENGAGED = 3
MISSING_DEPENDENCY = 4

#: What each code means, for logs and for `void doctor`-style reporting.
REASONS: dict[int, str] = {
    OK: "started normally (or another instance is already running)",
    FATAL: "unexpected failure - see ~/.void/void.log",
    VOICE_DISABLED: "voice is switched off in configuration (voice.enabled)",
    STOP_ENGAGED: "a stop is engaged - run 'python -m void clear-stop'",
    MISSING_DEPENDENCY: "the optional voice dependencies are not installed",
}


def describe(code: object) -> str:
    """A human reason for an exit code, for a log line or a status report."""
    try:
        return REASONS.get(int(code), f"unrecognised exit code {code}")
    except (TypeError, ValueError):
        return f"unrecognised exit code {code!r}"
