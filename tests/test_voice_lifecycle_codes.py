"""Why the voice runtime declined to start, and whether it survives the terminal that launched it.

Two lifecycle problems on the owner's machine motivated these:

**Task Scheduler said "Last Result: 1" for days.** Every refusal returned ``1``, so the one number
Windows puts in front of you could not distinguish "a stop is engaged" - a safety control working
exactly as designed - from a crash. The log held the answer; the surface did not. Each reason now has
its own code.

**The runtime died when the launching PowerShell was closed.** A ``void voice`` started in a terminal is
attached to that terminal's console, which is ordinary Windows behaviour rather than a V.O.I.D fault,
but it meant only the scheduled task could produce an independent runtime. ``--detached`` spawns the
same launcher Task Scheduler uses, with no console.

The security requirement these must not break: making an engaged stop *legible* must not make it
*bypassable*. Several tests below exist only to pin that.
"""
from __future__ import annotations

import subprocess

import pytest

import void.cli as cli
from void.runtime import exit_codes, voice_startup


class _FakeAssistant:
    """Enough of an Assistant for cmd_voice's precondition checks."""

    def __init__(self, *, voice_enabled=True, stop_engaged=False):
        outer = self

        class _Config:
            @staticmethod
            def get(key, default=None):
                if key == "voice.enabled":
                    return outer.voice_enabled
                return default

        class _KillSwitch:
            pass

        self.voice_enabled = voice_enabled
        self.config = _Config()
        self.kill_switch = _KillSwitch()
        self.kill_switch.engaged = stop_engaged


def _patch_assistant(monkeypatch, **kwargs):
    monkeypatch.setattr(cli, "Assistant", lambda on_event=None, **_: _FakeAssistant(**kwargs))


# --------------------------------------------------------------------------- the code contract

def test_every_refusal_has_its_own_code():
    """The whole point: "Last Result" must distinguish the reasons."""
    distinct = {exit_codes.VOICE_DISABLED, exit_codes.STOP_ENGAGED,
                exit_codes.MISSING_DEPENDENCY, exit_codes.FATAL}
    assert len(distinct) == 4
    assert exit_codes.OK not in distinct


def test_a_second_launch_finding_the_first_healthy_is_not_a_failure():
    """Task Scheduler's 15-minute watchdog fires this every quarter hour; reporting it as a failure
    would make a working system look broken and would trip RestartOnFailure."""
    assert exit_codes.ALREADY_RUNNING == exit_codes.OK == 0


@pytest.mark.parametrize("code", [exit_codes.OK, exit_codes.FATAL, exit_codes.VOICE_DISABLED,
                                  exit_codes.STOP_ENGAGED, exit_codes.MISSING_DEPENDENCY])
def test_each_code_can_be_explained_in_words(code):
    reason = exit_codes.describe(code)
    assert reason and "unrecognised" not in reason


def test_the_stop_code_says_how_to_recover():
    """Observable AND recoverable: the owner should not have to read source to get going again."""
    assert "clear-stop" in exit_codes.describe(exit_codes.STOP_ENGAGED)


@pytest.mark.parametrize("junk", [99, -1, None, "three", object()])
def test_an_unknown_code_is_described_rather_than_crashing(junk):
    assert "unrecognised" in exit_codes.describe(junk)


# --------------------------------------------------------------------------- cmd_voice refusals

def test_an_engaged_stop_is_reported_with_its_own_code(monkeypatch, capsys):
    _patch_assistant(monkeypatch, stop_engaged=True)
    assert cli.cmd_voice() == exit_codes.STOP_ENGAGED
    assert "clear-stop" in capsys.readouterr().out


def test_voice_switched_off_is_reported_with_its_own_code(monkeypatch, capsys):
    _patch_assistant(monkeypatch, voice_enabled=False)
    assert cli.cmd_voice() == exit_codes.VOICE_DISABLED
    assert "disabled" in capsys.readouterr().out.lower()


def test_an_engaged_stop_still_blocks_the_runtime(monkeypatch):
    """The security requirement. A clearer refusal must still be a refusal: no controller is built."""
    from void.voice.runtime import VoiceController
    _patch_assistant(monkeypatch, stop_engaged=True)
    built = []
    monkeypatch.setattr(VoiceController, "from_assistant",
                        lambda *a, **k: built.append(1))
    assert cli.cmd_voice() == exit_codes.STOP_ENGAGED
    assert built == [], "a stop was engaged and the voice runtime was started anyway"


def test_the_refusal_codes_are_non_zero_so_the_runtime_is_not_reported_as_started(monkeypatch):
    for kwargs, expected in (({"stop_engaged": True}, exit_codes.STOP_ENGAGED),
                             ({"voice_enabled": False}, exit_codes.VOICE_DISABLED)):
        _patch_assistant(monkeypatch, **kwargs)
        code = cli.cmd_voice()
        assert code == expected and code != 0


# --------------------------------------------------------------------------- the launcher

@pytest.fixture
def lock_free(monkeypatch):
    """Pretend no other instance holds the single-instance mutex.

    Needed because the mutex is REAL and process-wide: on a machine where V.O.I.D is actually running
    (the owner's, most of the time) ``main()`` correctly short-circuits to ALREADY_RUNNING, which would
    make these tests pass or fail depending on whether the assistant happened to be up.
    """
    monkeypatch.setattr(voice_startup, "_acquire_single_instance_lock", lambda: True)


def test_the_launcher_passes_the_commands_code_through_unchanged(lock_free):
    assert voice_startup.main(cli_main=lambda argv: exit_codes.STOP_ENGAGED) \
        == exit_codes.STOP_ENGAGED
    assert voice_startup.main(cli_main=lambda argv: exit_codes.OK) == exit_codes.OK


def test_the_launcher_runs_the_voice_command(lock_free):
    seen = []
    voice_startup.main(cli_main=lambda argv: seen.append(argv) or 0)
    assert seen == [["voice"]]


def test_an_unexpected_failure_is_the_fatal_code_not_a_traceback(lock_free):
    def exploding(argv):
        raise RuntimeError("something broke deep inside")

    assert voice_startup.main(cli_main=exploding) == exit_codes.FATAL


def test_a_duplicate_instance_exits_benignly(monkeypatch):
    """Measured on the owner's machine: the detached launch hit exactly this path while the scheduled
    instance held the microphone, and no second owner was created."""
    monkeypatch.setattr(voice_startup, "_acquire_single_instance_lock", lambda: False)
    called = []
    assert voice_startup.main(cli_main=lambda argv: called.append(argv) or 0) \
        == exit_codes.ALREADY_RUNNING
    assert called == [], "a second instance started the runtime anyway"


# --------------------------------------------------------------------------- detached launch

class _FakePopen:
    instances: list = []

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        _FakePopen.instances.append(self)


@pytest.fixture
def popen(monkeypatch):
    _FakePopen.instances = []
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_a_detached_launch_returns_immediately_without_starting_a_runtime(popen, monkeypatch):
    built = []
    from void.voice.runtime import VoiceController
    monkeypatch.setattr(VoiceController, "from_assistant", lambda *a, **k: built.append(1))
    assert cli.cmd_voice_detached() == exit_codes.OK
    assert built == [], "the detached command must spawn, not run the runtime in this process"
    assert len(popen.instances) == 1


def test_the_detached_launch_uses_the_same_launcher_task_scheduler_uses(popen):
    """One startup path, so the single-instance guard and the stop check apply identically."""
    cli.cmd_voice_detached()
    argv = popen.instances[0].argv
    assert argv[1].endswith("voice_startup.py")
    assert "pythonw" in argv[0].lower() or argv[0].lower().endswith("python.exe")


def test_the_detached_process_has_no_console_and_its_own_process_group(popen):
    """Without these, closing the launching terminal still takes the runtime with it."""
    cli.cmd_voice_detached()
    flags = popen.instances[0].kwargs.get("creationflags", 0)
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
        expected = getattr(subprocess, name, 0)
        if expected:
            assert flags & expected, f"{name} not set on the detached launch"


def test_the_detached_process_does_not_inherit_this_terminals_streams(popen):
    cli.cmd_voice_detached()
    kwargs = popen.instances[0].kwargs
    assert kwargs.get("stdin") == subprocess.DEVNULL
    assert kwargs.get("stdout") == subprocess.DEVNULL
    assert kwargs.get("stderr") == subprocess.DEVNULL


def test_a_failed_spawn_is_reported_rather_than_raised(monkeypatch, capsys):
    def refusing(*args, **kwargs):
        raise OSError("no such executable")

    monkeypatch.setattr(subprocess, "Popen", refusing)
    assert cli.cmd_voice_detached() == exit_codes.FATAL
    assert "Could not start" in capsys.readouterr().out


def test_the_detached_launch_does_not_bypass_the_stop_state():
    """It spawns the launcher, which performs the SAME stop check - there is no path here that skips
    it, and no flag that would."""
    import inspect
    source = inspect.getsource(cli.cmd_voice_detached)
    for forbidden in ("clear_stop", "kill_switch", "STOP", "engaged = False", "--force"):
        assert forbidden not in source


def test_the_voice_command_still_accepts_no_arguments():
    """--detached must be additive; plain `void voice` keeps working as it always did."""
    parser = cli.build_parser()
    assert parser.parse_args(["voice"]).detached is False
    assert parser.parse_args(["voice", "--detached"]).detached is True
