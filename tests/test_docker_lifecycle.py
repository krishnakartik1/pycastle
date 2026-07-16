import argparse
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle import cli
from pycastle.readiness import AgentImagePreparationError, prepare_agent_image

IMAGE = "sha256:" + "b" * 64


def test_canonical_build_uses_repository_context_and_pins_image_id(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    dockerfile = fixture / "Dockerfile"
    dockerfile.write_text("FROM scratch\n")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        stdout = IMAGE + "\n" if argv[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    assert prepare_agent_image(fixture, runner=runner, cwd=tmp_path) == IMAGE
    build = calls[0][0]
    assert build[:2] == ["docker", "build"]
    assert build[build.index("--file") + 1] == str(dockerfile.resolve())
    assert build[-1] == str(tmp_path.resolve())
    assert calls[1][0][:5] == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
    ]


@pytest.mark.parametrize("unsafe", ["missing", "symlink"])
def test_canonical_dockerfile_must_be_regular_and_not_symlinked(
    tmp_path: Path, unsafe: str
) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    if unsafe == "symlink":
        target = tmp_path / "Dockerfile"
        target.write_text("FROM scratch\n")
        (fixture / "Dockerfile").symlink_to(target)
    with pytest.raises(AgentImagePreparationError):
        prepare_agent_image(fixture, runner=MagicMock(), cwd=tmp_path)


def test_removed_cli_image_and_sandbox_commands_are_rejected() -> None:
    parser = cli.build_parser()
    for argv in (
        ["run", "--image", "example:latest"],
        ["doctor", "--image", "example:latest"],
        ["sandbox", "build"],
        ["sandbox", "setup"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)


def test_host_runtime_login_uses_native_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "run_cmd",
        lambda argv, **_kwargs: (
            calls.append(list(argv)) or subprocess.CompletedProcess(argv, 0, "", "")
        ),
    )
    args = argparse.Namespace(runtime="codex", sandbox="host")
    assert cli._cmd_runtime_login(args) == 0
    assert calls == [["codex", "login", "--device-auth"]]


def test_docker_runtime_login_builds_then_uses_pinned_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "Dockerfile").write_text("FROM scratch\n")
    calls: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        stdout = IMAGE if argv[:3] == ["docker", "image", "inspect"] else ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(cli, "run_cmd", runner)
    assert (
        cli._cmd_runtime_login(argparse.Namespace(runtime="claude", sandbox="docker"))
        == 0
    )
    assert calls[0][:2] == ["docker", "build"]
    assert calls[1][:3] == ["docker", "image", "inspect"]
    assert calls[2][:3] == ["docker", "run", "--rm"]
    assert IMAGE in calls[2]
    assert "pycastle-claude-auth:/pycastle/auth" in calls[2]
