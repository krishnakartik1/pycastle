"""The Runtime boundary: one interface over Claude Code and Codex.

v0.1 ships a :class:`StubRuntime` that fakes an agent so the loop's plumbing
can be proven end to end. The real Claude adapter, :class:`ClaudeRuntime`,
drives the ``claude`` CLI on the host behind this same interface; the Codex
adapter lands in a later slice.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models import RuntimeResult, Telemetry, TokenUsage

logger = logging.getLogger(__name__)

STUB_MARKER = "PYCASTLE_STUB.md"


class AgentCrashError(RuntimeError):
    """Raised when an agent process exits with a non-zero code.

    The orchestrator catches this to treat a crashed agent as a failed phase
    rather than letting the raw subprocess failure escape. The ``phase`` and
    ``exit_code`` are exposed as attributes so a caller can branch on them
    (retry, skip, escalate) without parsing the message string.
    """

    def __init__(self, message: str, *, phase: str, exit_code: int) -> None:
        """Record the failing phase and the process exit code."""
        super().__init__(message)
        self.phase = phase
        self.exit_code = exit_code


@runtime_checkable
class Runtime(Protocol):
    """Take a prompt plus options and return output text and telemetry."""

    name: str

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        """Execute the agent for one phase and return its parsed result."""
        ...


class StubRuntime:
    """A deterministic fake agent.

    Instead of calling a real CLI it writes a known marker file into the
    working tree, so a run produces a real, reviewable change with no agent
    and no network.
    """

    name = "stub"

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        """Write the marker file and return fixed output and telemetry."""
        marker = cwd / STUB_MARKER
        marker.write_text(
            f"# PyCastle stub runtime\n\nPhase: {phase}\nPrompt bytes: {len(prompt)}\n"
        )
        return RuntimeResult(
            output=f"stub wrote {STUB_MARKER} during {phase}",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


class ClaudeRuntime:
    """Drive the real ``claude`` CLI on the host behind the Runtime interface.

    It builds the ``claude`` command, runs it, and parses the ``stream-json``
    JSONL stream into output text plus a telemetry record. A non-zero agent
    exit (surfaced by the CLI's final ``result`` event or the process exit
    code) is raised as :class:`AgentCrashError`.
    """

    name = "claude"

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        max_turns: int | None = None,
        dangerously_skip_permissions: bool = False,
    ) -> None:
        """Configure the CLI invocation shared across phases."""
        self.command = command
        self.model = model
        self.max_turns = max_turns
        self.dangerously_skip_permissions = dangerously_skip_permissions

    def build_command(self, prompt: str) -> list[str]:
        """Build the ``claude`` argv for a single non-interactive run."""
        cmd = [
            self.command,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.max_turns is not None:
            cmd += ["--max-turns", str(self.max_turns)]
        if self.model is not None:
            cmd += ["--model", self.model]
        if self.dangerously_skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        return cmd

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        """Run the agent for one phase and return its parsed result.

        Raises :class:`AgentCrashError` when the agent exits non-zero.
        """
        cmd = self.build_command(prompt)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            text=True,
        )

        output_buf: list[str] = []
        result_info: dict[str, Any] | None = None
        result_text = ""

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                _collect_assistant_text(event, output_buf)

                if event.get("type") == "result":
                    result_text = event.get("result", "")
                    result_info = _parse_result_event(event)
        finally:
            proc.wait()

        stderr_text = proc.stderr.read() if proc.stderr else ""

        is_error = bool(result_info and result_info["is_error"]) or proc.returncode != 0
        if is_error:
            logger.error(
                "[%s] claude exited with code %s: %s",
                phase,
                proc.returncode,
                stderr_text[:500],
            )
            raise AgentCrashError(
                f"claude crashed during {phase} (exit code {proc.returncode})",
                phase=phase,
                exit_code=proc.returncode,
            )

        output = "".join(output_buf) if output_buf else result_text
        telemetry = _build_telemetry(self.name, phase, result_info)
        return RuntimeResult(output=output, telemetry=telemetry)


def _collect_assistant_text(event: dict[str, Any], output_buf: list[str]) -> None:
    """Append assistant ``text`` blocks from one stream-json event."""
    if event.get("type") != "assistant":
        return
    content = event.get("message", {}).get("content", [])
    for block in content:
        if block.get("type") == "text":
            text = block.get("text", "")
            if text:
                output_buf.append(text)


def _parse_result_event(event: dict[str, Any]) -> dict[str, Any]:
    """Pull cost, duration, turns, usage, and error flag from a result event."""
    return {
        "cost_usd": event.get("total_cost_usd"),
        "duration_ms": event.get("duration_ms"),
        "num_turns": event.get("num_turns"),
        "usage": event.get("usage"),
        "is_error": event.get("is_error", False),
    }


def _build_telemetry(
    runtime: str, phase: str, result_info: dict[str, Any] | None
) -> Telemetry:
    """Turn a parsed result event into a :class:`Telemetry` record."""
    if result_info is None:
        return Telemetry(runtime=runtime, phase=phase)

    usage_raw = result_info.get("usage")
    usage = None
    if isinstance(usage_raw, dict):
        usage = TokenUsage(
            input_tokens=usage_raw.get("input_tokens"),
            output_tokens=usage_raw.get("output_tokens"),
            cache_creation_input_tokens=usage_raw.get("cache_creation_input_tokens"),
            cache_read_input_tokens=usage_raw.get("cache_read_input_tokens"),
        )

    return Telemetry(
        runtime=runtime,
        phase=phase,
        cost_usd=result_info.get("cost_usd"),
        duration_ms=result_info.get("duration_ms"),
        num_turns=result_info.get("num_turns"),
        usage=usage,
        is_error=bool(result_info.get("is_error", False)),
    )


def make_runtime(name: str) -> Runtime:
    """Return the Runtime registered under ``name``.

    The stub and the real Claude adapter exist today; the Codex adapter is
    wired in behind this same factory in a later slice.
    """
    if name == "stub":
        return StubRuntime()
    if name == "claude":
        return ClaudeRuntime()
    if name == "codex":
        raise NotImplementedError(f"The {name!r} runtime lands in a later slice.")
    raise ValueError(f"Unknown runtime: {name!r}")
