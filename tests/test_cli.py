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
    # The flag now parses to None when omitted so the marker can be consulted;
    # the effective host/docker choice is resolved later (see _resolve_sandbox).
    assert args.sandbox is None


def test_run_sandbox_flag_defaults_to_none_for_marker_resolution() -> None:
    # No --sandbox flag parses to None, which signals "consult the marker".
    args = build_parser().parse_args(["run"])
    assert args.sandbox is None


def test_run_image_flag_parses() -> None:
    # `--image X` is bring-your-own-image: it is parsed verbatim onto args.
    args = build_parser().parse_args(["run", "--image", "my/agent:dev"])
    assert args.image == "my/agent:dev"


def test_run_image_flag_defaults_to_none() -> None:
    # No --image means "resolve from the Dockerfile, else the default tag".
    args = build_parser().parse_args(["run"])
    assert args.image is None


def test_parses_sandbox_build() -> None:
    args = build_parser().parse_args(["sandbox", "build"])
    assert args.command == "sandbox"
    assert args.sandbox_command == "build"


def test_make_run_id_is_a_timestamp_shape() -> None:
    # The CLI is where a run id is minted (the orchestrator never reads a clock,
    # so it stays deterministic in tests). The id is a YYYYMMDD-HHMMSS stamp.
    run_id = cli._make_run_id()
    assert len(run_id) == 15
    date, _, time = run_id.partition("-")
    assert date.isdigit() and len(date) == 8
    assert time.isdigit() and len(time) == 6


def test_run_passes_a_generated_run_id_to_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The orchestrator receives an injected run id from the CLI rather than
    # generating one itself; here we pin it and assert it is threaded through.
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())
    monkeypatch.setattr(cli, "_make_run_id", lambda: "20260613-101500")

    captured: dict[str, object] = {}

    def fake_run_loop(*, run_id: str, **_kwargs: object) -> MagicMock:
        captured["run_id"] = run_id
        outcome = MagicMock()
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--runtime", "stub"]) == 0
    assert captured["run_id"] == "20260613-101500"


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


def test_init_scaffolds_the_chosen_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``pycastle init`` prompts host/docker and scaffolds the matching fixture.

    The interactive prompt is stubbed (it is not unit-tested); we assert the
    answer is threaded into the scaffolder and a fixture is written into cwd.
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    # The user picks Docker-first at the prompt.
    monkeypatch.setattr(cli, "_prompt_sandbox", lambda: "docker")

    assert main(["init"]) == 0

    fixture = tmp_path / ".pycastle"
    assert (fixture / "main.py").is_file()
    assert (fixture / "Dockerfile").is_file()
    assert (fixture / ".gitignore").is_file()
    # The Docker-first choice is recorded in the scaffolded fixture.
    assert (fixture / "sandbox").read_text().strip() == "docker"


def test_init_defaults_the_prompt_to_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty answer at the init prompt defaults to host-first."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    # Simulate the user pressing Enter (empty input) by stubbing input().
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")

    assert main(["init"]) == 0
    assert (tmp_path / ".pycastle" / "sandbox").read_text().strip() == "host"


def test_init_refuses_to_clobber_an_existing_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running init where ``.pycastle/`` already exists exits non-zero, no clobber."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_prompt_sandbox", lambda: "host")

    assert main(["init"]) == 0
    # Second run is refused; the fixture is left as-is.
    assert main(["init"]) == 1
    assert (tmp_path / ".pycastle" / "sandbox").read_text().strip() == "host"


def test_init_does_not_require_docker_or_runtime_in_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """init only needs git/gh — never docker or an agent CLI on PATH."""
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_init", lambda _args: 0)

    main(["init"])

    assert "docker" not in seen["commands"]
    assert "claude" not in seen["commands"]
    assert "codex" not in seen["commands"]


def test_run_docker_builds_a_sandboxed_claude_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run --sandbox docker --runtime claude`` drives Claude inside Docker.

    The runtime handed to the orchestrator wraps its inner argv into a
    ``docker run`` argv, so both the Runtime and its commands run in the
    container. Everything external is mocked: no real Docker, gh, or git.
    """
    # No Dockerfile in this cwd, so image resolution falls back to the default
    # tag and never touches docker -- the run stays hermetic.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured = {}

    def fake_run_loop(*, runtime: object, **_kwargs: object) -> MagicMock:
        captured["runtime"] = runtime
        outcome = MagicMock()
        outcome.issues = []
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
    # No Dockerfile in this cwd, so image resolution falls back to the default
    # tag and never touches docker -- the run stays hermetic.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured = {}

    def fake_run_loop(*, runtime: object, **_kwargs: object) -> MagicMock:
        captured["runtime"] = runtime
        outcome = MagicMock()
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--sandbox", "docker", "--runtime", "codex"]) == 0

    runtime = captured["runtime"]
    assert runtime.name == "codex"
    assert runtime.argv_wrapper is not None
    wrapped = runtime.argv_wrapper(["codex", "exec", "--json", "x"])
    assert wrapped[:3] == ["docker", "run", "--rm"]
    assert "pycastle-codex-auth:/home/node/.codex" in wrapped


def _write_marker(tmp_path: Path, value: str) -> None:
    """Write ``value`` into a ``.pycastle/sandbox`` marker under ``tmp_path``."""
    fixture = tmp_path / ".pycastle"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "sandbox").write_text(value)


def _mock_run_externals(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub everything ``pycastle run`` touches and capture the run-loop kwargs.

    Mocks preflight, repo/branch/assignee resolution, the issue source, and the
    orchestrator's ``run_loop`` so no real gh/git/docker/network runs. Returns
    the dict the fake run loop records its kwargs into.
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured: dict[str, object] = {}

    def fake_run_loop(*, runtime: object, **_kwargs: object) -> MagicMock:
        captured["runtime"] = runtime
        outcome = MagicMock()
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    return captured


def test_run_defaults_sandbox_from_docker_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No flag + marker=docker drives the run inside the Docker sandbox.

    The recorded init-time choice is honoured: the runtime handed to the
    orchestrator carries a docker-wrapping argv even though no ``--sandbox`` was
    passed.
    """
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert runtime.argv_wrapper is not None
    wrapped = runtime.argv_wrapper(["claude", "-p", "x"])
    assert wrapped[:3] == ["docker", "run", "--rm"]


def test_run_defaults_sandbox_from_host_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No flag + marker=host drives the run on the host (no docker wrapper)."""
    _write_marker(tmp_path, "host\n")
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None


def test_run_flag_overrides_docker_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``--sandbox host`` overrides a marker that says docker."""
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--sandbox", "host", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None


def test_run_flag_overrides_host_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``--sandbox docker`` overrides a marker that says host."""
    _write_marker(tmp_path, "host\n")
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--sandbox", "docker", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert runtime.argv_wrapper is not None
    wrapped = runtime.argv_wrapper(["claude", "-p", "x"])
    assert wrapped[:3] == ["docker", "run", "--rm"]


def test_run_falls_back_to_host_when_marker_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No flag and no marker at all falls back to a host run."""
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None


def test_run_falls_back_to_host_for_empty_or_garbage_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty or unrecognised marker value falls back to host, never crashes."""
    monkeypatch.chdir(tmp_path)

    for value in ("", "   \n", "weird-value\n"):
        _write_marker(tmp_path, value)
        captured = _mock_run_externals(monkeypatch)
        assert main(["run", "--runtime", "claude"]) == 0
        runtime = captured["runtime"]
        assert getattr(runtime, "argv_wrapper", None) is None


def test_run_marker_docker_requires_docker_in_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A docker marker (no flag) makes preflight require docker, not the agent CLI.

    Resolution happens before preflight, so the required-command set matches
    where the run will actually execute.
    """
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)

    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 0)

    main(["run", "--runtime", "claude"])

    assert "docker" in seen["commands"]
    assert "claude" not in seen["commands"]


def test_resolve_sandbox_prefers_explicit_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_resolve_sandbox`` returns the flag verbatim without reading the marker."""
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_sandbox("host") == "host"
    assert cli._resolve_sandbox("docker") == "docker"


def test_resolve_sandbox_reads_marker_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no flag, ``_resolve_sandbox`` reads the marker; missing -> host."""
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_sandbox(None) == "host"  # no marker

    _write_marker(tmp_path, "docker\n")
    assert cli._resolve_sandbox(None) == "docker"


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
        # Login succeeds, the fresh-container status check fails. The login
        # argv carries `login`; the status argv carries `status`.
        proc.returncode = 0 if "login" in args else 1
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
    assert login[-3:] == ["codex", "login", "--device-auth"]
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


# --- Agent-image resolution and on-demand build (ADR-0005, issue #25) --------
#
# `_resolve_agent_image` encodes the 3-way precedence: --image wins (no build,
# no Dockerfile read); else a present Dockerfile is built on demand into its
# content-addressed tag, skipping the build when that tag already exists; else
# the default tag. docker is always mocked through run_cmd -- no real build.


def _write_dockerfile(tmp_path: Path, text: str) -> Path:
    """Write a ``Dockerfile`` into a ``.pycastle/`` fixture under ``tmp_path``."""
    fixture = tmp_path / ".pycastle"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "Dockerfile").write_text(text)
    return fixture


def _fake_docker(*, inspect_returncode: int) -> tuple[list[list[str]], object]:
    """Build a run_cmd stand-in recording every docker argv it is handed.

    ``docker image inspect`` returns ``inspect_returncode``; everything else
    (the build) returns success. Returns the recording list and the callable.
    """
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(list(args))
        proc = MagicMock()
        if args[:3] == ["docker", "image", "inspect"]:
            proc.returncode = inspect_returncode
        else:
            proc.returncode = 0
        return proc

    return calls, runner


def test_resolve_agent_image_flag_wins_never_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Precedence 1: --image X is returned verbatim. The Dockerfile is never read
    # and docker is never invoked (not even an inspect), so a present Dockerfile
    # is irrelevant under bring-your-own-image.
    fixture = _write_dockerfile(tmp_path, "FROM node:22-slim\n")
    calls, runner = _fake_docker(inspect_returncode=0)
    monkeypatch.setattr(cli, "run_cmd", runner)

    image = cli._resolve_agent_image("my/agent:dev", fixture)

    assert image == "my/agent:dev"
    assert calls == []


def test_resolve_agent_image_builds_when_tag_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Precedence 2, cold cache: Dockerfile present, `image inspect` non-zero ->
    # exactly one `docker build -t <hashed tag> .pycastle`, then run that tag.
    from pycastle import sandbox

    text = "FROM node:22-slim\nRUN echo hi\n"
    fixture = _write_dockerfile(tmp_path, text)
    tag = sandbox.image_tag_for_dockerfile(text)
    calls, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "run_cmd", runner)

    image = cli._resolve_agent_image(None, fixture)

    assert image == tag
    inspects = [c for c in calls if c[:3] == ["docker", "image", "inspect"]]
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert len(inspects) == 1
    assert len(builds) == 1
    assert builds[0] == ["docker", "build", "-t", tag, str(fixture)]


def test_resolve_agent_image_raises_when_build_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A failed `docker build` must surface, not be swallowed: returning the tag
    # would let the run proceed against an image that was never built. The
    # resolver raises PreflightError so the run never reaches `docker run`.
    text = "FROM node:22-slim\nRUN false\n"
    fixture = _write_dockerfile(tmp_path, text)
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(list(args))
        proc = MagicMock()
        # The image is absent (inspect non-zero) and the build itself fails.
        proc.returncode = 1
        return proc

    monkeypatch.setattr(cli, "run_cmd", runner)

    with pytest.raises(PreflightError):
        cli._resolve_agent_image(None, fixture)

    # The one build was attempted; the failure is what raised.
    assert [c for c in calls if c[:2] == ["docker", "build"]]


def test_run_docker_exits_nonzero_when_build_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End to end: a failed build aborts the run with a clean non-zero exit (not a
    # traceback) and never resolves a runtime or opens a PR.
    _write_dockerfile(tmp_path, "FROM node:22-slim\nRUN false\n")
    monkeypatch.chdir(tmp_path)

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 1  # inspect: absent; build: fails
        return proc

    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "run_cmd", runner)

    assert main(["run", "--sandbox", "docker", "--runtime", "claude"]) == 1


def test_sandbox_build_exits_nonzero_when_build_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `sandbox build` surfaces a build failure as a non-zero exit rather than
    # claiming the image is ready.
    _write_dockerfile(tmp_path, "FROM node:22-slim\nRUN false\n")
    monkeypatch.chdir(tmp_path)

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 1
        return proc

    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "run_cmd", runner)

    assert main(["sandbox", "build"]) == 1


def test_resolve_agent_image_skips_build_when_tag_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Precedence 2, warm cache: an unchanged Dockerfile resolves to a tag that
    # already exists (`image inspect` returns 0), so no build runs -- instant run.
    from pycastle import sandbox

    text = "FROM node:22-slim\n"
    fixture = _write_dockerfile(tmp_path, text)
    tag = sandbox.image_tag_for_dockerfile(text)
    calls, runner = _fake_docker(inspect_returncode=0)
    monkeypatch.setattr(cli, "run_cmd", runner)

    image = cli._resolve_agent_image(None, fixture)

    assert image == tag
    assert [c for c in calls if c[:2] == ["docker", "build"]] == []


def test_resolve_agent_image_tag_tracks_dockerfile_edits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Editing the Dockerfile changes the resolved tag, so the rebuild is driven
    # by content, not by a fixed default tag.
    fixture = _write_dockerfile(tmp_path, "FROM node:22-slim\n")
    _, runner = _fake_docker(inspect_returncode=0)
    monkeypatch.setattr(cli, "run_cmd", runner)
    first = cli._resolve_agent_image(None, fixture)

    (fixture / "Dockerfile").write_text("FROM node:22-slim\nRUN echo edited\n")
    second = cli._resolve_agent_image(None, fixture)

    assert first != second


def test_resolve_agent_image_falls_back_to_default_without_dockerfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Precedence 3: no --image and no Dockerfile -> the default tag, never a
    # build (there is no recipe to build).
    from pycastle import sandbox

    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    calls, runner = _fake_docker(inspect_returncode=0)
    monkeypatch.setattr(cli, "run_cmd", runner)

    image = cli._resolve_agent_image(None, fixture)

    assert image == sandbox.DEFAULT_IMAGE
    assert [c for c in calls if c[:2] == ["docker", "build"]] == []


def test_resolve_agent_image_builds_once_not_per_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A single resolve does one inspect and at most one build; the caller resolves
    # once before the run loop, never per iteration.
    fixture = _write_dockerfile(tmp_path, "FROM node:22-slim\n")
    calls, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "run_cmd", runner)

    cli._resolve_agent_image(None, fixture)

    assert len([c for c in calls if c[:2] == ["docker", "build"]]) == 1


def test_run_docker_resolves_and_threads_image_into_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End to end: `run --sandbox docker` resolves the image once and passes it to
    # the runtime, so the docker run argv carries the resolved tag.
    from pycastle import sandbox

    text = "FROM node:22-slim\nRUN echo wired\n"
    _write_dockerfile(tmp_path, text)
    monkeypatch.chdir(tmp_path)
    tag = sandbox.image_tag_for_dockerfile(text)
    _, runner = _fake_docker(inspect_returncode=0)
    monkeypatch.setattr(cli, "run_cmd", runner)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--sandbox", "docker", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    wrapped = runtime.argv_wrapper(["claude", "-p", "x"])
    assert tag in wrapped


def test_run_image_flag_threads_through_and_never_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `run --image X --sandbox docker` runs X and never builds, even with a
    # Dockerfile present.
    _write_dockerfile(tmp_path, "FROM node:22-slim\n")
    monkeypatch.chdir(tmp_path)
    calls, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "run_cmd", runner)
    captured = _mock_run_externals(monkeypatch)

    assert (
        main(
            [
                "run",
                "--sandbox",
                "docker",
                "--runtime",
                "claude",
                "--image",
                "ci/agent:fixed",
            ]
        )
        == 0
    )

    runtime = captured["runtime"]
    wrapped = runtime.argv_wrapper(["claude", "-p", "x"])
    assert "ci/agent:fixed" in wrapped
    assert [c for c in calls if c[:2] == ["docker", "build"]] == []


def test_run_host_never_resolves_an_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Resolution is docker-only: a host run never inspects or builds an image,
    # even with a Dockerfile present.
    _write_dockerfile(tmp_path, "FROM node:22-slim\n")
    monkeypatch.chdir(tmp_path)
    calls, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "run_cmd", runner)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--sandbox", "host", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None
    assert calls == []


def test_sandbox_build_builds_the_content_addressed_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `sandbox build` builds the Dockerfile into its content-addressed tag via
    # the same build path the run takes implicitly.
    from pycastle import sandbox

    text = "FROM node:22-slim\nRUN echo built\n"
    _write_dockerfile(tmp_path, text)
    monkeypatch.chdir(tmp_path)
    tag = sandbox.image_tag_for_dockerfile(text)
    calls, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "run_cmd", runner)

    assert main(["sandbox", "build"]) == 0

    # `sandbox build` builds the project fixture dir (the relative .pycastle).
    builds = [c for c in calls if c[:2] == ["docker", "build"]]
    assert builds == [["docker", "build", "-t", tag, str(cli.FIXTURE_DIR)]]


def test_sandbox_build_without_dockerfile_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # `sandbox build` with no Dockerfile has nothing to build: it errors with
    # guidance, never falling back to the default tag.
    (tmp_path / ".pycastle").mkdir()
    monkeypatch.chdir(tmp_path)
    _, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "run_cmd", runner)

    assert main(["sandbox", "build"]) == 1


def test_sandbox_build_requires_docker_in_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole sandbox command group requires docker on PATH; build is covered.
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_sandbox_build", lambda _args: 0)

    main(["sandbox", "build"])

    assert "docker" in seen["commands"]
