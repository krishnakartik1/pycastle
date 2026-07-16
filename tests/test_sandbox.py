from pathlib import Path

import pytest

from pycastle import sandbox

IMAGE = "sha256:" + "a" * 64


def test_run_uses_fresh_container_pinned_image_and_only_documented_mounts(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "tree with spaces"
    argv = sandbox.build_run_command(
        "claude",
        inner_argv=["tool", "arg"],
        workspace=tmp_path,
        workdir=worktree,
        image=IMAGE,
        environment={"PYCASTLE_SCOPE": "item"},
    )

    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("-w") + 1] == str(worktree.resolve())
    assert [argv[index + 1] for index, value in enumerate(argv) if value == "-v"] == [
        f"{tmp_path.resolve()}:{tmp_path.resolve()}",
        "pycastle-claude-auth:/pycastle/auth",
    ]
    assert "-u" not in argv
    assert "HOME" not in " ".join(argv)
    assert argv[argv.index(IMAGE) + 1 :] == ["tool", "arg"]
    assert "CLAUDE_CONFIG_DIR=/pycastle/auth" in argv
    assert "PYCASTLE_SCOPE=item" in argv


@pytest.mark.parametrize(
    ("runtime", "login", "status", "environment"),
    [
        (
            "claude",
            ["claude", "auth", "login", "--claudeai"],
            ["claude", "auth", "status"],
            "CLAUDE_CONFIG_DIR=/pycastle/auth",
        ),
        (
            "codex",
            ["codex", "login", "--device-auth"],
            ["codex", "login", "status"],
            "CODEX_HOME=/pycastle/auth",
        ),
    ],
)
def test_login_and_status_use_runtime_isolated_auth(
    runtime: str, login: list[str], status: list[str], environment: str
) -> None:
    login_argv = sandbox.build_login_command(runtime, image=IMAGE)
    status_argv = sandbox.build_status_command(runtime, image=IMAGE)
    mount = f"pycastle-{runtime}-auth:/pycastle/auth"
    assert mount in login_argv and mount in status_argv
    assert environment in login_argv and environment in status_argv
    assert login_argv[-len(login) :] == login
    assert status_argv[-len(status) :] == status
    assert login_argv[:3] == status_argv[:3] == ["docker", "run", "--rm"]


def test_environment_is_narrowly_allow_listed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allow-listed"):
        sandbox.build_run_command(
            "codex",
            inner_argv=["true"],
            workspace=tmp_path,
            image=IMAGE,
            environment={"TOKEN": "secret"},
        )


def test_image_is_required(tmp_path: Path) -> None:
    builders = (
        lambda image: sandbox.build_run_command(
            "claude", inner_argv=["true"], workspace=tmp_path, image=image
        ),
        lambda image: sandbox.build_login_command("claude", image=image),
        lambda image: sandbox.build_status_command("claude", image=image),
    )
    for builder in builders:
        with pytest.raises(ValueError, match="immutable"):
            builder(" ")


@pytest.mark.parametrize("inner_argv", [[], "true", [""], ["true", None]])
def test_run_requires_non_empty_string_arguments(
    tmp_path: Path, inner_argv: object
) -> None:
    with pytest.raises(ValueError, match="process argv|arguments"):
        sandbox.build_run_command(
            "claude",
            inner_argv=inner_argv,  # type: ignore[arg-type]
            workspace=tmp_path,
            image=IMAGE,
        )


def test_workdir_must_be_available_through_workspace_mount(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="within the mounted workspace"):
        sandbox.build_run_command(
            "claude",
            inner_argv=["true"],
            workspace=tmp_path / "workspace",
            workdir=tmp_path / "other-worktree",
            image=IMAGE,
        )
