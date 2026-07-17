import argparse
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle import cli, readiness
from pycastle.readiness import AgentImagePreparationError, prepare_agent_image
from pycastle.scaffold import scaffold_fixture

IMAGE = "sha256:" + "b" * 64


def test_canonical_build_uses_repository_context_and_pins_image_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        readiness, "_resolve_posix_host_identity", lambda: ("1234", "5678")
    )
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
    assert build[:6] == [
        "docker",
        "build",
        "--build-arg",
        "PYCASTLE_HOST_UID=1234",
        "--build-arg",
        "PYCASTLE_HOST_GID=5678",
    ]
    assert build[build.index("--file") + 1] == str(dockerfile.resolve())
    assert build[-1] == str(tmp_path.resolve())
    assert calls[1][0][:5] == [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
    ]


def test_host_identity_resolver_returns_decimal_effective_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness.os, "geteuid", lambda: 1234)
    monkeypatch.setattr(readiness.os, "getegid", lambda: 5678)

    assert readiness._resolve_posix_host_identity() == ("1234", "5678")


def test_host_identity_resolver_rejects_root_with_actionable_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness.os, "geteuid", lambda: 0)
    monkeypatch.setattr(readiness.os, "getegid", lambda: 0)

    with pytest.raises(AgentImagePreparationError, match="non-root host user"):
        readiness._resolve_posix_host_identity()


@pytest.mark.skipif(
    os.environ.get("PYCASTLE_DOCKER_TESTS") != "1",
    reason="set PYCASTLE_DOCKER_TESTS=1 to run Docker-backed regressions",
)
def test_scaffold_image_writes_host_owned_0755_bind_mount(tmp_path: Path) -> None:
    scaffold_fixture(tmp_path, sandbox="docker")
    workspace = tmp_path / "worktree"
    workspace.mkdir(mode=0o755)
    existing = workspace / "existing"
    existing.write_text("before\n")
    existing.chmod(0o644)

    image = prepare_agent_image(tmp_path / ".pycastle", cwd=tmp_path)
    command = [
        "docker",
        "run",
        "--rm",
        "--mount",
        f"type=bind,src={workspace},dst=/worktree",
        "--workdir",
        "/worktree",
        image,
        "sh",
        "-c",
        "mkdir created-dir && : > created-file && printf after >> existing",
    ]

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (workspace / "created-dir").is_dir()
    assert (workspace / "created-file").is_file()
    assert existing.read_text().endswith("after")


@pytest.mark.parametrize("missing", ["geteuid", "getegid"])
def test_host_identity_resolver_fails_cleanly_without_effective_unix_ids(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    monkeypatch.delattr(readiness.os, missing)

    with pytest.raises(AgentImagePreparationError, match="supported POSIX host"):
        readiness._resolve_posix_host_identity()


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


def test_docker_runtime_login_preserves_image_preparation_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = (
        "Docker Agent-image preparation cannot reconcile host UID 0 without "
        "violating the non-root Image contract. Run PyCastle as a non-root host user."
    )
    monkeypatch.setattr(
        cli,
        "prepare_agent_image",
        MagicMock(side_effect=AgentImagePreparationError(message)),
    )

    with pytest.raises(cli.PreflightError, match="cannot reconcile host UID 0") as exc:
        cli._cmd_runtime_login(argparse.Namespace(runtime="codex", sandbox="docker"))

    assert str(exc.value) == message
