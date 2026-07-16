"""Pure Docker Sandbox command builders.

The project-owned image is already pinned before these builders are called.
Every invocation is a fresh container which keeps only the repository workspace
and the selected Runtime's authentication volume.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

AUTH_DIR = "/pycastle/auth"


@dataclass(frozen=True)
class RuntimeSandboxConfig:
    cli: str
    config_dir: str
    config_env: str
    login_args: tuple[str, ...]
    status_args: tuple[str, ...]


RUNTIME_CONFIG: dict[str, RuntimeSandboxConfig] = {
    "claude": RuntimeSandboxConfig(
        "claude",
        AUTH_DIR,
        "CLAUDE_CONFIG_DIR",
        ("claude", "auth", "login", "--claudeai"),
        ("claude", "auth", "status"),
    ),
    "codex": RuntimeSandboxConfig(
        "codex",
        AUTH_DIR,
        "CODEX_HOME",
        ("codex", "login", "--device-auth"),
        ("codex", "login", "status"),
    ),
}


def _config_for(runtime_name: str) -> RuntimeSandboxConfig:
    try:
        return RUNTIME_CONFIG[runtime_name]
    except KeyError:
        raise ValueError(
            f"No Docker convention for Runtime: {runtime_name!r}"
        ) from None


def auth_volume(runtime_name: str) -> str:
    _config_for(runtime_name)
    return f"pycastle-{runtime_name}-auth"


def _validate_image(image: str) -> None:
    if not isinstance(image, str) or not image.strip():
        raise ValueError("An immutable Agent image identity is required")


def _validate_inner_argv(inner_argv: Sequence[str]) -> None:
    if isinstance(inner_argv, str) or not inner_argv:
        raise ValueError("A non-empty container process argv is required")
    if any(not isinstance(argument, str) or not argument for argument in inner_argv):
        raise ValueError("Container process arguments must be non-empty strings")


def _environment_args(
    runtime_name: str, environment: Mapping[str, str] | None = None
) -> list[str]:
    config = _config_for(runtime_name)
    values = {config.config_env: AUTH_DIR}
    for name, value in (environment or {}).items():
        if name != "PYCASTLE_SCOPE":
            raise ValueError(f"Docker environment value is not allow-listed: {name}")
        if value not in {"item", "run"}:
            raise ValueError("PYCASTLE_SCOPE must be 'item' or 'run'")
        values[name] = value
    result: list[str] = []
    for name, value in values.items():
        result.extend(("-e", f"{name}={value}"))
    return result


def build_run_command(
    runtime_name: str,
    *,
    inner_argv: Sequence[str],
    workspace: Path,
    image: str,
    workdir: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Wrap one process in a disposable container using a pinned image."""
    _validate_image(image)
    _validate_inner_argv(inner_argv)
    workspace_resolved = Path(workspace).resolve()
    workdir_resolved = (
        Path(workdir).resolve() if workdir is not None else workspace_resolved
    )
    if not workdir_resolved.is_relative_to(workspace_resolved):
        raise ValueError("Docker workdir must be within the mounted workspace")
    workspace_path = str(workspace_resolved)
    workdir_path = str(workdir_resolved)
    return [
        "docker",
        "run",
        "--rm",
        "-w",
        workdir_path,
        "-v",
        f"{workspace_path}:{workspace_path}",
        "-v",
        f"{auth_volume(runtime_name)}:{AUTH_DIR}",
        *_environment_args(runtime_name, environment),
        image,
        *inner_argv,
    ]


def build_login_command(runtime_name: str, *, image: str) -> list[str]:
    _validate_image(image)
    config = _config_for(runtime_name)
    tty = ["-it"] if runtime_name == "claude" else []
    return [
        "docker",
        "run",
        "--rm",
        *tty,
        "-v",
        f"{auth_volume(runtime_name)}:{AUTH_DIR}",
        *_environment_args(runtime_name),
        image,
        *config.login_args,
    ]


def build_status_command(runtime_name: str, *, image: str) -> list[str]:
    _validate_image(image)
    config = _config_for(runtime_name)
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{auth_volume(runtime_name)}:{AUTH_DIR}",
        *_environment_args(runtime_name),
        image,
        *config.status_args,
    ]
