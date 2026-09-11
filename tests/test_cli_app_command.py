"""Additive CLI wiring for `python -m void app` (the persistent desktop
runtime). Parser-level and dispatch-level only - never launches Qt/a real
app, and never touches void.runtime.app's real Qt-dependent factories.
"""
from __future__ import annotations

import sys

from void.cli import build_parser


def test_app_subcommand_is_registered():
    parser = build_parser()
    args = parser.parse_args(["app"])
    assert args.command == "app"


def test_existing_subcommands_still_parse():
    parser = build_parser()
    for argv in (["ui"], ["voice"], ["tasks"], ["stop"], ["clear-stop"],
                 ["resume", "abc123"], ["approve", "abc123"],
                 ["deny", "abc123"], ["clarify", "abc123", "1"]):
        args = parser.parse_args(argv)
        assert args.command == argv[0]


def test_cmd_app_dispatches_to_runtime_main(monkeypatch):
    import void.cli as cli_mod

    calls = []
    monkeypatch.setattr("void.runtime.app.main", lambda: calls.append(1) or 0)
    rc = cli_mod.cmd_app()
    assert rc == 0 and calls == [1]


def test_cmd_app_reports_cleanly_when_ui_stack_unavailable(monkeypatch):
    import void.cli as cli_mod

    # Simulating the import failing (e.g. PySide6 missing) must print a
    # clean message and return 1, never raise out of cmd_app.
    monkeypatch.setitem(sys.modules, "void.runtime.app", None)
    rc = cli_mod.cmd_app()
    assert rc == 1
