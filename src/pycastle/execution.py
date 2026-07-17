"""Language-neutral Setup/Gate process boundary and bounded evidence."""

from __future__ import annotations

import errno as errno_module
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, TypeAlias

HALF_STREAM_LIMIT = 8 * 1024 * 1024
EDGE_TAIL_LIMIT = 16 * 1024


@dataclass(frozen=True)
class CapturedStream:
    first: bytes
    last: bytes = b""
    omitted_bytes: int = 0

    def __post_init__(self) -> None:
        if self.omitted_bytes < 0:
            raise ValueError("omitted_bytes must not be negative")
        if len(self.retained) > 2 * HALF_STREAM_LIMIT:
            raise ValueError("captured stream exceeds the 16 MiB retention limit")

    @classmethod
    def from_bytes(cls, value: bytes) -> CapturedStream:
        if len(value) <= 2 * HALF_STREAM_LIMIT:
            return cls(value, b"")
        return cls(
            value[:HALF_STREAM_LIMIT],
            value[-HALF_STREAM_LIMIT:],
            len(value) - 2 * HALF_STREAM_LIMIT,
        )

    @property
    def retained(self) -> bytes:
        return self.first + self.last

    def tail(self, size: int = EDGE_TAIL_LIMIT) -> bytes:
        if size < 0:
            raise ValueError("tail size must not be negative")
        if size == 0:
            return b""
        return self.retained[-size:]

    @classmethod
    def from_file(cls, stream: BinaryIO) -> CapturedStream:
        """Capture the bounded first/final stream regions from a binary file."""
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        if size <= 2 * HALF_STREAM_LIMIT:
            return cls(stream.read())
        first = stream.read(HALF_STREAM_LIMIT)
        stream.seek(-HALF_STREAM_LIMIT, os.SEEK_END)
        return cls(
            first,
            stream.read(HALF_STREAM_LIMIT),
            size - 2 * HALF_STREAM_LIMIT,
        )


@dataclass(frozen=True)
class Exited:
    code: int
    kind: Literal["exited"] = "exited"


@dataclass(frozen=True)
class Signaled:
    signal: int
    kind: Literal["signaled"] = "signaled"


@dataclass(frozen=True)
class LaunchError:
    error_kind: str
    errno: int | None
    message: str
    kind: Literal["launch_error"] = "launch_error"


Termination: TypeAlias = Exited | Signaled | LaunchError


@dataclass(frozen=True)
class ExecutionRecord:
    executable: str
    scope: Literal["item", "run"]
    termination: Termination
    stdout: CapturedStream
    stderr: CapturedStream

    @property
    def success(self) -> bool:
        return isinstance(self.termination, Exited) and self.termination.code == 0


def _termination(returncode: int) -> Termination:
    if returncode < 0:
        return Signaled(-returncode)
    return Exited(returncode)


def persist_record(record: ExecutionRecord, destination: Path) -> None:
    """Atomically persist one immutable record before control may advance."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "executable": record.executable,
        "scope": record.scope,
        "termination": record.termination.__dict__,
        "stdout": {
            "first": record.stdout.first.hex(),
            "last": record.stdout.last.hex(),
            "omitted_bytes": record.stdout.omitted_bytes,
        },
        "stderr": {
            "first": record.stderr.first.hex(),
            "last": record.stderr.last.hex(),
            "omitted_bytes": record.stderr.omitted_bytes,
        },
    }
    fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".record-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def execute_hook(
    executable: Path,
    *,
    cwd: Path,
    scope: Literal["item", "run"],
    record_path: Path,
    environment: dict[str, str] | None = None,
    argv_builder: (
        Callable[[Path, Path, Literal["item", "run"]], list[str]] | None
    ) = None,
) -> ExecutionRecord:
    """Execute a hook by shebang, with no args and closed standard input."""
    env = dict(environment if environment is not None else os.environ)
    env["PYCASTLE_SCOPE"] = scope
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            argv = (
                argv_builder(executable, cwd, scope)
                if argv_builder is not None
                else [str(executable)]
            )
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=None if argv_builder is not None else env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            termination: Termination = _termination(process.wait())
        except OSError as error:
            termination = LaunchError(
                errno_module.errorcode.get(error.errno or 0, type(error).__name__),
                error.errno,
                str(error),
            )
        stdout = CapturedStream.from_file(stdout_file)
        stderr = CapturedStream.from_file(stderr_file)
    record = ExecutionRecord(
        str(executable),
        scope,
        termination,
        stdout,
        stderr,
    )
    persist_record(record, record_path)
    return record


_CONTROL = re.compile(
    rb"(?:"
    rb"\x1b(?:"
    rb"\[[0-?]*[ -/]*[@-~]"  # CSI
    rb"|\][^\x07\x1b]*(?:\x07|\x1b\\|$)"  # OSC
    rb"|[PX^_][^\x1b]*(?:\x1b\\|$)"  # DCS, SOS, PM, APC
    rb"|[@-_]"  # two-byte escape sequences
    rb")"
    rb"|\x9b[0-?]*[ -/]*[@-~]"  # eight-bit CSI
    rb")"
)
_CREDENTIAL = re.compile(
    rb"(?i)(authorization\s*:\s*(?:bearer|basic)\s+|(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+"
)
_KNOWN_CREDENTIAL = re.compile(
    rb"(?<![A-Za-z0-9_])(?:"
    rb"gh[pousr]_[A-Za-z0-9]{20,}"
    rb"|github_pat_[A-Za-z0-9_]{20,}"
    rb"|AKIA[0-9A-Z]{16}"
    rb"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    rb")(?![A-Za-z0-9_])"
)
_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?i)(?:^|_)(?:auth(?:entication|orization)?|credentials?|key|pass(?:word|wd)|secret|token|cookie|jwt)(?:_|$)"
)


def sensitive_environment_values(environment: dict[str, str]) -> tuple[str, ...]:
    """Select deterministic, non-empty secrets from a process environment."""
    return tuple(
        sorted(
            {
                value
                for name, value in environment.items()
                if _SENSITIVE_ENVIRONMENT_NAME.search(name) and value
            }
        )
    )


def sanitize_evidence(value: bytes, *, sensitive_values: tuple[str, ...] = ()) -> str:
    cleaned = _CONTROL.sub(b"", value)
    cleaned = _CREDENTIAL.sub(lambda match: match.group(1) + b"[REDACTED]", cleaned)
    cleaned = _KNOWN_CREDENTIAL.sub(b"[REDACTED]", cleaned)
    text = cleaned.decode("utf-8", errors="replace")
    for secret in sorted((x for x in sensitive_values if x), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def project_gate_evidence(
    record: ExecutionRecord,
    *,
    node: str,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object]:
    """Create bounded, sanitized, path-free immediate Gate evidence."""
    return {
        "source": node,
        "success": record.success,
        "termination": record.termination.__dict__,
        "stdout": sanitize_evidence(
            record.stdout.tail(), sensitive_values=sensitive_values
        ),
        "stderr": sanitize_evidence(
            record.stderr.tail(), sensitive_values=sensitive_values
        ),
    }
