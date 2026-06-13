"""CLI argument parsing, preflight, and command dispatch."""

from __future__ import annotations

from pathlib import Path
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
    assert args.sandbox == "host"


def test_run_sandbox_defaults_to_host() -> None:
    args = build_parser().parse_args(["run"])
    assert args.sandbox == "host"


def test_parses_run_sandbox_docker() -> None:
    args = build_parser().parse_args(["run", "--sandbox", "docker"])
    assert args.sandbox == "docker"


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


def test_run_docker_builds_a_sandboxed_claude_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run --sandbox docker --runtime claude`` drives Claude inside Docker.

    The runtime handed to the orchestrator wraps its inner argv into a
    ``docker run`` argv, so both the Runtime and its commands run in the
    container. Everything external is mocked: no real Docker, gh, or git.
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured = {}

    def fake_run_loop(*, runtime: object, **_kwargs: object) -> MagicMock:
        captured["runtime"] = runtime
        outcome = MagicMock()
        outcome.issue = None
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--sandbox", "docker", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    # The handed-off runtime carries a wrapper that produces a docker argv.
    assert runtime.argv_wrapper is not None
    wrapped = runtime.argv_wrapper(["claude", "-p", "x"])
    assert wrapped[:3] == ["docker", "run", "--rm"]
    assert "pycastle-claude-auth:/home/node/.claude" in wrapped


def test_run_docker_builds_a_sandboxed_codex_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run --sandbox docker --runtime codex`` drives Codex inside Docker.

    Switching runtime is just the flag: the same dispatch wraps the codex inner
    argv into a docker run argv against the codex auth volume. Everything
    external is mocked.
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured = {}

    def fake_run_loop(*, runtime: object, **_kwargs: object) -> MagicMock:
        captured["runtime"] = runtime
        outcome = MagicMock()
        outcome.issue = None
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--sandbox", "docker", "--runtime", "codex"]) == 0

    runtime = captured["runtime"]
    assert runtime.name == "codex"
    assert runtime.argv_wrapper is not None
    wrapped = runtime.argv_wrapper(["codex", "exec", "--json", "x"])
    assert wrapped[:3] == ["docker", "run", "--rm"]
    assert "pycastle-codex-auth:/home/node/.codex" in wrapped


def test_run_host_codex_requires_codex_in_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 0)

    main(["run", "--sandbox", "host", "--runtime", "codex"])

    # The host codex path needs the codex CLI on PATH, not docker.
    assert "codex" in seen["commands"]
    assert "docker" not in seen["commands"]


def test_run_docker_requires_docker_in_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 0)

    main(["run", "--sandbox", "docker", "--runtime", "claude"])

    assert "docker" in seen["commands"]
    # The host claude binary is not required when the agent runs in Docker.
    assert "claude" not in seen["commands"]


def test_build_runtime_docker_codex_builds_a_sandboxed_runtime(
    tmp_path: Path,
) -> None:
    # The docker-vs-host choice is orthogonal to the runtime: asking for codex
    # in Docker yields a CodexRuntime whose wrapper produces a docker argv
    # against the codex auth volume, exactly as claude does.
    runtime = cli._build_runtime("codex", "docker", tmp_path)
    assert runtime.name == "codex"
    assert runtime.argv_wrapper is not None
    wrapped = runtime.argv_wrapper(["codex", "exec", "--json", "x"])
    assert wrapped[:3] == ["docker", "run", "--rm"]
    assert "pycastle-codex-auth:/home/node/.codex" in wrapped
    assert "CODEX_HOME=/home/node/.codex" in wrapped


def test_build_runtime_host_path_is_a_bare_runtime(tmp_path: Path) -> None:
    # The host path produces a plain runtime with no docker wrapper, so its
    # argv stays the bare claude command. Docker only enters via --sandbox docker.
    runtime = cli._build_runtime("claude", "host", tmp_path)
    assert getattr(runtime, "argv_wrapper", None) is None


def test_sandbox_setup_claude_runs_login_then_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox setup --runtime claude`` runs the login then the status check.

    The interactive login is not unit-tested; the command *construction* and
    ordering are. Docker is never really invoked: the runner is a mock.
    """
    from pycastle import sandbox as sandbox_mod

    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)

    calls: list[list[str]] = []

    def fake_runner(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        return proc

    monkeypatch.setattr(cli, "run_cmd", fake_runner)

    assert main(["sandbox", "setup", "--runtime", "claude"]) == 0

    assert calls[0] == sandbox_mod.build_login_command("claude")
    assert calls[1] == sandbox_mod.build_status_command("claude")
    # Credentials are never read: no cat/echo of the volume anywhere.
    for argv in calls:
        joined = " ".join(argv)
        assert "cat" not in joined
        assert ".credentials.json" not in joined


def test_sandbox_setup_status_failure_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)

    def fake_runner(args: list[str], **_kwargs: object) -> MagicMock:
        proc = MagicMock()
        # Login succeeds, the fresh-container status check fails.
        proc.returncode = 0 if "/login" in args else 1
        return proc

    monkeypatch.setattr(cli, "run_cmd", fake_runner)

    assert main(["sandbox", "setup", "--runtime", "claude"]) == 1


def test_sandbox_setup_codex_uses_device_auth_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sandbox setup --runtime codex`` runs the device-authorization login.

    The login command construction is asserted (no localhost callback, no TTY);
    Docker is never really invoked. A device-auth login is the whole flow: no
    fresh-container status check runs, unlike Claude.
    """
    from pycastle import sandbox as sandbox_mod

    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)

    calls: list[list[str]] = []

    def fake_runner(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(list(args))
        proc = MagicMock()
        proc.returncode = 0
        return proc

    monkeypatch.setattr(cli, "run_cmd", fake_runner)

    assert main(["sandbox", "setup", "--runtime", "codex"]) == 0

    # Exactly one command: the device-authorization login. No status check.
    assert calls == [sandbox_mod.build_login_command("codex")]
    login = calls[0]
    assert login[-3:] == ["codex", "login", "--device-code"]
    # The device flow needs no TTY, so -it is never passed.
    assert "-it" not in login


def test_sandbox_setup_codex_login_failure_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)

    def fake_runner(_args: list[str], **_kwargs: object) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 1
        return proc

    monkeypatch.setattr(cli, "run_cmd", fake_runner)

    assert main(["sandbox", "setup", "--runtime", "codex"]) == 1
