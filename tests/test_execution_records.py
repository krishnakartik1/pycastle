import signal
import tempfile
from pathlib import Path

import pytest

from pycastle.execution import (
    HALF_STREAM_LIMIT,
    CapturedStream,
    Exited,
    LaunchError,
    Signaled,
    execute_hook,
    project_gate_evidence,
)


def test_hook_has_no_arguments_closed_stdin_cwd_and_scope(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    hook.write_text(
        "#!/bin/sh\n"
        'printf \'%s|%s|%s\' "$#" "$PWD" "$PYCASTLE_SCOPE"\n'
        "if read value; then exit 9; fi\n"
    )
    hook.chmod(0o755)
    record = execute_hook(
        hook, cwd=tmp_path, scope="item", record_path=tmp_path / "record.json"
    )
    assert record.termination == Exited(0)
    assert record.stdout.retained == f"0|{tmp_path}|item".encode()
    assert (tmp_path / "record.json").is_file()


def test_launch_error_preserves_os_facts_without_exit_code(tmp_path: Path) -> None:
    record = execute_hook(
        tmp_path / "missing",
        cwd=tmp_path,
        scope="run",
        record_path=tmp_path / "record.json",
    )
    assert isinstance(record.termination, LaunchError)
    assert record.termination.errno is not None


def test_signal_is_not_fabricated_as_exit_code(tmp_path: Path) -> None:
    hook = tmp_path / "hook"
    hook.write_text(f"#!/bin/sh\nkill -{signal.SIGTERM} $$\n")
    hook.chmod(0o755)
    record = execute_hook(
        hook, cwd=tmp_path, scope="run", record_path=tmp_path / "record.json"
    )
    assert record.termination == Signaled(signal.SIGTERM)


def test_stream_keeps_first_and_last_eight_mib_and_exact_omission() -> None:
    value = b"a" * HALF_STREAM_LIMIT + b"middle" + b"z" * HALF_STREAM_LIMIT
    stream = CapturedStream.from_bytes(value)
    assert stream.first == b"a" * HALF_STREAM_LIMIT
    assert stream.last == b"z" * HALF_STREAM_LIMIT
    assert stream.omitted_bytes == len(b"middle")


def test_stream_boundary_and_invalid_limits() -> None:
    exact = b"x" * (2 * HALF_STREAM_LIMIT)
    assert CapturedStream.from_bytes(exact).retained == exact
    assert CapturedStream.from_bytes(exact).omitted_bytes == 0
    assert CapturedStream(b"value", b"").tail(0) == b""
    with pytest.raises(ValueError, match="negative"):
        CapturedStream(b"", b"", omitted_bytes=-1)
    with pytest.raises(ValueError, match="negative"):
        CapturedStream(b"value", b"").tail(-1)


def test_file_capture_matches_bytes_capture_across_truncation_boundary() -> None:
    value = b"a" * HALF_STREAM_LIMIT + b"middle" + b"z" * HALF_STREAM_LIMIT
    with tempfile.TemporaryFile() as stream:
        stream.write(value)
        assert CapturedStream.from_file(stream) == CapturedStream.from_bytes(value)


def test_gate_evidence_is_bounded_sanitized_and_path_free(tmp_path: Path) -> None:
    hook = tmp_path / "gate"
    hook.write_bytes(
        b"#!/bin/sh\nprintf '\\033[31mtoken=abc\\033[0m\\377' >&2\nexit 3\n"
    )
    hook.chmod(0o755)
    record = execute_hook(
        hook, cwd=tmp_path, scope="item", record_path=tmp_path / "secret-path"
    )
    evidence = project_gate_evidence(record, node="verify", sensitive_values=("abc",))
    assert evidence["termination"]["code"] == 3
    assert "[REDACTED]" in evidence["stderr"]
    assert "\x1b" not in evidence["stderr"]
    assert "secret-path" not in repr(evidence)
