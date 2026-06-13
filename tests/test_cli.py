"""CLI argument parsing, preflight, and command dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pycastle import cli
from pycastle.cli import build_parser, main
from pycastle.preflight import PreflightError


def test_parses_run_arguments() -> None:
    args = build_parser().parse_args(["run", "-i", "3", "--runtime", "stub"])
    assert args.command == "run"
    assert args.iterations == 3
    assert args.runtime == "stub"
    assert args.include_unassigned is False


def test_parses_sandbox_setup() -> None:
    args = build_parser().parse_args(["sandbox", "setup", "--runtime", "codex"])
    assert args.command == "sandbox"
    assert args.sandbox_command == "setup"
    assert args.runtime == "codex"


def test_parses_init() -> None:
    args = build_parser().parse_args(["init"])
    assert args.command == "init"


def test_main_fails_fast_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_commands: object) -> None:
        raise PreflightError("Required command(s) not found on PATH: gh")

    monkeypatch.setattr(cli, "check_required_commands", boom)
    assert main(["run", "--runtime", "stub"]) == 1


def test_main_dispatches_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    dispatched = MagicMock(return_value=0)
    monkeypatch.setattr(cli, "_cmd_run", dispatched)

    assert main(["run", "--runtime", "stub"]) == 0
    dispatched.assert_called_once()


def test_main_reports_unimplemented_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    assert main(["init"]) == 2
