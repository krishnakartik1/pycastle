"""Preflight checks that required external commands exist before a run."""

from __future__ import annotations

from collections.abc import Iterable

from .commands import command_exists


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
