"""Language-neutral Setup/Gate process boundary and bounded evidence."""

from __future__ import annotations

import errno as errno_module
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

HALF_STREAM_LIMIT = 8 * 1024 * 1024
EDGE_TAIL_LIMIT = 16 * 1024


@dataclass(frozen=True)
class CapturedStream:
    first: bytes
    last: bytes
    omitted_bytes: int = 0

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
        return self.retained[-size:]


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
) -> ExecutionRecord:
    """Execute a hook by shebang, with no args and closed standard input."""
    env = dict(environment if environment is not None else os.environ)
    env["PYCASTLE_SCOPE"] = scope
    try:
        process = subprocess.run(
            [str(executable)],
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
        termination: Termination = _termination(process.returncode)
        stdout, stderr = process.stdout, process.stderr
    except OSError as error:
        termination = LaunchError(
            errno_module.errorcode.get(error.errno or 0, type(error).__name__),
            error.errno,
            str(error),
        )
        stdout = stderr = b""
    record = ExecutionRecord(
        str(executable),
        scope,
        termination,
        CapturedStream.from_bytes(stdout),
        CapturedStream.from_bytes(stderr),
    )
    persist_record(record, record_path)
    return record


_CONTROL = re.compile(rb"(?:\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\))")
_CREDENTIAL = re.compile(
    rb"(?i)(authorization\s*:\s*(?:bearer|basic)\s+|(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+"
)


def sanitize_evidence(value: bytes, *, sensitive_values: tuple[str, ...] = ()) -> str:
    cleaned = _CONTROL.sub(b"", value)
    cleaned = _CREDENTIAL.sub(lambda match: match.group(1) + b"[REDACTED]", cleaned)
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
