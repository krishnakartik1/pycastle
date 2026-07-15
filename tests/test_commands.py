from __future__ import annotations

import subprocess
import sys
import time

import pytest

from pycastle.commands import MAX_CAPTURE_BYTES, run_cmd


def test_run_cmd_bounds_captured_stdout_and_stderr() -> None:
    size = MAX_CAPTURE_BYTES * 4
    result = run_cmd(
        [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write('o'*{size}); sys.stderr.write('e'*{size})",
        ],
        capture=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert len(result.stdout) <= MAX_CAPTURE_BYTES
    assert len(result.stderr) <= MAX_CAPTURE_BYTES


def test_run_cmd_times_out_and_reaps_a_hanging_child() -> None:
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired) as exc_info:
        run_cmd(
            [sys.executable, "-c", "import time; print('unsafe'); time.sleep(30)"],
            capture=True,
            timeout=0.1,
        )

    assert time.monotonic() - started < 3
    assert len(exc_info.value.output or "") <= MAX_CAPTURE_BYTES


def test_run_cmd_timeout_terminates_descendants_holding_capture_pipes() -> None:
    script = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print('parent done')"
    )
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        run_cmd([sys.executable, "-c", script], capture=True, timeout=0.1)

    assert time.monotonic() - started < 3


def test_run_cmd_propagates_child_failure_without_raising() -> None:
    result = run_cmd(
        [sys.executable, "-c", "raise SystemExit(23)"], capture=True, timeout=5
    )

    assert result.returncode == 23
