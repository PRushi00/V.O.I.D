"""Voice-first startup contract: normal startup has no presentation dependency."""
from __future__ import annotations

import sys

from void.config import Config


def test_tracked_default_enables_voice_without_machine_local_overrides(tmp_path):
    config = Config.load(local_path=tmp_path / "missing-local-config.yaml")
    assert config.get("voice.enabled") is True


def test_headless_voice_command_does_not_require_ui_modules(monkeypatch):
    import void.cli as cli
    from void.voice.runtime import VoiceController

    class FakeAssistant:
        class Config:
            @staticmethod
            def get(_key, default=None):
                return True if _key == "voice.enabled" else default

        class KillSwitch:
            engaged = False

        config = Config()
        kill_switch = KillSwitch()

    class FakeController:
        stopped = True

        def start(self):
            pass

        def shutdown(self, _reason):
            pass

    monkeypatch.setattr(cli, "Assistant", lambda on_event: FakeAssistant())
    monkeypatch.setattr(
        VoiceController, "from_assistant", lambda *args, **kwargs: FakeController())
    monkeypatch.setitem(sys.modules, "void.ui.singularity_overlay", None)
    monkeypatch.setitem(sys.modules, "void.ui.widget", None)

    assert cli.cmd_voice() == 0


def test_developer_ui_commands_remain_explicitly_registered():
    from void.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["app"]).command == "app"
    assert parser.parse_args(["singularity"]).command == "singularity"


def test_autostart_launcher_delegates_to_existing_headless_voice_command(monkeypatch):
    from void.runtime import voice_startup

    calls = []
    monkeypatch.setattr(voice_startup, "_install_background_logging", lambda: None)
    assert voice_startup.main(lambda argv: calls.append(argv) or 0) == 0
    assert calls == [["voice"]]
