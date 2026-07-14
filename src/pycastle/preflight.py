"""Preflight checks that required external commands exist before a run."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from . import sandbox
from .commands import command_exists, run_cmd


class PreflightError(RuntimeError):
    """Raised when one or more required external commands are missing."""


def check_required_commands(commands: Iterable[str]) -> None:
    """Raise :class:`PreflightError` naming any command not found on PATH.

    Fail fast here so a run never dies halfway through for want of ``git`` or
    ``gh``.
    """
    missing = [name for name in commands if not command_exists(name)]
    if missing:
        raise PreflightError(
            "Required command(s) not found on PATH: " + ", ".join(missing)
        )


def check_docker_gate_toolchain(
    fixture_dir: Path,
    *,
    image: str,
    runtime_name: str,
    workspace: Path,
    runner: Callable[..., Any] = run_cmd,
) -> None:
    """Verify the Project gate's tool lookups in the resolved agent image.

    A gate opts into this one-time Docker preflight by accepting
    ``--check-tools``. The project-owned script remains the authority on which
    commands constitute its gate toolchain; PyCastle only launches that mode in
    the same image and mount layout the phases and gate use. A fixture without a
    gate continues to opt out of gating entirely.
    """
    gate = fixture_dir / "gate"
    if not gate.is_file():
        return

    argv = sandbox.build_run_command(
        runtime_name,
        inner_argv=["bash", str(gate.resolve()), "--check-tools"],
        workspace=workspace,
        workdir=workspace,
        image=image,
    )
    try:
        result = runner(argv, capture=True)
    except OSError as exc:
        raise PreflightError(
            f"Could not check the gate toolchain in agent image {image}: {exc}"
        ) from exc

    if getattr(result, "returncode", 1) == 0:
        return

    output = (getattr(result, "stdout", "") or "") + (
        getattr(result, "stderr", "") or ""
    )
    detail = output.strip() or "The gate's --check-tools mode exited non-zero."
    raise PreflightError(
        f"Agent image {image} does not satisfy the gate toolchain preflight.\n"
        f"{detail}\n"
        "Add the missing tools to .pycastle/Dockerfile and rebuild, or fix the "
        "image supplied with --image, then retry the run."
    )
