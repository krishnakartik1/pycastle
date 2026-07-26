"""The run lifecycle: turn a batch of ready issues into one pull request.

A run selects up to N ready, appropriately-assigned issues and works them as a
bounded batch. It cuts a per-run branch in its own worktree (the main checkout
stays untouched), then works each issue in its own worktree off that run branch:
run the graph, commit, and on a clean merge fold the issue branch back into the
run branch. Successful issues are accumulated and a single pull request is opened
for the whole run. Per-node provider telemetry and a run log are written into
the Project fixture under ``.pycastle/runs/<run_id>/`` (an ignored path, so run
output is never committed).

A merge that does not apply cleanly does not fail the run (#9): the conflicting
issue is labelled ``ready-for-human`` and the run continues with the remaining
items. An interrupt (SIGINT) while an issue is in flight unwinds through a
cleanup-and-restore path that removes the run's worktrees and releases the
claimed issue back to ``ready-for-agent``, so a cancelled run leaves no orphaned
worktrees and no issue stuck in a claimed state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal

from . import sandbox as sandbox_mod
from . import selection
from .commands import MAX_CAPTURE_BYTES, run_cmd
from .execution import (
    ExecutionRecord,
    execute_hook,
    project_gate_evidence,
    sensitive_environment_values,
)
from .graph import (
    HUMAN,
    ExecutionGraph,
    GateNode,
    NodeOutcome,
    NodeVisit,
    RuntimeNode,
    Terminal,
    walk_execution_graph,
)
from .issues import IssueSource
from .models import IssueRef, RuntimeResult, Telemetry
from .readiness import FrozenReadinessInputs
from .runtime import AgentCrashError, Runtime
from .selection import ItemSelectionError, SelectionEnd, SelectionFailure

logger = logging.getLogger(__name__)

Runner = Callable[..., Any]
ITEM_SELECTION_NODE = selection.ITEM_SELECTION_NODE
SELECTION_REASON_LIMIT = selection.SELECTION_REASON_LIMIT
SELECTION_RESPONSE_LIMIT_BYTES = selection.SELECTION_RESPONSE_LIMIT_BYTES
SELECTION_CANDIDATE_ENVELOPE_LIMIT_BYTES = (
    selection.SELECTION_CANDIDATE_ENVELOPE_LIMIT_BYTES
)
_parse_item_selection = selection.parse_response
_render_item_candidate_envelope = selection.render_candidate_envelope
_render_item_selection_prompt = selection.render_prompt
_without_github_credentials_for_selection = selection.without_github_credentials
_write_item_selection_record = selection.write_audit_record


@dataclass(frozen=True)
class NodeResult:
    node: str
    result: RuntimeResult


@dataclass
class ExecutionResult:
    results: list[NodeResult]
    terminal: Terminal


class PromptRenderer:
    """Render a frozen Runtime-node prompt without executing graph policy."""

    def __init__(self, *, prompts: Mapping[str, str], preamble: str = "") -> None:
        self.prompts = prompts
        self.preamble = preamble

    def render_prompt(self, node: RuntimeNode, extra: str | None = None) -> str:
        try:
            prompt = self.prompts[node.prompt]
        except KeyError:
            raise ValueError(
                f"Frozen Runtime prompt is missing: {node.prompt!r}"
            ) from None
        return "\n\n".join(value for value in (self.preamble, prompt, extra) if value)


class WorktreeError(RuntimeError):
    """A ``git worktree add`` exited non-zero and created no worktree.

    Every git call here runs with ``check=False``, so a failing
    ``git worktree add`` (a path collision, a checkout conflict, no disk) was
    silently ignored and the run drove the Runtime against a worktree that never
    existed — cascading into confusing downstream errors (#64). Raised instead so
    the failure surfaces at its source, carrying git's captured output.
    """


class SelectionWorktreeCleanupError(WorktreeError):
    """Selection cleanup failed after PyCastle captured a durable checkpoint."""

    def __init__(self, message: str, *, expected_commit: str) -> None:
        super().__init__(message)
        self.expected_commit = expected_commit


class BranchError(RuntimeError):
    """A prerequisite ``git branch`` exited non-zero.

    Branch creation must succeed before its worktree is added. Otherwise a stale
    branch with the requested name can be checked out successfully from the wrong
    base, recreating the wrong-tree failure that worktree validation prevents.
    """


class PruneError(RuntimeError):
    """Run-branch discovery or deletion failed, making pruning unsafe."""


class RunCheckpointError(RuntimeError):
    """A successful Run node could not be committed durably."""


@dataclass(frozen=True)
class IgnoredPublicationArtifact:
    """One ignored file whose contents can affect Run publication."""

    kind: Literal["file", "symlink"]
    content: bytes | str
    mode: int | None = None


@dataclass(frozen=True)
class DurableRunCheckpoint:
    """Git-visible state plus ignored publication state for a Run worktree."""

    commit: str
    ignored_publication: tuple[tuple[str, IgnoredPublicationArtifact], ...]


class ItemClaimError(RuntimeError):
    """The Issue source did not confirm ownership of a selected Item."""


def prune_run_branches(
    *,
    repo: str,
    cwd: Path,
    include_no_pr: bool = False,
    runner: Runner = run_cmd,
) -> list[str]:
    """Delete remote Run branches whose pull requests are no longer open.

    Pull-request history is resolved before any remote branches are considered.
    Branches with no associated pull request are recovery artifacts and are
    retained unless ``include_no_pr`` is true. Branches with an open pull
    request are always retained.
    If either discovery call fails, pruning stops without deleting anything so
    an unavailable GitHub API can never make an open PR branch look stale.
    """
    open_prs = runner(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "all",
            "--limit",
            "10000",
            "--json",
            "headRefName,state",
        ],
        capture=True,
    )
    if getattr(open_prs, "returncode", 1) != 0:
        raise PruneError("Could not list open pull requests; no branches were deleted.")
    try:
        pr_data = json.loads(open_prs.stdout)
    except (AttributeError, json.JSONDecodeError, TypeError):
        raise PruneError(
            "Could not parse open pull requests; no branches were deleted."
        ) from None
    if not isinstance(pr_data, list) or any(
        not isinstance(pr, dict)
        or not isinstance(pr.get("headRefName"), str)
        or not pr["headRefName"]
        or pr.get("state") not in {"OPEN", "CLOSED", "MERGED"}
        for pr in pr_data
    ):
        raise PruneError(
            "Could not parse open pull requests; no branches were deleted."
        )
    associated_heads = {pr["headRefName"] for pr in pr_data}
    open_heads = {pr["headRefName"] for pr in pr_data if pr.get("state") == "OPEN"}

    refs = runner(
        ["git", "ls-remote", "--heads", "origin", "refs/heads/pycastle/run-*"],
        capture=True,
        cwd=cwd,
    )
    if getattr(refs, "returncode", 1) != 0:
        raise PruneError(
            "Could not list remote run branches; no branches were deleted."
        )

    refs_stdout = getattr(refs, "stdout", None)
    if not isinstance(refs_stdout, str):
        raise PruneError(
            "Could not parse remote run branches; no branches were deleted."
        )
    ref_lines = refs_stdout.splitlines()

    ref_prefix = "refs/heads/pycastle/run-"
    remote_branches: list[str] = []
    seen_branches: set[str] = set()
    for line in ref_lines:
        fields = line.split("\t")
        branch = fields[1].removeprefix("refs/heads/") if len(fields) == 2 else ""
        if (
            len(fields) != 2
            or not fields[0]
            or fields[1] == ref_prefix
            or not fields[1].startswith(ref_prefix)
            or branch in seen_branches
        ):
            raise PruneError(
                "Could not parse remote run branches; no branches were deleted."
            )
        remote_branches.append(branch)
        seen_branches.add(branch)
    stale = [
        branch
        for branch in remote_branches
        if branch not in open_heads and (branch in associated_heads or include_no_pr)
    ]
    deleted: list[str] = []
    for branch in stale:
        result = runner(
            ["git", "push", "origin", "--delete", branch],
            capture=True,
            cwd=cwd,
        )
        if getattr(result, "returncode", 1) != 0:
            raise PruneError(f"Could not delete remote run branch {branch}.")
        deleted.append(branch)
    return deleted


@dataclass
class GateOutcome:
    """The safe publication facts produced by the most recent Gate node."""

    passed: bool
    output: str
    duration_seconds: float = 0.0
    command: str = ".pycastle/gate"
    termination: dict[str, object] | None = None


#: Mandatory project-owned executables relative to the Project fixture.
FIXTURE_GATE = "gate"
FIXTURE_SETUP = "setup"


@dataclass(frozen=True)
class SetupFailure:
    """Allow-listed Setup facts safe to expose outside the local record."""

    command: str
    termination: dict[str, object]


class SetupError(RuntimeError):
    """The frozen Setup prerequisite was not durably established."""

    def __init__(self, failure: SetupFailure, message: str = "Setup failed") -> None:
        if not isinstance(failure, SetupFailure):
            raise TypeError("failure must be a SetupFailure")
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class FrozenRunExecution:
    """Frozen Run hooks and deterministic per-graph visit record allocation."""

    setup: Path
    gate: Path
    records: Path
    setup_content: bytes = field(repr=False)
    setup_mode: int
    gate_content: bytes = field(repr=False)
    gate_mode: int
    prompts: Mapping[str, str] = field(repr=False)
    sandbox: str = "host"
    runtime_name: str = "claude"
    workspace: Path | None = None
    image: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "prompts", MappingProxyType(dict(self.prompts)))

    def _hook_argv(
        self, executable: Path, cwd: Path, scope: Literal["item", "run"]
    ) -> list[str]:
        if self.sandbox == "host":
            return [str(executable)]
        if self.workspace is None or self.image is None:
            raise ValueError("Docker hook execution requires workspace and image")
        return sandbox_mod.build_run_command(
            self.runtime_name,
            inner_argv=[str(executable)],
            workspace=self.workspace,
            workdir=cwd,
            image=self.image,
            environment={"PYCASTLE_SCOPE": scope},
        )

    @staticmethod
    def _record_identity(value: str) -> str:
        """Return a readable, collision-resistant filename component."""
        readable = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "node"
        digest = hashlib.sha256(value.encode()).hexdigest()[:12]
        return f"{readable[:48]}-{digest}"

    @classmethod
    def freeze(cls, fixture_dir: Path, run_id: str) -> FrozenRunExecution:
        fixture_dir = fixture_dir.resolve()
        root = fixture_dir / "runs" / run_id
        frozen = root / "frozen"
        frozen.mkdir(parents=True, exist_ok=True)
        setup = fixture_dir / FIXTURE_SETUP
        gate = fixture_dir / FIXTURE_GATE
        prompts = {
            str(path.relative_to(fixture_dir / "prompts")): path.read_text()
            for path in (fixture_dir / "prompts").rglob("*")
            if path.is_file()
        }
        return cls(
            frozen / FIXTURE_SETUP,
            frozen / FIXTURE_GATE,
            root / "executions",
            setup.read_bytes(),
            setup.stat().st_mode & 0o777,
            gate.read_bytes(),
            gate.stat().st_mode & 0o777,
            prompts,
        )

    @staticmethod
    def _stage_executable(path: Path, content: bytes, mode: int) -> None:
        """Atomically stage one frozen hook immediately before invocation."""
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".hook-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def _execute_hook(
        self,
        executable: Path,
        content: bytes,
        mode: int,
        *,
        cwd: Path,
        scope: Literal["item", "run"],
        record_path: Path,
        environment: dict[str, str] | None = None,
    ) -> ExecutionRecord:
        self._stage_executable(executable, content, mode)
        try:
            return execute_hook(
                executable,
                cwd=cwd,
                scope=scope,
                record_path=record_path,
                environment=environment,
                argv_builder=self._hook_argv if self.sandbox == "docker" else None,
            )
        finally:
            executable.unlink(missing_ok=True)

    def invoke_setup(
        self,
        worktree: Path,
        *,
        scope: Literal["item", "run"] = "item",
        identity: str,
        ordinal: int,
    ) -> None:
        record = self._execute_hook(
            self.setup,
            self.setup_content,
            self.setup_mode,
            cwd=worktree,
            scope=scope,
            record_path=self.records
            / f"{self._record_identity(identity)}-setup-{ordinal}.json",
        )
        if not record.success:
            raise SetupError(
                SetupFailure(".pycastle/setup", dict(record.termination.__dict__))
            )

    def invoke_gate(
        self,
        worktree: Path,
        *,
        scope: Literal["item", "run"] = "item",
        identity: str,
        node: str,
        ordinal: int,
    ) -> tuple[NodeOutcome, GateOutcome]:
        environment = dict(os.environ)
        started_at = time.monotonic()
        try:
            record = self._execute_hook(
                self.gate,
                self.gate_content,
                self.gate_mode,
                cwd=worktree,
                scope=scope,
                record_path=self.records
                / (
                    f"{self._record_identity(identity)}-"
                    f"{self._record_identity(node)}-gate-{ordinal}.json"
                ),
                environment=environment,
            )
        finally:
            duration_seconds = time.monotonic() - started_at
        evidence = project_gate_evidence(
            record,
            node=node,
            sensitive_values=sensitive_environment_values(environment),
        )
        termination = evidence["termination"]
        return NodeOutcome(
            record.success,
            evidence,
        ), GateOutcome(
            record.success,
            "",
            duration_seconds=duration_seconds,
            termination=termination if isinstance(termination, dict) else None,
        )


@dataclass
class IssueOutcome:
    """What working a single issue inside a batch produced."""

    issue: IssueRef
    branch: str
    merged: bool


@dataclass
class RunOutcome:
    """What a whole batch run produced."""

    run_id: str
    run_branch: str
    selected: list[int] = field(default_factory=list)
    attempted: list[int] = field(default_factory=list)
    stale: list[int] = field(default_factory=list)
    issues: list[IssueOutcome] = field(default_factory=list)
    selection_end: SelectionEnd | None = None
    selection_failure: SelectionFailure | None = None
    selection_failure_checkpoint: str | None = None
    pr_opened: bool = False
    pr_ready: bool = False
    succeeded: bool = True
    stopping_point: str | None = None
    setup_failure: SetupFailure | None = None

    @property
    def completed(self) -> list[int]:
        """Issue numbers that merged cleanly into the run branch."""
        return [o.issue.number for o in self.issues if o.merged]

    @property
    def skipped(self) -> list[int]:
        """Claimed Item attempts not folded into the Run branch."""
        completed = set(self.completed)
        return [number for number in self.attempted if number not in completed]


@dataclass
class RunContext:
    """Host-side identity and command boundary for one active Run worktree."""

    run_id: str
    branch: str
    worktree: Path
    fixture_dir: Path
    runner: Runner
    remote_checkpoint_succeeded: bool = False
    selection_failure_checkpoint: str | None = None


@dataclass
class CancellationState:
    """Mutable ownership state inspected by the Run cancellation boundary."""

    in_flight: IssueRef | None = None


@dataclass(frozen=True)
class PublicationOutcome:
    """Named outcomes from final push and pull-request publication."""

    pr_opened: bool = False
    report_published: bool = False
    pr_ready: bool = False
    final_push_succeeded: bool = False


def slugify(title: str, *, max_words: int = 6) -> str:
    """Turn an issue title into a short, branch-safe slug."""
    words = re.sub(r"[^a-z0-9\s-]", "", title.lower()).split()
    return "-".join(words[:max_words]) or "issue"


def issue_branch_name(issue: IssueRef) -> str:
    """Return the per-issue branch name PyCastle works an issue on."""
    return f"pycastle/issue-{issue.number}-{slugify(issue.title)}"


def render_issue_context(issue: IssueRef) -> str:
    """Format an issue as the preamble handed to the runtime each node.

    The node prompts tell the runtime to read the issue's "What to build" and
    "Acceptance criteria", so it must actually be handed the issue. This renders
    a ``# Issue #<n>: <title>`` header followed by the complete frozen labels and
    assignees, the body when non-empty, then every author-attributed issue
    comment in source order. The title keeps its punctuation and markdown
    (unlike :func:`slugify`).
    """
    header = f"# Issue #{issue.number}: {issue.title}".rstrip()
    body = issue.body.strip()
    facts = (
        f"Labels (JSON): {json.dumps(issue.labels, ensure_ascii=False)}\n\n"
        f"Assignees (JSON): {json.dumps(issue.assignees, ensure_ascii=False)}"
    )
    parts = [header, f"## Frozen Item facts\n\n{facts}"]
    if body:
        parts.append(body)
    if issue.comments:
        comments = "\n\n".join(
            f"### @{comment.author}\n\n{comment.body.strip()}"
            for comment in issue.comments
        )
        parts.append(f"## Issue Comments\n\n{comments}")
    return "\n\n".join(parts)


def render_item_selection_prompt(
    candidates: Sequence[IssueRef],
    outcomes: Sequence[IssueOutcome],
    directions: str,
    *,
    remaining_attempt_capacity: int,
    attempted: Sequence[int] = (),
    stale: Sequence[int] = (),
) -> str:
    """Compose frozen facts, project policy, and PyCastle's response contract."""
    completed = [outcome.issue.number for outcome in outcomes if outcome.merged]
    return _render_item_selection_prompt(
        candidates,
        completed,
        directions,
        remaining_attempt_capacity=remaining_attempt_capacity,
        attempted=attempted,
        stale=stale,
    )


def _select_item(
    candidates: Sequence[IssueRef],
    outcomes: Sequence[IssueOutcome],
    *,
    runtime: Runtime,
    worktree: Path,
    prompt_name: str,
    execution: FrozenRunExecution,
    fixture_dir: Path,
    run_id: str,
    round_number: int,
    remaining_attempt_capacity: int,
    attempted: Sequence[int] = (),
    stale: Sequence[int] = (),
) -> IssueRef | None:
    try:
        directions = execution.prompts[prompt_name]
    except KeyError:
        raise ValueError(
            f"Frozen Item selection prompt is missing: {prompt_name!r}"
        ) from None
    candidate_envelope = _render_item_candidate_envelope(candidates)

    def record(
        status: Literal["accepted", "failed"],
        code: str,
        *,
        result: RuntimeResult | None = None,
        runtime_transcript: str | None = None,
        runtime_stderr: str | None = None,
        runtime_telemetry: Telemetry | None = None,
        runtime_error: str | None = None,
        parsed_response: object | None = None,
    ) -> None:
        _write_item_selection_record(
            fixture_dir=fixture_dir,
            run_id=run_id,
            round_number=round_number,
            candidate_envelope=candidate_envelope,
            prompt_name=prompt_name,
            directions=directions,
            runtime_transcript=(
                result.output if result is not None else runtime_transcript
            ),
            runtime_stderr=runtime_stderr,
            runtime_telemetry=(
                result.telemetry if result is not None else runtime_telemetry
            ),
            runtime_error=runtime_error,
            parsed_response=parsed_response,
            validation_status=status,
            validation_code=code,
        )

    if (
        len(candidate_envelope.encode("utf-8"))
        > SELECTION_CANDIDATE_ENVELOPE_LIMIT_BYTES
    ):
        error = ItemSelectionError(
            "Item candidate envelope exceeds the selection invocation limit",
            code=SelectionFailure.CANDIDATE_ENVELOPE_OVERSIZED,
        )
        record("failed", error.code)
        raise error
    prompt = render_item_selection_prompt(
        candidates,
        outcomes,
        directions,
        remaining_attempt_capacity=remaining_attempt_capacity,
        attempted=attempted,
        stale=stale,
    )
    execution.invoke_setup(
        worktree,
        scope="run",
        identity=f"item-selection-{round_number}",
        ordinal=1,
    )
    try:
        with _without_github_credentials_for_selection():
            result = runtime.run(prompt, cwd=worktree, node=ITEM_SELECTION_NODE)
    except Exception as exc:
        error = ItemSelectionError(
            "Item selection Runtime failed",
            code=SelectionFailure.RUNTIME_FAILED,
        )
        record(
            "failed",
            error.code,
            runtime_transcript=getattr(exc, "transcript", None),
            runtime_stderr=getattr(exc, "stderr", None),
            runtime_telemetry=getattr(exc, "telemetry", None),
            runtime_error=f"{type(exc).__name__}: {exc}",
        )
        raise error from exc
    if not isinstance(result, RuntimeResult):
        error = ItemSelectionError(
            "Item selection Runtime returned a malformed result",
            code=SelectionFailure.RUNTIME_RESULT_INVALID,
        )
        record("failed", error.code)
        raise error
    try:
        decision, parsed_response = _parse_item_selection(
            result.output, {candidate.number for candidate in candidates}
        )
    except ItemSelectionError as error:
        record(
            "failed",
            error.code,
            result=result,
            parsed_response=error.parsed_response,
        )
        raise
    record(
        "accepted",
        "selection-accepted",
        result=result,
        parsed_response=parsed_response,
    )
    if decision.item is None:
        return None
    return next(
        candidate for candidate in candidates if candidate.number == decision.item
    )


def _telemetry_dir(fixture_dir: Path, run_id: str) -> Path:
    """Return (and create) the ignored per-run telemetry/log directory."""
    run_dir = fixture_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_telemetry(
    fixture_dir: Path,
    run_id: str,
    issue: IssueRef,
    node_results: list[NodeResult],
) -> None:
    """Write per-node telemetry for one issue into the Project fixture.

    Telemetry comes from each :class:`~pycastle.graph.NodeResult`'s
    ``result.telemetry`` (a pydantic model dumped with ``model_dump``). Only the
    cost/duration/turns/token counts are recorded; the agent's prose output is
    not, so nothing credential-like is written.
    """
    run_dir = _telemetry_dir(fixture_dir, run_id)
    records = [pr.result.telemetry.model_dump(mode="json") for pr in node_results]
    path = run_dir / f"issue-{issue.number}-telemetry.json"
    path.write_text(json.dumps(records, indent=2) + "\n")


def _transcript_sink(
    fixture_dir: Path, run_id: str, issue_number: int
) -> Callable[[str, str, str], None]:
    """Build a sink that persists the agent's transcript for one issue.

    The runtime surfaces both the agent's thinking and its output but does not
    know ``run_id`` or the issue number (its :meth:`run` only gets ``cwd`` and
    ``node``), so the orchestrator — which owns both — binds this sink onto the
    runtime per issue (#48, #52). Each chunk is appended to
    ``.pycastle/runs/<run_id>/issue-<n>-transcript.log``, tagged with its stream
    and prefixed with its node, so OUTPUT and THINKING interleave in one file in
    chronological order (matching the predecessor ralph runner's single-file
    model), beside the per-issue telemetry and the run log. Neither stream is
    credentials, so writing them to the (gitignored) run dir is fine; it makes a
    finished run auditable even if nobody watched it live.
    """
    run_dir = _telemetry_dir(fixture_dir, run_id)
    path = run_dir / f"issue-{issue_number}-transcript.log"

    def _sink(node: str, tag: str, text: str) -> None:
        with path.open("a") as handle:
            handle.write(f"[{node}] [{tag}] {text}\n")

    return _sink


def _run_transcript_sink(
    fixture_dir: Path, run_id: str, scope: str
) -> Callable[[str, str, str], None]:
    """Build a sink for one before-Run or after-Run node graph."""
    path = _telemetry_dir(fixture_dir, run_id) / "run-node-transcript.log"

    def _sink(node: str, tag: str, text: str) -> None:
        with path.open("a") as handle:
            lines = text.splitlines() or [""]
            for line in lines:
                handle.write(f"[{scope}] [{node}] [{tag}] {line}\n")

    return _sink


def _append_run_telemetry(
    fixture_dir: Path,
    run_id: str,
    scope: str,
    node_results: list[NodeResult],
) -> None:
    """Append scoped Run-node telemetry in lifecycle order."""
    path = _telemetry_dir(fixture_dir, run_id) / "run-node-telemetry.json"
    records = json.loads(path.read_text()) if path.exists() else []
    records.extend(
        {
            "scope": scope,
            **node_result.result.telemetry.model_dump(mode="json"),
            "node": node_result.node,
        }
        for node_result in node_results
    )
    path.write_text(json.dumps(records, indent=2) + "\n")


def _append_log(fixture_dir: Path, run_id: str, message: str) -> None:
    """Append one line to the run log and emit it through ``logging``."""
    logger.info(message)
    _append_local_log(fixture_dir, run_id, message)


def _append_local_log(fixture_dir: Path, run_id: str, message: str) -> None:
    """Append private Run evidence without emitting it to the console."""
    run_dir = _telemetry_dir(fixture_dir, run_id)
    with (run_dir / "run.log").open("a") as handle:
        handle.write(message + "\n")


def _git_failure_detail(result: Any) -> str:
    """Return captured git output formatted for an exception message."""
    output = (
        getattr(result, "stderr", "") or getattr(result, "stdout", "") or ""
    ).strip()
    return f": {output}" if output else ""


def create_branch(branch: str, start_point: str, *, runner: Runner, cwd: Path) -> None:
    """Create ``branch`` at ``start_point`` or raise :class:`BranchError`."""
    result = runner(
        ["git", "branch", branch, start_point],
        capture=True,
        cwd=cwd,
    )
    if getattr(result, "returncode", 1) != 0:
        raise BranchError(
            f"git branch failed for {branch} at {start_point}"
            f"{_git_failure_detail(result)}"
        )


def add_worktree(worktree: Path, branch: str, *, runner: Runner, cwd: Path) -> None:
    """Create ``worktree`` for ``branch`` or raise :class:`WorktreeError`.

    ``git worktree add`` runs with ``check=False`` like every git call here, so
    a non-zero exit is otherwise swallowed and the run proceeds to drive the
    Runtime against a directory that was never created (#64). This reads the exit
    code and raises with git's captured stderr/stdout so the failure surfaces at
    its source instead of cascading into confusing downstream errors.
    """
    result = runner(
        ["git", "worktree", "add", str(worktree), branch],
        capture=True,
        cwd=cwd,
    )
    if getattr(result, "returncode", 1) != 0:
        raise WorktreeError(
            f"git worktree add failed for {branch} at {worktree}"
            f"{_git_failure_detail(result)}"
        )


def cleanup_worktree(worktree: Path, *, runner: Runner, cwd: Path) -> None:
    """Remove a worktree and prune the registry.

    Used both at the end of normal per-issue work and on the interrupt path
    (:func:`_cleanup_interrupted`), so a cancelled run leaves no worktrees
    behind.
    """
    runner(
        ["git", "worktree", "remove", str(worktree), "--force"],
        capture=True,
        cwd=cwd,
    )
    runner(["git", "worktree", "prune"], capture=True, cwd=cwd)


def _git_checkpoint_value(
    argv: list[str],
    *,
    runner: Runner,
    cwd: Path,
    description: str,
) -> str:
    """Read one required Git checkpoint fact or fail closed."""
    result = runner(argv, capture=True, cwd=cwd)
    value = getattr(result, "stdout", "")
    if getattr(result, "returncode", 1) != 0 or not isinstance(value, str):
        raise RunCheckpointError(
            f"Could not verify the durable Run {description}"
            f"{_git_failure_detail(result)}"
        )
    return value.strip()


def _ignored_publication_paths(run: RunContext) -> tuple[str, ...]:
    """List ignored files only inside the Project fixture publication namespace."""
    result = run.runner(
        [
            "git",
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
            "--",
            ".pycastle",
        ],
        capture=True,
        cwd=run.worktree,
    )
    output = getattr(result, "stdout", "")
    if getattr(result, "returncode", 1) != 0 or not isinstance(output, str):
        raise RunCheckpointError(
            "Could not inspect ignored Run publication artifacts"
            f"{_git_failure_detail(result)}"
        )
    if len(output.encode("utf-8")) >= MAX_CAPTURE_BYTES:
        raise RunCheckpointError(
            "Ignored Run publication artifact index exceeds the verification limit"
        )
    paths = tuple(path for path in output.split("\0") if path)
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts or parts[0] != ".pycastle" or ".." in parts:
            raise RunCheckpointError("Git returned an unsafe publication path")
    return paths


def _capture_ignored_publication(
    run: RunContext,
) -> tuple[tuple[str, IgnoredPublicationArtifact], ...]:
    """Snapshot ignored publication files without traversing dependency trees."""
    artifacts: list[tuple[str, IgnoredPublicationArtifact]] = []
    for relative in _ignored_publication_paths(run):
        path = run.worktree.joinpath(*PurePosixPath(relative).parts)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            artifact = IgnoredPublicationArtifact("symlink", os.readlink(path))
        elif stat.S_ISREG(metadata.st_mode):
            artifact = IgnoredPublicationArtifact(
                "file",
                path.read_bytes(),
                stat.S_IMODE(metadata.st_mode),
            )
        else:
            raise RunCheckpointError(
                f"Unsupported ignored Run publication artifact: {relative}"
            )
        artifacts.append((relative, artifact))
    return tuple(sorted(artifacts))


def _capture_durable_run_checkpoint(
    run: RunContext, expected_commit: str
) -> DurableRunCheckpoint:
    """Capture the immutable commit and ignored publication artifacts."""
    return DurableRunCheckpoint(
        expected_commit,
        _capture_ignored_publication(run),
    )


def _ignored_publication_matches(
    run: RunContext,
    expected: tuple[tuple[str, IgnoredPublicationArtifact], ...],
) -> bool:
    """Compare the bounded namespace without reading newly-created large files."""
    if tuple(sorted(_ignored_publication_paths(run))) != tuple(
        relative for relative, _artifact in expected
    ):
        return False
    for relative, artifact in expected:
        path = run.worktree.joinpath(*PurePosixPath(relative).parts)
        try:
            metadata = path.lstat()
            if artifact.kind == "symlink":
                if not stat.S_ISLNK(metadata.st_mode):
                    return False
                if os.readlink(path) != artifact.content:
                    return False
            else:
                assert isinstance(artifact.content, bytes)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != len(artifact.content)
                    or stat.S_IMODE(metadata.st_mode) != artifact.mode
                    or path.read_bytes() != artifact.content
                ):
                    return False
        except OSError:
            return False
    return True


def _verify_durable_run_checkpoint(
    run: RunContext, checkpoint: DurableRunCheckpoint
) -> None:
    """Prove selection did not change durable Git or publication state."""
    branch_commit = _git_checkpoint_value(
        ["git", "rev-parse", "--verify", f"{run.branch}^{{commit}}"],
        runner=run.runner,
        cwd=run.worktree,
        description="branch",
    )
    worktree_commit = _git_checkpoint_value(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        runner=run.runner,
        cwd=run.worktree,
        description="worktree HEAD",
    )
    checked_out_branch = _git_checkpoint_value(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        runner=run.runner,
        cwd=run.worktree,
        description="worktree branch",
    )
    status = _git_checkpoint_value(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        runner=run.runner,
        cwd=run.worktree,
        description="worktree status",
    )
    if (
        branch_commit != checkpoint.commit
        or worktree_commit != checkpoint.commit
        or checked_out_branch != run.branch
        or status
        or not _ignored_publication_matches(run, checkpoint.ignored_publication)
    ):
        raise RunCheckpointError(
            "Item selection changed the durable Run branch or worktree"
        )


def _remove_publication_path(path: Path, namespace: Path) -> None:
    """Remove one validated path and any newly-empty parents in `.pycastle`."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    parent = path.parent
    while parent != namespace and parent.is_relative_to(namespace):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _restore_ignored_publication(
    run: RunContext,
    expected: tuple[tuple[str, IgnoredPublicationArtifact], ...],
) -> None:
    """Restore the exact ignored publication snapshot in the owned Run worktree."""
    namespace = run.worktree / ".pycastle"
    expected_map = dict(expected)
    current_paths = _ignored_publication_paths(run)
    for relative in sorted(
        current_paths, key=lambda value: value.count("/"), reverse=True
    ):
        if relative not in expected_map:
            path = run.worktree.joinpath(*PurePosixPath(relative).parts)
            _remove_publication_path(path, namespace)
    for relative, artifact in expected:
        path = run.worktree.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or path.exists():
            _remove_publication_path(path, namespace)
        path.parent.mkdir(parents=True, exist_ok=True)
        if artifact.kind == "symlink":
            assert isinstance(artifact.content, str)
            path.symlink_to(artifact.content)
        else:
            assert isinstance(artifact.content, bytes)
            path.write_bytes(artifact.content)
            assert artifact.mode is not None
            path.chmod(artifact.mode)


def _restore_durable_run_checkpoint(
    run: RunContext, checkpoint: DurableRunCheckpoint
) -> None:
    """Restore only PyCastle-owned Run state to its last verified checkpoint."""
    commands = (
        [
            "git",
            "update-ref",
            f"refs/heads/{run.branch}",
            checkpoint.commit,
        ],
        ["git", "reset", "--hard", checkpoint.commit],
        ["git", "clean", "-fd"],
    )
    for argv in commands:
        result = run.runner(argv, capture=True, cwd=run.worktree)
        if getattr(result, "returncode", 1) != 0:
            raise RunCheckpointError(
                "Could not restore the durable Run checkpoint"
                f"{_git_failure_detail(result)}"
            )
    try:
        _restore_ignored_publication(run, checkpoint.ignored_publication)
    except OSError as exc:
        raise RunCheckpointError(
            f"Could not restore ignored Run publication artifacts: {exc}"
        ) from exc
    _verify_durable_run_checkpoint(run, checkpoint)


def _selection_worktree_registered(
    worktree: Path,
    *,
    runner: Runner,
    workspace: Path,
) -> bool:
    """Return whether Git still records the disposable selection checkout."""
    result = runner(
        ["git", "worktree", "list", "--porcelain", "-z"],
        capture=True,
        cwd=workspace,
    )
    output = getattr(result, "stdout", "")
    if (
        getattr(result, "returncode", 1) != 0
        or not isinstance(output, str)
        or len(output.encode("utf-8")) >= MAX_CAPTURE_BYTES
    ):
        raise WorktreeError(
            "Could not verify disposable Item selection worktree cleanup"
            f"{_git_failure_detail(result)}"
        )
    return f"worktree {worktree}" in output.split("\0")


@contextmanager
def _disposable_selection_worktree(
    run: RunContext,
    *,
    worktree_root: Path,
    workspace: Path,
) -> Iterator[Path]:
    """Yield a detached writable checkout and verify containment on exit."""
    expected_commit = _git_checkpoint_value(
        ["git", "rev-parse", "--verify", f"{run.branch}^{{commit}}"],
        runner=run.runner,
        cwd=run.worktree,
        description="checkpoint",
    )
    # The immutable final-push source must survive every failure from worktree
    # creation onward. A non-zero `worktree add` can still have created a path,
    # registered a checkout, or changed the durable Run through a hostile
    # command boundary.
    run.selection_failure_checkpoint = expected_commit
    checkpoint = _capture_durable_run_checkpoint(run, expected_commit)
    _verify_durable_run_checkpoint(run, checkpoint)
    selection_worktree = worktree_root / f"selection-{run.run_id}"
    primary_error: BaseException | None = None
    worktree_created = False
    body_completed = False
    try:
        result = run.runner(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(selection_worktree),
                expected_commit,
            ],
            capture=True,
            cwd=workspace,
        )
        if getattr(result, "returncode", 1) != 0:
            raise WorktreeError(
                f"git worktree add failed for Item selection at "
                f"{selection_worktree}{_git_failure_detail(result)}"
            )
        worktree_created = True
        yield selection_worktree
        body_completed = True
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_error: SelectionWorktreeCleanupError | None = None
        remove: Any | None = None
        prune: Any | None = None
        try:
            remove = run.runner(
                ["git", "worktree", "remove", str(selection_worktree), "--force"],
                capture=True,
                cwd=workspace,
            )
        except Exception as exc:
            cleanup_error = SelectionWorktreeCleanupError(
                "Could not remove the disposable Item selection worktree" f": {exc}",
                expected_commit=expected_commit,
            )
        # A failed add may leave an unregistered directory that Git cannot
        # remove. It is still a PyCastle-owned disposable path, so remove it
        # before pruning any partial registry entry.
        if not worktree_created and (
            selection_worktree.exists() or selection_worktree.is_symlink()
        ):
            try:
                if selection_worktree.is_symlink() or selection_worktree.is_file():
                    selection_worktree.unlink()
                else:
                    shutil.rmtree(selection_worktree)
            except OSError as exc:
                if cleanup_error is None:
                    cleanup_error = SelectionWorktreeCleanupError(
                        "Could not remove the partial Item selection worktree"
                        f": {exc}",
                        expected_commit=expected_commit,
                    )
        try:
            prune = run.runner(
                ["git", "worktree", "prune"],
                capture=True,
                cwd=workspace,
            )
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = SelectionWorktreeCleanupError(
                    "Could not remove the disposable Item selection worktree"
                    f": {exc}",
                    expected_commit=expected_commit,
                )
        registered = True
        try:
            registered = _selection_worktree_registered(
                selection_worktree,
                runner=run.runner,
                workspace=workspace,
            )
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = SelectionWorktreeCleanupError(
                    f"Could not verify disposable Item selection cleanup: {exc}",
                    expected_commit=expected_commit,
                )
        if cleanup_error is None and (
            (worktree_created and getattr(remove, "returncode", 1) != 0)
            or getattr(prune, "returncode", 1) != 0
            or selection_worktree.exists()
            or selection_worktree.is_symlink()
            or registered
        ):
            cleanup_error = SelectionWorktreeCleanupError(
                "Could not remove the disposable Item selection worktree"
                f"{_git_failure_detail(remove) or _git_failure_detail(prune)}",
                expected_commit=expected_commit,
            )

        checkpoint_error: Exception | None = None
        try:
            _verify_durable_run_checkpoint(run, checkpoint)
        except Exception as exc:
            checkpoint_error = exc
            try:
                _restore_durable_run_checkpoint(run, checkpoint)
            except Exception as restore_error:
                checkpoint_error.add_note(
                    f"Durable Run restoration also failed: {restore_error}"
                )

        secondary_errors = tuple(
            error for error in (cleanup_error, checkpoint_error) if error is not None
        )
        if primary_error is not None and secondary_errors:
            for error in secondary_errors:
                primary_error.add_note(
                    f"Item selection containment also failed: {error}"
                )
            try:
                _append_local_log(
                    run.fixture_dir,
                    run.run_id,
                    "Item selection containment also failed while preserving "
                    f"{type(primary_error).__name__}: "
                    + "; ".join(str(error) for error in secondary_errors),
                )
            except BaseException as record_error:
                primary_error.add_note(
                    "Could not retain secondary Item selection containment "
                    f"evidence: {record_error}"
                )
        elif cleanup_error is not None:
            if checkpoint_error is not None:
                cleanup_error.add_note(str(checkpoint_error))
            raise cleanup_error
        elif checkpoint_error is not None:
            raise checkpoint_error
        if worktree_created and body_completed and not secondary_errors:
            run.selection_failure_checkpoint = None


def delete_local_branch(branch: str, *, runner: Runner, cwd: Path) -> None:
    """Remove Git state that cannot represent a publishable Run checkpoint."""
    result = runner(["git", "branch", "-D", branch], capture=True, cwd=cwd)
    if getattr(result, "returncode", 1) != 0:
        raise BranchError(
            f"git branch deletion failed for {branch}{_git_failure_detail(result)}"
        )


def _delete_failed_run_branch(run: RunContext, *, workspace: Path) -> None:
    """Remove local and previously-pushed Run refs after pre-integration failure."""
    if run.remote_checkpoint_succeeded:
        result = run.runner(
            ["git", "push", "origin", "--delete", run.branch],
            capture=True,
            cwd=workspace,
        )
        if getattr(result, "returncode", 1) != 0:
            raise BranchError(
                f"remote Run branch deletion failed for {run.branch}"
                f"{_git_failure_detail(result)}"
            )
    delete_local_branch(run.branch, runner=run.runner, cwd=workspace)


@contextmanager
def _sigint_as_keyboard_interrupt() -> Iterator[None]:
    """Make SIGINT raise ``KeyboardInterrupt`` for the duration of the run.

    Ported in shape from Ralph's ``sigint_handler``: a SIGINT arriving mid-run is
    turned into a :class:`KeyboardInterrupt` so the per-issue work unwinds
    through the run's cleanup-and-restore path rather than killing the process
    outright. The previous handler is restored on exit, and installing a handler
    is skipped when not on the main thread (where ``signal.signal`` would raise).
    """

    def _raise(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGINT, _raise)
    except ValueError:
        # Not on the main thread: no handler to install; the default applies.
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _cleanup_cancelled(
    *,
    issue: IssueRef | None,
    issue_source: IssueSource,
    worktree_root: Path,
    workspace: Path,
    run: RunContext,
) -> None:
    """Best-effort cancellation cleanup with a factual recovery summary."""
    manual_cleanup: list[Path] = []
    worktrees = ([worktree_root / f"issue-{issue.number}"] if issue else []) + [
        run.worktree
    ]
    for worktree in worktrees:
        failed = False
        for argv in (
            ["git", "worktree", "remove", str(worktree), "--force"],
            ["git", "worktree", "prune"],
        ):
            try:
                result = run.runner(argv, capture=True, cwd=workspace)
                failed = failed or getattr(result, "returncode", 1) != 0
            except BaseException:  # cleanup must never replace the interruption
                failed = True
                logger.exception("Cancellation cleanup command failed for %s", worktree)
        if failed:
            manual_cleanup.append(worktree)

    release_failure = False
    if issue is not None:
        try:
            issue_source.release(issue.number)
        except BaseException:  # release is independent best-effort cleanup
            release_failure = True
            logger.exception("Failed to release in-flight Item #%s", issue.number)

    records = _telemetry_dir(run.fixture_dir, run.run_id)
    recovery = (
        f"Remote checkpoint: origin/{run.branch}."
        if run.remote_checkpoint_succeeded
        else "No remote checkpoint survived."
    )
    cleanup = (
        "Cleanup completed."
        if not manual_cleanup
        else "Manual worktree cleanup required: "
        + ", ".join(str(path) for path in manual_cleanup)
        + "."
    )
    release = (
        f" In-flight Item #{issue.number} could not be released."
        if issue is not None and release_failure
        else (f" In-flight Item #{issue.number} released." if issue is not None else "")
    )
    summary = (
        f"Run cancelled; no pull request opened. {recovery} "
        f"Retained records: {records}. {cleanup}{release}"
    )
    try:
        _append_log(run.fixture_dir, run.run_id, summary)
    except BaseException:  # reporting is best-effort and cannot mask cancellation
        logger.exception("Could not persist cancellation recovery summary: %s", summary)


def _walk_execution_graph(
    issue: IssueRef | None,
    *,
    runtime: Runtime,
    worktree: Path,
    graph: ExecutionGraph,
    execution: FrozenRunExecution,
    scope: Literal["item", "run"] = "item",
    identity: str | None = None,
    context: str | None = None,
    checkpoint: Callable[[RuntimeNode], None] | None = None,
) -> tuple[ExecutionResult, GateOutcome | None]:
    """Walk one mixed Execution graph with Setup before every node visit."""
    if issue is None and context is None:
        raise ValueError("Run graph execution requires factual Run context")
    executor = PromptRenderer(
        prompts=execution.prompts,
        preamble=context if context is not None else render_issue_context(issue),
    )
    results: list[NodeResult] = []
    identity = identity or f"item-{issue.number}"
    last_gate: GateOutcome | None = None

    def visit(entry: NodeVisit) -> NodeOutcome:
        node = entry.node
        execution.invoke_setup(
            worktree,
            scope=scope,
            identity=f"{identity}-{node.name}",
            ordinal=entry.ordinal,
        )
        if isinstance(node, GateNode):
            nonlocal last_gate
            outcome, last_gate = execution.invoke_gate(
                worktree,
                scope=scope,
                identity=identity,
                node=node.name,
                ordinal=entry.ordinal,
            )
            return outcome
        if not isinstance(node, RuntimeNode):
            raise TypeError("Unknown Execution node")
        extra = (
            json.dumps(entry.predecessor, sort_keys=True)
            if entry.predecessor is not None
            else None
        )
        try:
            result = runtime.run(
                executor.render_prompt(node, extra),
                cwd=worktree,
                node=node.name,
            )
        except AgentCrashError as error:
            return NodeOutcome(
                False,
                {
                    "source": node.name,
                    "success": False,
                    "termination": {"kind": "exited", "code": error.exit_code},
                },
            )
        except OSError as error:
            return NodeOutcome(
                False,
                {
                    "source": node.name,
                    "success": False,
                    "termination": {
                        "kind": "launch_error",
                        "error_kind": type(error).__name__,
                        "errno": error.errno,
                    },
                },
            )
        except subprocess.TimeoutExpired as error:
            return NodeOutcome(
                False,
                {
                    "source": node.name,
                    "success": False,
                    "termination": {
                        "kind": "timeout",
                        "timeout_seconds": error.timeout,
                    },
                },
            )
        except (TypeError, ValueError) as error:
            return NodeOutcome(
                False,
                {
                    "source": node.name,
                    "success": False,
                    "termination": {
                        "kind": "malformed_result",
                        "error_kind": type(error).__name__,
                    },
                },
            )
        if not isinstance(result, RuntimeResult):
            return NodeOutcome(
                False,
                {
                    "source": node.name,
                    "success": False,
                    "termination": {
                        "kind": "malformed_result",
                        "error_kind": type(result).__name__,
                    },
                },
            )
        results.append(NodeResult(node.name, result))
        if checkpoint is not None:
            checkpoint(node)
        return NodeOutcome(True, {"source": node.name, "success": True})

    walked = walk_execution_graph(graph, visit)
    return ExecutionResult(results, walked.terminal), last_gate


def _work_issue(
    issue: IssueRef,
    *,
    runtime: Runtime,
    issue_source: IssueSource,
    run: RunContext,
    worktree_root: Path,
    assignee: str,
    workspace: Path,
    item_graph: ExecutionGraph,
    execution: FrozenRunExecution,
    cancellation: CancellationState | None = None,
    verbose: bool = False,
) -> IssueOutcome:
    """Work one issue in its own worktree and merge it into the run branch.

    The issue is claimed, branched off the run branch into its own worktree, and
    driven by walking its Execution graph: from ``start`` each node runs and
    its success/failure outcome follows the node's ``on_success`` /
    ``on_failure`` edge until a Terminal. A failed node follows only its declared
    edge; no retry or automatic context is added.
    A walk that reaches
    :data:`~pycastle.graph.DONE` is committed and, on a clean merge, folded into
    the run; the issue worktree and branch are then removed.

    A walk that reaches :data:`~pycastle.graph.HUMAN` (through a declared edge
    or the visit bound) labels the issue ``ready-for-human`` and skips it
    (recorded as not merged) so the run continues to the next issue — one stuck
    item does not sink the batch. A merge that does not apply cleanly is likewise
    recorded as not merged and skipped, and the issue is labelled
    ``ready-for-human`` (#9) so a person resolves the conflict while the run
    carries on with the remaining items.
    """
    fixture_dir = run.fixture_dir
    run_id = run.run_id
    run_branch = run.branch
    runner = run.runner
    branch = issue_branch_name(issue)
    # Record ownership before calling the external Issue source.  A claim can
    # complete remotely and then surface ``KeyboardInterrupt`` locally, so
    # recording it afterwards leaves a cancellation race where the Item stays
    # claimed.  ``release`` is the existing idempotent recovery operation for
    # claim failures as well as interruptions.
    if cancellation is not None:
        cancellation.in_flight = issue
    try:
        issue_source.claim(issue.number, assignee=assignee)
    except Exception as exc:
        raise ItemClaimError("Item claim failed") from exc
    _append_log(fixture_dir, run_id, f"Working #{issue.number} on {branch}")

    if verbose:
        # The runtime is shared across issues (built once so a Docker image builds
        # once), so its transcript sink is rebound per issue to point at this
        # issue's transcript log. The runtime keeps surfacing [THINKING:<node>]
        # and [OUTPUT:<node>] lines live regardless; the sink is only the run-dir
        # persistence target. A deliberate mutation of the shared runtime: the
        # Claude/Codex runtimes read this attribute, a runtime that does not just
        # ignores it.
        runtime.transcript_sink = _transcript_sink(  # type: ignore[attr-defined]
            fixture_dir, run_id, issue.number
        )

    issue_worktree = worktree_root / f"issue-{issue.number}"
    create_branch(branch, run_branch, runner=runner, cwd=workspace)
    # A failed worktree add is an infra fault, not an issue-content fault, so it
    # is raised (not routed to ready-for-human): run_batch's interrupt teardown
    # removes the absent worktree and releases the claimed issue back to
    # ready-for-agent, then the run aborts rather than driving the Runtime against a
    # directory that was never created (#64).
    add_worktree(issue_worktree, branch, runner=runner, cwd=workspace)

    walk, _ = _walk_execution_graph(
        issue,
        runtime=runtime,
        worktree=issue_worktree,
        graph=item_graph,
        execution=execution,
    )
    if walk.results:
        _write_telemetry(fixture_dir, run_id, issue, walk.results)

    if walk.terminal is HUMAN:
        # The walk routed to a human or hit the visit bound: hand the issue over and
        # move on. Cleaning up the worktree and branch keeps the batch tidy.
        issue_source.mark_for_human(issue.number)
        _append_log(
            fixture_dir,
            run_id,
            f"#{issue.number} reached the HUMAN terminal; marked ready-for-human.",
        )
        if cancellation is not None:
            cancellation.in_flight = None
        cleanup_worktree(issue_worktree, runner=runner, cwd=workspace)
        runner(["git", "branch", "-D", branch], capture=True, cwd=workspace)
        return IssueOutcome(issue=issue, branch=branch, merged=False)

    # Fixed scratch paths are gitignored. These exclusions also cover the two
    # historical planning paths from #68 if a Runtime writes them despite the
    # plan prompt's canonical .pycastle/plan.md destination.
    runner(
        [
            "git",
            "add",
            "-A",
            "--",
            ".",
            ":(exclude,top)PLAN.md",
            ":(exclude,top,glob).pycastle/plan-issue-*.md",
        ],
        capture=True,
        cwd=issue_worktree,
    )
    runner(
        ["git", "commit", "-m", f"feat: address #{issue.number} ({runtime.name})"],
        capture=True,
        cwd=issue_worktree,
    )

    if _branch_has_no_diff(branch, run=run):
        # The walk reached DONE but the node produced no change (e.g. a runtime
        # that silently no-ops): the issue branch equals the run branch, so a
        # merge would be a clean no-op and report a phantom success with no PR.
        # Hand the issue to a human instead of counting it completed (#35).
        issue_source.mark_for_human(issue.number)
        _append_log(
            fixture_dir,
            run_id,
            f"#{issue.number} produced no changes; marked ready-for-human.",
        )
        if cancellation is not None:
            cancellation.in_flight = None
        cleanup_worktree(issue_worktree, runner=runner, cwd=workspace)
        runner(["git", "branch", "-D", branch], capture=True, cwd=workspace)
        return IssueOutcome(issue=issue, branch=branch, merged=False)

    merged = _merge_issue_branch(
        branch,
        issue=issue,
        run=run,
    )
    if not merged:
        # The merge conflicted and was aborted (#9): hand the issue to a human so
        # the loop keeps going with the remaining items instead of failing the run.
        issue_source.mark_for_human(issue.number)
        _append_log(
            fixture_dir,
            run_id,
            f"#{issue.number} did not merge cleanly; marked ready-for-human.",
        )
    else:
        # The merge is the durability boundary: push it before cleaning up the
        # item worktree so the remote checkpoint follows the fold immediately.
        _push_run_branch(run=run, final=False)

    # The Item outcome and its durability attempt are complete.  Clear ownership
    # before local cleanup so an interrupt at the return boundary cannot release
    # an Item whose outcome is already durable.
    if cancellation is not None:
        cancellation.in_flight = None

    cleanup_worktree(issue_worktree, runner=runner, cwd=workspace)
    runner(["git", "branch", "-D", branch], capture=True, cwd=workspace)
    return IssueOutcome(issue=issue, branch=branch, merged=merged)


def _branch_has_no_diff(
    branch: str,
    *,
    run: RunContext,
) -> bool:
    """Whether the issue branch introduced no change over the run branch.

    After the walk commits, ``git diff --quiet <run_branch> <branch>`` exits 0
    when the two trees are identical (no change) and non-zero when they differ.
    An empty diff means the node silently no-opped: merging it would be a clean
    no-op that reports a phantom success and opens no PR, so the caller routes the
    issue to a human instead (#35). The check runs in the run worktree, which can
    resolve both branch refs. It stays git-only and does not touch the Issue
    source.
    """
    diff = run.runner(
        ["git", "diff", "--quiet", run.branch, branch],
        capture=True,
        cwd=run.worktree,
    )
    return getattr(diff, "returncode", 1) == 0


def _merge_issue_branch(
    branch: str,
    *,
    issue: IssueRef,
    run: RunContext,
) -> bool:
    """Merge an issue branch into the run worktree; return True on a clean merge.

    On a merge that does not apply cleanly the merge is aborted and ``False``
    returned so the caller skips the issue. The caller (:func:`_work_issue`)
    labels a skipped issue ``ready-for-human`` (#9); this stays git-only and does
    not touch the Issue source.
    """
    merge = run.runner(
        ["git", "merge", branch, "--no-edit"],
        capture=True,
        cwd=run.worktree,
    )
    if getattr(merge, "returncode", 1) == 0:
        _append_log(
            run.fixture_dir,
            run.run_id,
            f"Merged #{issue.number} into {run.run_id}",
        )
        return True

    # A conflicting merge is aborted and the issue skipped; the caller marks it
    # ready-for-human so a person resolves the conflict (#9).
    _append_log(
        run.fixture_dir,
        run.run_id,
        f"Merge of #{issue.number} did not apply cleanly; skipping for a human.",
    )
    run.runner(["git", "merge", "--abort"], capture=True, cwd=run.worktree)
    return False


RUN_REPORT = ".pycastle/run-report.md"
RUN_REVIEW = ".pycastle/run-review.md"
RUN_REPORT_LIMIT = 65_536


def render_run_context(
    run_id: str,
    candidates: Sequence[IssueRef],
    outcomes: Sequence[IssueOutcome],
    *,
    stale: Sequence[int] = (),
    selection_end: SelectionEnd | None = None,
) -> str:
    """Render the bounded factual envelope supplied to each Run node."""
    outcome_by_number = {o.issue.number: o for o in outcomes}
    stale_numbers = set(stale)
    rows = []
    for issue in candidates:
        outcome = outcome_by_number.get(issue.number)
        if outcome is not None:
            state = "completed" if outcome.merged else "skipped"
        elif issue.number in stale_numbers:
            state = "stale"
        elif selection_end is not None:
            state = "not-selected"
        else:
            state = "pending"
        rows.append(f"- #{issue.number}: {issue.title} [{state}]")
    context = f"# PyCastle Run {run_id}\n\n## Item candidate pool\n\n" + "\n".join(rows)
    if selection_end is not None:
        context += f"\n\n## Item selection\n\nEnded: {selection_end}"
    return context


def _checkpoint_run_node(
    node: RuntimeNode,
    *,
    run: RunContext,
    scope: str = "Run",
) -> None:
    """Commit a successful Run node when dirty and attempt a durability push."""
    argv: Sequence[str]
    try:
        # Run review/report artifacts are part of the Project fixture's ignored
        # scratch-file contract.  Explicitly naming those ignored paths as
        # exclusion pathspecs makes real Git reject the otherwise valid add.
        add_argv = ["git", "add", "-A", "--", "."]
        argv = add_argv
        staged = run.runner(
            add_argv,
            capture=True,
            cwd=run.worktree,
        )
        if getattr(staged, "returncode", 1) != 0:
            detail = _record_host_command_failure(
                run, scope=scope, node=node.name, argv=add_argv, result=staged
            )
            raise RunCheckpointError(detail)

        diff_argv = ["git", "diff", "--cached", "--quiet"]
        argv = diff_argv
        dirty = run.runner(
            diff_argv,
            capture=True,
            cwd=run.worktree,
        )
        dirty_code = getattr(dirty, "returncode", 2)
        if dirty_code not in (0, 1):
            detail = _record_host_command_failure(
                run, scope=scope, node=node.name, argv=diff_argv, result=dirty
            )
            raise RunCheckpointError(detail)
        if dirty_code == 1:
            commit_argv = [
                "git",
                "commit",
                "-m",
                f"chore: checkpoint Run node {node.name}",
            ]
            argv = commit_argv
            committed = run.runner(
                commit_argv,
                capture=True,
                cwd=run.worktree,
            )
            if getattr(committed, "returncode", 1) != 0:
                detail = _record_host_command_failure(
                    run,
                    scope=scope,
                    node=node.name,
                    argv=commit_argv,
                    result=committed,
                )
                raise RunCheckpointError(detail)
    except OSError as exc:
        detail = _record_host_command_exception(
            run, scope=scope, node=node.name, argv=argv, exc=exc
        )
        raise RunCheckpointError(detail) from exc

    _push_run_branch(run=run, final=False, scope=scope, node=node.name)


def _discard_incomplete_run_scope(run: RunContext) -> None:
    """Restore the Run worktree to its last committed durable checkpoint."""
    _discard_run_commands(run, scope="after-Run", node="Setup")


def _discard_run_commands(run: RunContext, *, scope: str, node: str) -> None:
    """Discard incomplete Run work and retain diagnostics for failed commands."""
    commands = (
        ["git", "reset", "--hard", "HEAD"],
        ["git", "clean", "-fd"],
        ["git", "clean", "-fdX", "--", RUN_REVIEW, RUN_REPORT],
    )
    for argv in commands:
        result = run.runner(argv, capture=True, cwd=run.worktree)
        if getattr(result, "returncode", 1) != 0:
            detail = _record_host_command_failure(
                run, scope=scope, node=node, argv=argv, result=result
            )
            raise RunCheckpointError(
                f"could not discard incomplete Run scope\n{detail}"
            )


def _record_host_command_failure(
    run: RunContext,
    *,
    scope: str,
    node: str,
    argv: Sequence[str],
    result: Any,
) -> str:
    """Surface and retain all captured diagnostics from a boundary command."""
    return _record_host_command_diagnostics(
        run,
        scope=scope,
        node=node,
        argv=argv,
        headline="Host command failed",
        exit_code=repr(getattr(result, "returncode", None)),
        stdout=repr(getattr(result, "stdout", None)),
        stderr=repr(getattr(result, "stderr", None)),
    )


def _record_host_command_exception(
    run: RunContext,
    *,
    scope: str,
    node: str,
    argv: Sequence[str],
    exc: OSError,
) -> str:
    """Surface command identity when a boundary command cannot be launched."""
    return _record_host_command_diagnostics(
        run,
        scope=scope,
        node=node,
        argv=argv,
        headline="Host command could not be launched",
        exit_code="unavailable",
        stdout="unavailable",
        stderr=repr(str(exc)),
    )


def _record_host_command_diagnostics(
    run: RunContext,
    *,
    scope: str,
    node: str,
    argv: Sequence[str],
    headline: str,
    exit_code: str,
    stdout: str,
    stderr: str,
) -> str:
    """Write one consistently formatted host-command failure record."""
    detail = "\n".join(
        (
            headline,
            f"argv: {json.dumps(list(argv))}",
            f"exit code: {exit_code}",
            f"stdout: {stdout}",
            f"stderr: {stderr}",
        )
    )
    logger.error("%s", detail)
    sink = _run_transcript_sink(run.fixture_dir, run.run_id, scope)
    for line in detail.splitlines():
        sink(node, "HOST-COMMAND", line)
    return detail


def _harvest_report(run: RunContext) -> tuple[str | None, str | None]:
    """Retain and validate the optional authored Run report without truncation."""
    source = run.worktree / RUN_REPORT
    if source.is_symlink():
        return None, "Run report must be a regular file."
    if not source.exists():
        return None, None
    if not source.is_file():
        return None, "Run report must be a regular file."
    try:
        raw = source.read_bytes()
    except OSError as exc:
        return None, f"Run report could not be read: {exc}."
    retained = _telemetry_dir(run.fixture_dir, run.run_id) / "run-report.md"
    retained.write_bytes(raw)
    if len(raw) > RUN_REPORT_LIMIT:
        return (
            None,
            f"Run report exceeds the {RUN_REPORT_LIMIT}-byte publication limit.",
        )
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "Run report is not valid UTF-8."


def run_batch(
    *,
    runtime: Runtime,
    issue_source: IssueSource,
    candidates: Sequence[IssueRef],
    fixture_dir: Path,
    repo: str,
    base_branch: str,
    assignee: str,
    run_id: str,
    iterations: int = 1,
    workspace: Path | None = None,
    worktree_root: Path | None = None,
    include_unassigned: bool = False,
    runner: Runner = run_cmd,
    verbose: bool = False,
    frozen_inputs: FrozenReadinessInputs,
) -> RunOutcome:
    """Work up to ``iterations`` ready issues into one integrated pull request.

    ``candidates`` is the candidate pool frozen by readiness. A per-run branch
    is cut in its own worktree so the main checkout stays put; each
    policy-selected issue is then worked in its own worktree off the Run branch
    and, on a clean merge, folded into that branch. One pull request is opened
    for the Run, closing every issue that merged. ``run_id`` is injected (not
    read from a clock) to keep Runs deterministic for tests.

    Runtime-node and Gate-node outcomes follow only their declared Execution
    graph edges. Every Runtime visit is fresh, and its successor receives only
    the immediate predecessor's typed outcome. Frozen Setup runs once at Run
    bootstrap and again immediately before every executable node; any Setup
    failure stops the Run outside graph control flow.

    ``verbose`` (#48, #52) turns on transcript persistence: before each issue is
    worked the runtime's transcript sink is bound to that issue's transcript log
    under ``.pycastle/runs/<run_id>/`` (live ``[THINKING:<node>]`` and
    ``[OUTPUT:<node>]`` surfacing is already on in the runtime itself). Off by
    default, so a normal run writes no transcript log and behaves exactly as
    before.
    """
    fixture_dir = fixture_dir.resolve()
    if not isinstance(frozen_inputs, FrozenReadinessInputs):
        raise TypeError("Run requires FrozenReadinessInputs")
    # Copy again at the orchestration boundary so callers cannot mutate the
    # active membership, order, or Item content during project execution.
    candidates = tuple(issue.model_copy(deep=True) for issue in candidates)
    frozen_items = frozen_inputs.candidate_pool
    if not isinstance(frozen_items, tuple) or not all(
        isinstance(issue, IssueRef) for issue in frozen_items
    ):
        raise ValueError("Frozen readiness Item candidate pool is invalid")
    if candidates != frozen_items:
        raise ValueError(
            "Item candidates differ from the frozen readiness candidate pool"
        )
    if not re.fullmatch(r"[0-9a-f]{40,64}", frozen_inputs.base_commit):
        raise ValueError("Frozen readiness base commit is invalid")
    branch_start = frozen_inputs.base_commit
    run_branch = f"pycastle/run-{run_id}"
    outcome = RunOutcome(
        run_id=run_id,
        run_branch=run_branch,
    )
    if not candidates:
        return outcome

    frozen_project = frozen_inputs.project_fixture
    for frozen_file in frozen_project.files:
        relative_path = Path(frozen_file.relative_path)
        if (
            not frozen_file.relative_path
            or relative_path.is_absolute()
            or ".." in relative_path.parts
        ):
            raise ValueError("Frozen Project fixture path is invalid")
    workspace = workspace or Path.cwd()
    worktree_root = worktree_root or (fixture_dir / "worktrees")
    worktree_root.mkdir(parents=True, exist_ok=True)

    files = {
        frozen_file.relative_path: frozen_file for frozen_file in frozen_project.files
    }
    try:
        setup_file = files[FIXTURE_SETUP]
        gate_file = files[FIXTURE_GATE]
    except KeyError as error:
        raise ValueError(
            f"Frozen Project fixture is missing mandatory executable: {error.args[0]}"
        ) from None
    prompts: dict[str, str] = {}
    for relative_path, frozen_file in files.items():
        path = Path(relative_path)
        if not path.parts or path.parts[0] != "prompts":
            continue
        prompt_name = str(Path(*path.parts[1:]))
        try:
            prompts[prompt_name] = frozen_file.content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                f"Frozen Runtime prompt is not valid UTF-8: {prompt_name}"
            ) from None
    run_definition = frozen_project.run_definition
    frozen_dir = fixture_dir / "runs" / run_id / "frozen"
    execution = FrozenRunExecution(
        frozen_dir / FIXTURE_SETUP,
        frozen_dir / FIXTURE_GATE,
        fixture_dir / "runs" / run_id / "executions",
        setup_file.content,
        setup_file.mode,
        gate_file.content,
        gate_file.mode,
        prompts,
        sandbox=frozen_inputs.sandbox,
        runtime_name=frozen_inputs.runtime,
        workspace=workspace,
        image=frozen_inputs.agent_image,
    )

    # Per-run branch + worktree: the main checkout is left on its branch.
    run_worktree = worktree_root / f"run-{run_id}"
    create_branch(run_branch, branch_start, runner=runner, cwd=workspace)
    # A failed run-worktree add is fatal: no issue can be worked without it, so
    # raise rather than silently drive the batch against a missing directory (#64).
    add_worktree(run_worktree, run_branch, runner=runner, cwd=workspace)
    run = RunContext(
        run_id=run_id,
        branch=run_branch,
        worktree=run_worktree,
        fixture_dir=fixture_dir,
        runner=runner,
    )
    _append_log(
        fixture_dir,
        run_id,
        f"Run {run_id}: {len(candidates)} candidate(s) on {run_branch} "
        f"(base {branch_start})",
    )

    cancellation = CancellationState()
    try:
        with _sigint_as_keyboard_interrupt():
            execution.invoke_setup(
                run_worktree, scope="run", identity="run-bootstrap", ordinal=1
            )
            if run_definition.before is not None:
                if verbose:
                    runtime.transcript_sink = _run_transcript_sink(  # type: ignore[attr-defined]
                        fixture_dir, run_id, "before-Run"
                    )
                before, _ = _walk_execution_graph(
                    None,
                    runtime=runtime,
                    worktree=run_worktree,
                    graph=run_definition.before,
                    execution=execution,
                    context=render_run_context(run_id, candidates, []),
                    scope="run",
                    identity="before-run",
                    checkpoint=lambda node: _checkpoint_run_node(
                        node, run=run, scope="before-Run"
                    ),
                )
            else:
                before = None
    except KeyboardInterrupt:
        _cleanup_cancelled(
            issue=cancellation.in_flight,
            issue_source=issue_source,
            worktree_root=worktree_root,
            workspace=workspace,
            run=run,
        )
        raise
    except SetupError as exc:
        outcome.succeeded = False
        outcome.stopping_point = "Run bootstrap Setup"
        outcome.setup_failure = exc.failure
        _append_log(fixture_dir, run_id, outcome.stopping_point)
        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
        delete_local_branch(run_branch, runner=runner, cwd=workspace)
        return outcome
    except RunCheckpointError as exc:
        outcome.succeeded = False
        outcome.stopping_point = f"before-Run checkpoint: {exc}"
        _append_log(fixture_dir, run_id, outcome.stopping_point)
        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
        _delete_failed_run_branch(run, workspace=workspace)
        return outcome
    if before is not None and before.terminal is HUMAN:
        outcome.succeeded = False
        outcome.stopping_point = "before-Run HUMAN"
        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
        _delete_failed_run_branch(run, workspace=workspace)
        return outcome

    # Track the issue currently in flight so an interrupt (SIGINT) or any
    # exception mid-issue can clean up that issue's worktree and restore its
    # ready state. SIGINT is turned into a KeyboardInterrupt so it unwinds here.
    with _sigint_as_keyboard_interrupt():
        try:
            remaining = list(candidates)
            claimed_attempts = 0
            selection_round = 0
            while remaining and claimed_attempts < iterations:
                selection_round += 1
                try:
                    with _disposable_selection_worktree(
                        run,
                        worktree_root=worktree_root,
                        workspace=workspace,
                    ) as selection_worktree:
                        issue = _select_item(
                            remaining,
                            outcome.issues,
                            runtime=runtime,
                            worktree=selection_worktree,
                            prompt_name=run_definition.item.selection.prompt,
                            execution=execution,
                            fixture_dir=fixture_dir,
                            run_id=run_id,
                            round_number=selection_round,
                            remaining_attempt_capacity=iterations - claimed_attempts,
                            attempted=outcome.attempted,
                            stale=outcome.stale,
                        )
                except Exception as exc:
                    outcome.succeeded = False
                    outcome.stopping_point = "Item selection"
                    outcome.selection_failure_checkpoint = (
                        run.selection_failure_checkpoint
                    )
                    if isinstance(exc, ItemSelectionError):
                        outcome.selection_failure = exc.code
                    else:
                        outcome.selection_failure = (
                            SelectionFailure.INFRASTRUCTURE_FAILED
                        )
                        if isinstance(exc, SetupError):
                            outcome.setup_failure = exc.failure
                    try:
                        _append_log(
                            fixture_dir,
                            run_id,
                            "Item selection failed; details retained in local "
                            "Run records.",
                        )
                    except Exception:
                        logger.exception(
                            "Could not retain the Item selection failure summary."
                        )
                    break
                if issue is None:
                    outcome.selection_end = SelectionEnd.POLICY_HALT
                    _append_log(
                        fixture_dir,
                        run_id,
                        "Project policy halted Item selection.",
                    )
                    break
                remaining.remove(issue)
                outcome.selected.append(issue.number)
                _append_log(
                    fixture_dir,
                    run_id,
                    f"Selected Item #{issue.number}: {issue.title}",
                )
                try:
                    eligible = issue_source.is_still_eligible(
                        issue,
                        assignee=assignee,
                        include_unassigned=include_unassigned,
                    )
                    if not isinstance(eligible, bool):
                        raise TypeError("Item eligibility recheck did not return bool")
                except Exception as exc:
                    outcome.succeeded = False
                    outcome.stopping_point = (
                        f"Item #{issue.number} eligibility recheck failure: {exc}"
                    )
                    _append_log(fixture_dir, run_id, outcome.stopping_point)
                    break
                if not eligible:
                    outcome.stale.append(issue.number)
                    _append_log(
                        fixture_dir,
                        run_id,
                        f"Item #{issue.number} is stale; skipped without mutation",
                    )
                    continue
                claimed_attempts += 1
                outcome.attempted.append(issue.number)
                try:
                    item_outcome = _work_issue(
                        issue,
                        runtime=runtime,
                        issue_source=issue_source,
                        run=run,
                        worktree_root=worktree_root,
                        assignee=assignee,
                        workspace=workspace,
                        item_graph=run_definition.item.graph,
                        execution=execution,
                        cancellation=cancellation,
                        verbose=verbose,
                    )
                except SetupError as exc:
                    cleanup_worktree(
                        worktree_root / f"issue-{issue.number}",
                        runner=runner,
                        cwd=workspace,
                    )
                    issue_source.release(issue.number)
                    cancellation.in_flight = None
                    delete_local_branch(
                        issue_branch_name(issue), runner=runner, cwd=workspace
                    )
                    outcome.succeeded = False
                    outcome.stopping_point = f"Item #{issue.number} Setup"
                    outcome.setup_failure = exc.failure
                    _append_log(fixture_dir, run_id, outcome.stopping_point)
                    if not outcome.completed:
                        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
                        delete_local_branch(run_branch, runner=runner, cwd=workspace)
                        return outcome
                    break
                except Exception as exc:  # handled infrastructure boundary
                    cleanup_worktree(
                        worktree_root / f"issue-{issue.number}",
                        runner=runner,
                        cwd=workspace,
                    )
                    try:
                        issue_source.release(issue.number)
                    except Exception:
                        logger.exception(
                            "Could not release Item #%s after infrastructure failure.",
                            issue.number,
                        )
                    cancellation.in_flight = None
                    if isinstance(exc, ItemClaimError):
                        claimed_attempts -= 1
                        outcome.attempted.pop()
                    runner(
                        ["git", "branch", "-D", issue_branch_name(issue)],
                        capture=True,
                        cwd=workspace,
                    )
                    outcome.succeeded = False
                    outcome.stopping_point = (
                        f"Item #{issue.number} infrastructure failure"
                    )
                    _append_log(fixture_dir, run_id, outcome.stopping_point)
                    break
                outcome.issues.append(item_outcome)
            if outcome.succeeded and outcome.selection_end is None:
                if claimed_attempts >= iterations:
                    outcome.selection_end = SelectionEnd.ATTEMPT_LIMIT_REACHED
                elif not remaining:
                    outcome.selection_end = SelectionEnd.CANDIDATE_POOL_EXHAUSTED
        except KeyboardInterrupt:
            _cleanup_cancelled(
                issue=cancellation.in_flight,
                issue_source=issue_source,
                worktree_root=worktree_root,
                workspace=workspace,
                run=run,
            )
            raise

    completed = outcome.completed
    if completed:
        run_gate: GateOutcome | None = None
        publication_error: str | None = None
        suppress_report_harvest = outcome.selection_failure is not None
        try:
            with _sigint_as_keyboard_interrupt():
                if outcome.succeeded:
                    if run_definition.after is not None:
                        if verbose:
                            runtime.transcript_sink = _run_transcript_sink(  # type: ignore[attr-defined]
                                fixture_dir, run_id, "after-Run"
                            )
                        after, run_gate = _walk_execution_graph(
                            None,
                            runtime=runtime,
                            worktree=run_worktree,
                            graph=run_definition.after,
                            execution=execution,
                            context=render_run_context(
                                run_id,
                                candidates,
                                outcome.issues,
                                stale=outcome.stale,
                                selection_end=outcome.selection_end,
                            ),
                            scope="run",
                            identity="after-run",
                            checkpoint=lambda node: _checkpoint_run_node(
                                node, run=run, scope="after-Run"
                            ),
                        )
                        if after.terminal is HUMAN:
                            outcome.succeeded = False
                            outcome.stopping_point = "after-Run HUMAN"
        except SetupError as exc:
            suppress_report_harvest = True
            if outcome.succeeded:
                outcome.succeeded = False
                outcome.stopping_point = "after-Run Setup"
                outcome.setup_failure = exc.failure
            _append_log(fixture_dir, run_id, f"After-Run Setup failed: {exc}")
            try:
                _discard_incomplete_run_scope(run)
            except RunCheckpointError as cleanup_error:
                # Publication pushes committed history only. A failed worktree
                # restore must be visible, but must not strand checkpoints that
                # were already made durable before Setup failed.
                _append_log(fixture_dir, run_id, str(cleanup_error))
        except RunCheckpointError as exc:
            outcome.succeeded = False
            outcome.stopping_point = f"after-Run checkpoint: {exc}"
            _append_log(fixture_dir, run_id, outcome.stopping_point)
        except KeyboardInterrupt:
            _cleanup_cancelled(
                issue=cancellation.in_flight,
                issue_source=issue_source,
                worktree_root=worktree_root,
                workspace=workspace,
                run=run,
            )
            raise
        report, report_error = (
            (None, None) if suppress_report_harvest else _harvest_report(run)
        )
        if report_error:
            outcome.succeeded = False
            outcome.stopping_point = "Run report validation"
            publication_error = report_error
        publication = _open_pull_request(
            repo=repo,
            base_branch=base_branch,
            run=run,
            completed=completed,
            selected=outcome.selected,
            skipped=outcome.skipped,
            gate=run_gate,
            report=report,
            publication_error=publication_error,
            successful=outcome.succeeded,
            stopping_point=outcome.stopping_point,
            selection_failure=outcome.selection_failure,
            setup_failure=outcome.setup_failure,
        )
        outcome.pr_opened = publication.pr_opened
        outcome.pr_ready = publication.pr_ready
        if not publication.final_push_succeeded:
            outcome.succeeded = False
            outcome.stopping_point = "Final push"
        elif not publication.pr_opened:
            outcome.succeeded = False
            outcome.stopping_point = "Pull request publication"
        elif outcome.succeeded and not publication.report_published:
            outcome.succeeded = False
            outcome.stopping_point = "Run report publication"
        elif outcome.succeeded and not outcome.pr_ready:
            outcome.succeeded = False
            outcome.stopping_point = "Pull request ready transition"
    else:
        _append_log(fixture_dir, run_id, "No issues merged; opening no pull request.")

    cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
    if not completed:
        _delete_failed_run_branch(run, workspace=workspace)
    return outcome


def _open_pull_request(
    *,
    repo: str,
    base_branch: str,
    run: RunContext,
    completed: list[int],
    selected: Sequence[int],
    skipped: list[int],
    gate: GateOutcome | None,
    report: str | None,
    publication_error: str | None,
    successful: bool,
    stopping_point: str | None,
    selection_failure: SelectionFailure | None,
    setup_failure: SetupFailure | None = None,
) -> PublicationOutcome:
    """Final-push, draft-create, report, then ready a successful Run PR."""
    if not _push_run_branch(run=run, final=True):
        return PublicationOutcome()
    closes = "\n".join(f"- Closes #{number}" for number in completed)
    body = (
        f"Automated PyCastle run {run.run_id} completing {len(completed)} issue(s).\n\n"
        f"{closes}\n"
    )
    try:
        existing = run.runner(
            [
                "gh",
                "pr",
                "list",
                "-R",
                repo,
                "--head",
                run.branch,
                "--state",
                "open",
                "--json",
                "number",
            ],
            capture=True,
        )
    except OSError as exc:
        _append_log(
            run.fixture_dir,
            run.run_id,
            f"Pull request lookup failed ({exc}); refusing to create a possible duplicate.",
        )
        return PublicationOutcome(final_push_succeeded=True)
    if getattr(existing, "returncode", 1) != 0:
        _append_log(
            run.fixture_dir,
            run.run_id,
            "Pull request lookup failed; refusing to create a possible duplicate.",
        )
        return PublicationOutcome(final_push_succeeded=True)
    pr_number: int | None = None
    try:
        raw_rows = getattr(existing, "stdout", None)
        if not isinstance(raw_rows, str) or not raw_rows.strip():
            raise TypeError
        rows = json.loads(raw_rows)
        if not isinstance(rows, list):
            raise TypeError
        if rows:
            pr_number = int(rows[0]["number"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        _append_log(
            run.fixture_dir,
            run.run_id,
            "Pull request lookup returned invalid data; refusing to create a possible duplicate.",
        )
        return PublicationOutcome(final_push_succeeded=True)
    pr = existing
    if pr_number is None:
        try:
            pr = run.runner(
                [
                    "gh",
                    "pr",
                    "create",
                    "-R",
                    repo,
                    "--base",
                    base_branch,
                    "--head",
                    run.branch,
                    "--title",
                    f"pycastle: run {run.run_id}",
                    "--body",
                    body,
                    "--draft",
                ],
                capture=True,
            )
        except OSError as exc:
            records = _telemetry_dir(run.fixture_dir, run.run_id)
            _append_log(
                run.fixture_dir,
                run.run_id,
                f"Pull request creation failed ({exc}); pushed branch origin/"
                f"{run.branch} and retained records at {records}.",
            )
            return PublicationOutcome(final_push_succeeded=True)
    opened = getattr(pr, "returncode", 1) == 0
    _append_log(
        run.fixture_dir,
        run.run_id,
        (
            f"Pull request opened for {run.branch}"
            if opened
            else f"Pull request creation failed; pushed branch origin/{run.branch} "
            f"and retained records at "
            f"{_telemetry_dir(run.fixture_dir, run.run_id)}."
        ),
    )
    if not opened:
        return PublicationOutcome(final_push_succeeded=True)

    if pr_number is None:
        view = run.runner(
            [
                "gh",
                "pr",
                "view",
                run.branch,
                "-R",
                repo,
                "--json",
                "number",
                "--jq",
                ".number",
            ],
            capture=True,
        )
        try:
            if getattr(view, "returncode", 1) != 0:
                raise ValueError
            pr_number = int((getattr(view, "stdout", "") or "").strip())
        except ValueError:
            pr_number = None
            _append_log(
                run.fixture_dir,
                run.run_id,
                "Pull request number lookup failed; publishing by branch.",
            )

    state = "complete" if successful else "draft"
    gate_line = "not run"
    if gate is not None:
        termination = gate.termination or {}
        kind = termination.get("kind")
        if kind == "signaled":
            result = f"signal {termination.get('signal')}"
        elif kind == "launch_error":
            result = f"launch error {termination.get('error_kind', 'unknown')}"
        else:
            result = (
                f"exit {termination['code']}" if "code" in termination else "unknown"
            )
        gate_line = (
            f"`{gate.command}` — {'PASS' if gate.passed else 'FAIL'} "
            f"({result}, {gate.duration_seconds:.2f}s)"
        )
    marker = f"<!-- pycastle-run-report:{run.run_id} -->"
    comment = (
        f"{marker}\n## PyCastle Run {run.run_id}\n\n"
        f"- State: **{state}**\n"
        f"- Selected Items: {', '.join(f'#{n}' for n in selected) or 'none'}\n"
        f"- Completed Items: {', '.join(f'#{n}' for n in completed) or 'none'}\n"
        f"- Skipped Items: {', '.join(f'#{n}' for n in skipped) or 'none'}\n"
        f"- Run Gate: {gate_line}\n"
    )
    if stopping_point:
        comment += f"- Stopping point: {stopping_point}\n"
    if selection_failure:
        comment += f"- Item selection failure: `{selection_failure}`\n"
    if setup_failure is not None:
        termination = setup_failure.termination
        kind = termination.get("kind")
        if kind == "exited":
            setup_result = f"exit {termination.get('code')}"
        elif kind == "signaled":
            setup_result = f"signal {termination.get('signal')}"
        elif kind == "launch_error":
            setup_result = f"launch error {termination.get('error_kind', 'unknown')}"
        elif kind == "record_persistence_error":
            setup_result = "record persistence error"
        else:
            setup_result = "orchestration error"
        comment += f"- Setup: `{setup_failure.command}` — {setup_result}\n"
    if publication_error:
        comment += f"\n> Run report validation error: {publication_error}\n"
    elif report is not None:
        comment += "\n---\n\n" + report

    comment_argv = [
        "gh",
        "pr",
        "comment",
        str(pr_number or run.branch),
        "-R",
        repo,
        "--body",
        comment,
    ]
    if pr_number is not None:
        try:
            listed = run.runner(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/issues/{pr_number}/comments",
                    "--paginate",
                ],
                capture=True,
            )
        except OSError as exc:
            _append_log(
                run.fixture_dir,
                run.run_id,
                f"Run report lookup failed ({exc}); PR remains draft.",
            )
            return PublicationOutcome(
                pr_opened=True,
                final_push_succeeded=True,
            )
        if getattr(listed, "returncode", 1) != 0:
            _append_log(
                run.fixture_dir,
                run.run_id,
                "Run report lookup failed; PR remains draft.",
            )
            return PublicationOutcome(
                pr_opened=True,
                final_push_succeeded=True,
            )
        comment_id: int | None = None
        try:
            raw_comments = getattr(listed, "stdout", None)
            if not isinstance(raw_comments, str) or not raw_comments.strip():
                raise TypeError
            comments = json.loads(raw_comments)
            if not isinstance(comments, list):
                raise TypeError
            for candidate in comments:
                if not isinstance(candidate, dict):
                    raise TypeError
                if marker in candidate.get("body", ""):
                    comment_id = int(candidate["id"])
                    break
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            _append_log(
                run.fixture_dir,
                run.run_id,
                "Run report lookup returned invalid data; PR remains draft.",
            )
            return PublicationOutcome(
                pr_opened=True,
                final_push_succeeded=True,
            )
        endpoint = (
            f"repos/{repo}/issues/comments/{comment_id}"
            if comment_id is not None
            else f"repos/{repo}/issues/{pr_number}/comments"
        )
        method = "PATCH" if comment_id is not None else "POST"
        comment_argv = [
            "gh",
            "api",
            "--method",
            method,
            endpoint,
            "-f",
            f"body={comment}",
        ]
    published = run.runner(comment_argv, capture=True)
    if getattr(published, "returncode", 1) != 0:
        _append_log(
            run.fixture_dir,
            run.run_id,
            "Run report publication failed; PR remains draft.",
        )
        return PublicationOutcome(pr_opened=True, final_push_succeeded=True)
    ready_succeeded = False
    if successful:
        ready = run.runner(
            ["gh", "pr", "ready", str(pr_number or run.branch), "-R", repo],
            capture=True,
        )
        if getattr(ready, "returncode", 1) != 0:
            _append_log(
                run.fixture_dir,
                run.run_id,
                "Ready transition failed; PR remains draft.",
            )
        else:
            ready_succeeded = True
    return PublicationOutcome(
        pr_opened=True,
        report_published=True,
        pr_ready=ready_succeeded,
        final_push_succeeded=True,
    )


def _push_run_branch(
    *,
    run: RunContext,
    final: bool,
    scope: str = "Run",
    node: str | None = None,
) -> bool:
    """Push the current Run checkpoint, logging failures without raising."""
    if final and run.selection_failure_checkpoint is not None:
        argv = [
            "git",
            "push",
            "origin",
            (f"{run.selection_failure_checkpoint}:refs/heads/{run.branch}"),
        ]
    else:
        argv = ["git", "push", "-u", "origin", run.branch]
    node_name = node or ("final-push" if final else "durability-push")
    try:
        result = run.runner(
            argv,
            capture=True,
            cwd=run.worktree,
        )
        succeeded = getattr(result, "returncode", 1) == 0
        if not succeeded:
            _record_host_command_failure(
                run,
                scope=scope,
                node=node_name,
                argv=argv,
                result=result,
            )
    except OSError as exc:
        succeeded = False
        _record_host_command_exception(
            run,
            scope=scope,
            node=node_name,
            argv=argv,
            exc=exc,
        )

    if succeeded:
        if not final:
            run.remote_checkpoint_succeeded = True
        _append_log(run.fixture_dir, run.run_id, f"Pushed Run checkpoint {run.branch}")
        return True

    kind = "Final" if final else "Durability"
    _append_log(
        run.fixture_dir,
        run.run_id,
        f"{kind} push failed for {run.branch}; remote checkpoint was not updated.",
    )
    logger.warning("%s push failed for Run branch %s", kind, run.branch)
    return False
