"""Subprocess helpers shared across PyCastle modules."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

MAX_CAPTURE_BYTES = 64 * 1024


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate and reap a runner-owned child and any descendants."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    # The group may still contain descendants after its leader exits.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _bounded_reader(pipe: object, captured: bytearray) -> None:
    read = getattr(pipe, "read")
    try:
        while chunk := read(8192):
            remaining = MAX_CAPTURE_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
    finally:
        getattr(pipe, "close")()


def run_cmd(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded child process, retaining at most a small diagnostic."""
    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        start_new_session=True,
    )
    stdout = bytearray()
    stderr = bytearray()
    threads: list[threading.Thread] = []
    if capture:
        assert process.stdout is not None and process.stderr is not None
        threads = [
            threading.Thread(target=_bounded_reader, args=(process.stdout, stdout)),
            threading.Thread(target=_bounded_reader, args=(process.stderr, stderr)),
        ]
        for thread in threads:
            thread.start()
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        for thread in threads:
            thread.join()
        raise subprocess.TimeoutExpired(
            list(args),
            timeout,
            output=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        ) from None
    except BaseException:
        _terminate_process_group(process)
        for thread in threads:
            thread.join()
        raise
    for thread in threads:
        remaining = (
            max(0.0, deadline - time.monotonic()) if deadline is not None else None
        )
        thread.join(remaining)
    if any(thread.is_alive() for thread in threads):
        _terminate_process_group(process)
        for thread in threads:
            thread.join()
        raise subprocess.TimeoutExpired(
            list(args),
            timeout,
            output=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
    return subprocess.CompletedProcess(
        list(args),
        process.returncode,
        stdout.decode(errors="replace") if capture else None,
        stderr.decode(errors="replace") if capture else None,
    )


def command_exists(name: str) -> bool:
    """Return True if an executable named ``name`` is on PATH."""
    return shutil.which(name) is not None
