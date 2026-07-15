"""Subprocess helpers shared across PyCastle modules."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path


def run_cmd(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command as text, optionally capturing its output.

    A thin wrapper over :func:`subprocess.run` so every call site shares the
    same defaults and is easy to mock in tests.
    """
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=False,
        timeout=timeout,
    )


def command_exists(name: str) -> bool:
    """Return True if an executable named ``name`` is on PATH."""
    return shutil.which(name) is not None
