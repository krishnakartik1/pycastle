"""The Runtime boundary: one interface over Claude Code and Codex.

v0.1 ships a :class:`StubRuntime` that fakes an agent so the loop's plumbing
can be proven end to end. The real Claude adapter, :class:`ClaudeRuntime`,
drives the ``claude`` CLI; the Codex adapter, :class:`CodexRuntime`, drives the
``codex`` CLI. Both run on the host or inside the Docker sandbox behind this
same interface, so switching runtime is a flag, not a code change.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import sandbox
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
    """Drive the real ``claude`` CLI behind the Runtime interface.

    It builds the ``claude`` command, runs it, and parses the ``stream-json``
    JSONL stream into output text plus a telemetry record. A non-zero agent
    exit (surfaced by the CLI's final ``result`` event or the process exit
    code) is raised as :class:`AgentCrashError`.

    By default the ``claude`` CLI runs on the host. Pass ``argv_wrapper`` (or
    construct via :meth:`in_docker`) to wrap the inner ``claude …`` argv before
    it is launched — for example, into a ``docker run`` argv so both the
    Runtime and the commands it invokes run inside the agent sandbox. The
    wrapper changes only how the process is launched; the same stream-json
    parsing applies to its stdout either way.
    """

    name = "claude"

    def __init__(
        self,
        *,
        command: str = "claude",
        model: str | None = None,
        max_turns: int | None = None,
        dangerously_skip_permissions: bool = False,
        argv_wrapper: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        """Configure the CLI invocation shared across phases."""
        self.command = command
        self.model = model
        self.max_turns = max_turns
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.argv_wrapper = argv_wrapper

    @classmethod
    def in_docker(
        cls,
        *,
        workspace: Path,
        image: str = sandbox.DEFAULT_IMAGE,
        model: str | None = None,
        max_turns: int | None = None,
        dangerously_skip_permissions: bool = False,
    ) -> ClaudeRuntime:
        """Build a Claude runtime that runs each phase inside the Docker sandbox.

        The inner ``claude …`` argv is wrapped into a ``docker run`` argv (see
        :func:`pycastle.sandbox.build_run_command`) so the agent runs as
        non-root ``node`` against the per-Runtime auth volume, with
        ``workspace`` bind-mounted so it can read and write the real tree.
        """

        def wrap(inner_argv: list[str]) -> list[str]:
            return sandbox.build_run_command(
                cls.name,
                inner_argv=inner_argv,
                workspace=workspace,
                image=image,
            )

        return cls(
            model=model,
            max_turns=max_turns,
            dangerously_skip_permissions=dangerously_skip_permissions,
            argv_wrapper=wrap,
        )

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
        if self.argv_wrapper is not None:
            cmd = self.argv_wrapper(cmd)
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


#: The flag that lets Codex write inside the Docker sandbox. Codex's own
#: bubblewrap sandbox cannot nest inside Docker (ADR-0003), so the in-container
#: run bypasses approvals and Codex's sandbox and relies on Docker for
#: isolation. It lives on the *runtime's* inner argv, not the docker wrapper, so
#: the wrapper stays runtime-agnostic.
CODEX_DOCKER_BYPASS = "--dangerously-bypass-approvals-and-sandbox"

#: The sandbox mode Codex runs under on the host. Codex's default sandbox is
#: read-only, so a host run would silently reject every patch ("writing is
#: blocked by read-only sandbox"). ``workspace-write`` scopes writes to the
#: per-item git worktree (the cwd) without the full Docker bypass, which stays
#: reserved for the container where Docker is the isolation boundary (ADR-0003).
#: It is passed as ``-s workspace-write`` before the ``exec`` subcommand, the
#: same spot the bypass occupies, so the two paths stay symmetric.
CODEX_HOST_SANDBOX = "workspace-write"


class CodexRuntime:
    """Drive the real ``codex`` CLI behind the Runtime interface.

    It builds the ``codex exec`` command, runs it, and parses the Codex JSONL
    event stream (``thread.started`` → thread id; ``item.completed`` with an
    ``agent_message`` → output text; ``turn.completed`` → token usage) into
    output text plus a telemetry record. A non-zero process exit is raised as
    :class:`AgentCrashError`.

    Codex reports no cost or duration, so :attr:`Telemetry.duration_ms` stays
    ``None`` and PyCastle's own measured wall time is recorded as
    ``elapsed_ms``. The thread id from ``thread.started`` is surfaced as
    :attr:`Telemetry.thread_id` and is the handle a later handoff resumes via
    :meth:`run` with ``resume_thread_id``.

    Like :class:`ClaudeRuntime`, an ``argv_wrapper`` (or :meth:`in_docker`)
    wraps the inner ``codex …`` argv before launch — for the Docker sandbox,
    into a ``docker run`` argv. When sandboxed in Docker the inner argv carries
    :data:`CODEX_DOCKER_BYPASS` so Codex can write inside the container; on the
    host the inner argv carries ``-s`` :data:`CODEX_HOST_SANDBOX` so Codex may
    write within the worktree instead of falling back to its read-only default.
    """

    name = "codex"

    def __init__(
        self,
        *,
        command: str = "codex",
        model: str | None = None,
        bypass_sandbox: bool = False,
        argv_wrapper: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        """Configure the CLI invocation shared across phases.

        ``bypass_sandbox`` swaps the scoped host sandbox (``-s``
        :data:`CODEX_HOST_SANDBOX`) for the full :data:`CODEX_DOCKER_BYPASS`;
        :meth:`in_docker` sets it so Codex can write inside the container.
        """
        self.command = command
        self.model = model
        self.bypass_sandbox = bypass_sandbox
        self.argv_wrapper = argv_wrapper

    @classmethod
    def in_docker(
        cls,
        *,
        workspace: Path,
        image: str = sandbox.DEFAULT_IMAGE,
        model: str | None = None,
    ) -> CodexRuntime:
        """Build a Codex runtime that runs each phase inside the Docker sandbox.

        The inner ``codex …`` argv (carrying :data:`CODEX_DOCKER_BYPASS`) is
        wrapped into a ``docker run`` argv (see
        :func:`pycastle.sandbox.build_run_command`) so the agent runs as
        non-root ``node`` against the Codex auth volume with ``CODEX_HOME``
        pinned, and ``workspace`` bind-mounted so it can read and write the real
        tree.
        """

        def wrap(inner_argv: list[str]) -> list[str]:
            return sandbox.build_run_command(
                cls.name,
                inner_argv=inner_argv,
                workspace=workspace,
                image=image,
            )

        return cls(model=model, bypass_sandbox=True, argv_wrapper=wrap)

    def build_command(
        self, prompt: str, *, cwd: Path, resume_thread_id: str | None = None
    ) -> list[str]:
        """Build the ``codex exec`` argv for one non-interactive run.

        Without ``resume_thread_id`` this starts a fresh thread
        (``codex … exec --json <prompt>``); with one it resumes that thread
        (``codex … exec resume --json <thread_id> <prompt>``) so a handoff
        continues the conversation that did the failed attempt.

        ``cwd`` is resolved to an absolute path for the ``-C`` value. Codex
        resolves a relative ``-C`` against its own process working directory,
        which :meth:`run` also sets to ``cwd``; a relative value would double
        (e.g. ``…/issue-3/.pycastle/worktrees/issue-3``) and fail. Resolving
        here mirrors :func:`pycastle.sandbox.build_run_command`, which resolves
        the bind-mount source for the same reason.

        The sandbox argument sits before ``exec``: a Docker run carries
        :data:`CODEX_DOCKER_BYPASS`; a host run carries ``-s``
        :data:`CODEX_HOST_SANDBOX` so Codex may write within the worktree rather
        than using its read-only default. The two are mutually exclusive, so a
        host run never gets the bypass and a Docker run never gets ``-s``.
        """
        cmd = [self.command, "-C", str(Path(cwd).resolve())]
        if self.model is not None:
            cmd += ["--model", self.model]
        if self.bypass_sandbox:
            cmd.append(CODEX_DOCKER_BYPASS)
        else:
            cmd += ["-s", CODEX_HOST_SANDBOX]
        cmd.append("exec")
        if resume_thread_id is not None:
            cmd.append("resume")
        cmd.append("--json")
        if resume_thread_id is not None:
            cmd.append(resume_thread_id)
        cmd.append(prompt)
        return cmd

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        phase: str,
        resume_thread_id: str | None = None,
    ) -> RuntimeResult:
        """Run the agent for one phase and return its parsed result.

        Pass ``resume_thread_id`` to continue a prior thread (used for
        handoffs). Raises :class:`AgentCrashError` when the agent exits
        non-zero.

        ``cwd`` is resolved to an absolute path once so the ``-C`` value and the
        subprocess working directory agree; otherwise Codex would re-resolve a
        relative ``-C`` against the process cwd and double the path.
        """
        cwd = Path(cwd).resolve()
        cmd = self.build_command(prompt, cwd=cwd, resume_thread_id=resume_thread_id)
        if self.argv_wrapper is not None:
            cmd = self.argv_wrapper(cmd)
        started_at = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            text=True,
        )

        output_buf: list[str] = []
        thread_id: str | None = None
        usage: dict[str, Any] = {}
        num_turns = 0

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

                event_type = event.get("type")
                if event_type == "thread.started":
                    thread_id = event.get("thread_id")
                elif event_type == "item.completed":
                    _collect_codex_item(event.get("item", {}), output_buf)
                elif event_type == "turn.completed":
                    num_turns += 1
                    event_usage = event.get("usage")
                    if isinstance(event_usage, dict):
                        usage = event_usage
        finally:
            proc.wait()

        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        stderr_text = proc.stderr.read() if proc.stderr else ""

        if proc.returncode != 0:
            logger.error(
                "[%s] codex exited with code %s: %s",
                phase,
                proc.returncode,
                stderr_text[:500],
            )
            raise AgentCrashError(
                f"codex crashed during {phase} (exit code {proc.returncode})",
                phase=phase,
                exit_code=proc.returncode,
            )

        telemetry = _build_codex_telemetry(
            phase,
            thread_id=thread_id,
            usage=usage,
            num_turns=num_turns,
            elapsed_ms=elapsed_ms,
        )
        return RuntimeResult(output="".join(output_buf), telemetry=telemetry)


def _collect_codex_item(item: object, output_buf: list[str]) -> None:
    """Append the text of an ``agent_message`` Codex item, ignoring the rest.

    Codex narrates tool runs and file changes as their own ``item.completed``
    events; only ``agent_message`` items are the model's prose output, so the
    verbose command/file-change items are dropped. A malformed event whose
    ``item`` is not a mapping contributes nothing rather than crashing the parse.
    """
    if not isinstance(item, dict):
        return
    if item.get("type") == "agent_message":
        text = item.get("text", "")
        if text:
            output_buf.append(text)


def _build_codex_telemetry(
    phase: str,
    *,
    thread_id: str | None,
    usage: dict[str, Any],
    num_turns: int,
    elapsed_ms: int,
) -> Telemetry:
    """Turn parsed Codex stream fields into a :class:`Telemetry` record.

    Codex's usage vocabulary maps onto :class:`TokenUsage` as: ``input_tokens``
    and ``output_tokens`` pass through; ``cached_input_tokens`` is the cache-read
    count (Codex has no separate cache-creation count); ``reasoning_output_tokens``
    records tokens spent on hidden reasoning. Cost and duration are unavailable
    from Codex, so they stay ``None`` and ``elapsed_ms`` carries the measured
    wall time.
    """
    token_usage = None
    if usage:
        token_usage = TokenUsage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cached_input_tokens"),
            reasoning_output_tokens=usage.get("reasoning_output_tokens"),
        )
    return Telemetry(
        runtime=CodexRuntime.name,
        phase=phase,
        cost_usd=None,
        duration_ms=None,
        elapsed_ms=elapsed_ms,
        num_turns=num_turns,
        usage=token_usage,
        thread_id=thread_id,
        is_error=False,
    )


def make_runtime(name: str) -> Runtime:
    """Return the Runtime registered under ``name``.

    The stub, the real Claude adapter, and the real Codex adapter all exist
    today, all behind this same factory.
    """
    if name == "stub":
        return StubRuntime()
    if name == "claude":
        return ClaudeRuntime()
    if name == "codex":
        return CodexRuntime()
    raise ValueError(f"Unknown runtime: {name!r}")
