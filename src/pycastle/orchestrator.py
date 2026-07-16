"""The run lifecycle: turn a batch of ready issues into one pull request.

A run selects up to N ready, appropriately-assigned issues and works them as a
bounded batch. It cuts a per-run branch in its own worktree (the main checkout
stays untouched), then works each issue in its own worktree off that run branch:
run the graph, commit, and on a clean merge fold the issue branch back into the
run branch. Successful issues are accumulated and a single pull request is opened
for the whole run. Per-phase provider telemetry and a run log are written into
the Project fixture under ``.pycastle/runs/<run_id>/`` (an ignored path, so run
output is never committed).

A failed implement attempt (an agent crash, or a clean run whose gates come
back red) is retried in place on the same worktree (#8): a handoff document is
written summarising what was tried and what to fix, and the next attempt carries
that context. For Codex the handoff resumes the thread that did the failed
attempt; Claude has no thread resume, so its handoff is a fresh call carrying
the prior-attempt context. An item that exhausts its retries is labelled
``ready-for-human`` and the run continues to the next item.

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
import re
import shutil
import signal
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from . import sandbox as sandbox_mod
from .commands import run_cmd
from .execution import execute_hook, project_gate_evidence
from .graph import (
    HUMAN,
    GateNode,
    GraphExecutor,
    NodeOutcome,
    Phase,
    PhaseGraph,
    PhaseResult,
    RunDefinition,
    RuntimeNode,
    WalkResult,
    load_run,
    walk_execution_graph,
)
from .issues import IssueSource
from .models import IssueRef
from .runtime import AgentCrashError, CodexRuntime, Runtime

logger = logging.getLogger(__name__)

Runner = Callable[..., Any]


class WorktreeError(RuntimeError):
    """A ``git worktree add`` exited non-zero and created no worktree.

    Every git call here runs with ``check=False``, so a failing
    ``git worktree add`` (a path collision, a checkout conflict, no disk) was
    silently ignored and the run drove the Runtime against a worktree that never
    existed — cascading into confusing downstream errors (#64). Raised instead so
    the failure surfaces at its source, carrying git's captured output.
    """


class BranchError(RuntimeError):
    """A prerequisite ``git branch`` exited non-zero.

    Branch creation must succeed before its worktree is added. Otherwise a stale
    branch with the requested name can be checked out successfully from the wrong
    base, recreating the wrong-tree failure that worktree validation prevents.
    """


class PruneError(RuntimeError):
    """Run-branch discovery or deletion failed, making pruning unsafe."""


class RunCheckpointError(RuntimeError):
    """A successful Run phase could not be committed durably."""


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


#: A gate check decides whether an implement attempt's quality gates passed.
#: It takes the issue worktree and returns a :class:`GateOutcome` whose
#: ``passed`` is the green/red verdict and whose ``output`` is the captured gate
#: text. It is injectable so a "gates red" outcome can drive a retry without
#: hardcoding a specific project gate command here; the default treats every
#: attempt as passing (the real gate is the project's own and is wired by the
#: caller).
GateCheck = Callable[[Path], "GateOutcome"]
Setup = Callable[[Path], None]

#: Where a handoff document is written inside the issue worktree (ignored path).
HANDOFF_DOC = ".pycastle/handoff.md"

#: The phase name used for the handoff invocation's telemetry.
HANDOFF_PHASE = "handoff"


@dataclass
class GateOutcome:
    """What running an attempt's quality gate produced.

    ``passed`` is the green/red verdict (the gate exited 0); ``output`` is the
    captured stdout+stderr the orchestrator surfaces (logged on failure, and
    persisted into the per-issue transcript so a run is auditable). Replaces the
    bare bool the gate check used to return so the gate's reasoning is no longer
    discarded (#28).
    """

    passed: bool
    output: str
    exit_code: int = 0
    duration_seconds: float = 0.0
    command: str = ".pycastle/gate"


def _gates_always_pass(_worktree: Path) -> GateOutcome:
    """Default gate check: treat every attempt as passing, with no output.

    The real gate is the project's own quality-gate command; it is injected by
    the caller. With no gate wired, a single implement attempt is made and no
    retry/handoff is triggered.
    """
    return GateOutcome(passed=True, output="")


#: The optional project-owned quality gate, relative to the Project fixture.
#: If this file exists it is run (as an executable) inside the issue worktree
#: after the implement phase; exit 0 means the gates passed, any non-zero exit
#: means they failed and the attempt is retried with a handoff. The file is
#: project-owned (it lives in and travels with ``.pycastle/``), so each project
#: decides its own gate without the runner hardcoding a command. ``pycastle
#: init`` (#11) will scaffold a default ``gate`` file matching this convention.
FIXTURE_GATE = "gate"
FIXTURE_SETUP = "setup"


class SetupError(RuntimeError):
    """The project-owned setup hook could not prepare an issue worktree."""


@dataclass
class ExplicitItemExecution:
    """Frozen host hook inputs and deterministic per-visit record allocation."""

    setup: Path
    gate: Path
    records: Path

    @staticmethod
    def _record_identity(value: str) -> str:
        """Return a readable, collision-resistant filename component."""
        readable = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-") or "node"
        digest = hashlib.sha256(value.encode()).hexdigest()[:12]
        return f"{readable[:48]}-{digest}"

    @classmethod
    def freeze(cls, fixture_dir: Path, run_id: str) -> ExplicitItemExecution:
        fixture_dir = fixture_dir.resolve()
        root = fixture_dir / "runs" / run_id
        frozen = root / "frozen"
        frozen.mkdir(parents=True, exist_ok=True)
        paths: dict[str, Path] = {}
        for name in (FIXTURE_SETUP, FIXTURE_GATE):
            source = fixture_dir / name
            destination = frozen / name
            shutil.copyfile(source, destination)
            destination.chmod(source.stat().st_mode & 0o777)
            paths[name] = destination
        return cls(paths[FIXTURE_SETUP], paths[FIXTURE_GATE], root / "executions")

    def invoke_setup(
        self,
        worktree: Path,
        *,
        scope: Literal["item", "run"],
        identity: str,
        ordinal: int,
    ) -> None:
        record = execute_hook(
            self.setup,
            cwd=worktree,
            scope=scope,
            record_path=self.records
            / f"{self._record_identity(identity)}-setup-{ordinal}.json",
        )
        if not record.success:
            raise SetupError(f"Project setup failed: {record.termination}")

    def invoke_gate(
        self, worktree: Path, *, identity: str, node: str, ordinal: int
    ) -> NodeOutcome:
        record = execute_hook(
            self.gate,
            cwd=worktree,
            scope="item",
            record_path=self.records
            / (
                f"{self._record_identity(identity)}-"
                f"{self._record_identity(node)}-gate-{ordinal}.json"
            ),
        )
        return NodeOutcome(record.success, project_gate_evidence(record, node=node))


def make_fixture_setup(
    fixture_dir: Path,
    *,
    runner: Runner = run_cmd,
    sandbox: str = "host",
    image: str | None = None,
    runtime_name: str = "claude",
    workspace: Path | None = None,
) -> Setup:
    """Build the optional project-owned setup hook for the selected sandbox.

    The canonical fixture executable runs with the issue worktree as its cwd,
    immediately before that issue's phase graph is walked. Missing hooks are a
    backward-compatible no-op. Launch errors and non-zero exits raise
    :class:`SetupError`, because an unprepared sandbox is a run infrastructure
    failure rather than a phase outcome that should follow a graph edge.
    """
    setup_path = fixture_dir / FIXTURE_SETUP

    def _setup(worktree: Path) -> None:
        if not setup_path.is_file():
            return
        try:
            if sandbox == "docker":
                argv = sandbox_mod.build_run_command(
                    runtime_name,
                    inner_argv=["bash", str(setup_path.resolve())],
                    workspace=workspace or Path.cwd(),
                    workdir=worktree,
                    image=image or sandbox_mod.DEFAULT_IMAGE,
                )
                result = runner(argv, capture=True)
            else:
                result = runner([str(setup_path.resolve())], cwd=worktree, capture=True)
        except OSError as exc:
            raise SetupError(f"Project setup could not be launched: {exc}") from exc
        if getattr(result, "returncode", 1) != 0:
            output = (getattr(result, "stdout", "") or "") + (
                getattr(result, "stderr", "") or ""
            )
            raise SetupError(f"Project setup failed:\n{output}".rstrip())

    return _setup


def _gate_outcome_from_result(result: Any) -> GateOutcome:
    """Build a :class:`GateOutcome` from a runner's completed-process result.

    Captures the gate's combined stdout+stderr (so the orchestrator can surface
    the actual failing checks) and reads the verdict off the exit code: 0 is a
    pass, anything else (including a missing/odd returncode) is a failure.
    """
    output = (getattr(result, "stdout", "") or "") + (
        getattr(result, "stderr", "") or ""
    )
    exit_code = getattr(result, "returncode", 1)
    return GateOutcome(passed=exit_code == 0, output=output, exit_code=exit_code)


def make_fixture_gate_check(
    fixture_dir: Path,
    *,
    runner: Runner = run_cmd,
    sandbox: str = "host",
    image: str | None = None,
    runtime_name: str = "claude",
    workspace: Path | None = None,
) -> GateCheck:
    """Build a :data:`GateCheck` from the Project fixture's gate file.

    The gate definition is project-owned: if ``<fixture_dir>/gate`` exists it is
    run (so it sees the attempt's code, not the fixture), and the returned check
    passes only when that command exits 0, carrying the captured output back in
    the :class:`GateOutcome`. With no gate file the fixture opts out of gating
    and the check falls back to :func:`_gates_always_pass`, so a project without
    a gate keeps the single-attempt, no-retry behaviour (back-compat).

    The gate runs where the phases run, driven by the single ``--sandbox`` flag
    (#28) so gate and runtime are always on the same side:

    * ``sandbox="host"`` (the default) runs the gate as a host subprocess inside
      the issue worktree, exactly as before — byte-for-byte unchanged behaviour
      for existing callers.
    * ``sandbox="docker"`` runs the gate INSIDE the same resolved agent image as
      the phases, wrapped through :func:`pycastle.sandbox.build_run_command` (the
      same wrapper the runtime uses, #50 workdir fix included). The repo root
      (``workspace``) is bind-mounted at its own path and the issue ``worktree``
      becomes the container ``-w``, so the gate sees the same cwd it does on the
      host. The inner argv runs ``bash`` on the *canonical* repo-root gate
      (``fixture_dir/gate``), NOT the worktree's copy, so an attempt cannot
      weaken its own gate; the canonical absolute path is valid in the container
      because the repo root is mounted at the same path. The runtime's auth mount
      is inert for the gate, so ``build_run_command`` is reused as-is rather than
      building a leaner gate-only wrapper.

    The gate is resolved once here, but read freshly per call so a fixture that
    grows or drops its gate between runs is honoured. ``runner`` is injectable so
    the subprocess can be mocked in tests; production passes the real
    :func:`~pycastle.commands.run_cmd`.

    A gate that exists but cannot be launched — it lost its executable bit on
    checkout (``PermissionError``), or has a bad interpreter line
    (``FileNotFoundError``/``ENOEXEC``), or the ``docker run`` itself cannot be
    spawned — is treated as a *failing* gate, not a crash: the :class:`OSError`
    is logged and the check returns a failing :class:`GateOutcome` so the attempt
    is retried and the issue ultimately handed to a human, rather than one bad
    file mode sinking the whole run.
    """
    gate_path = fixture_dir / FIXTURE_GATE

    def _check(worktree: Path) -> GateOutcome:
        if not gate_path.is_file():
            return GateOutcome(passed=True, output="")
        started = time.monotonic()
        try:
            if sandbox == "docker":
                # Wrap the canonical gate through the same docker wrapper the
                # runtime uses: repo root mounted at its own path, worktree as the
                # container cwd (-w). ``docker run`` is launched from the host with
                # no cwd dependence, so no ``cwd=`` is passed here.
                argv = sandbox_mod.build_run_command(
                    runtime_name,
                    inner_argv=["bash", str(gate_path.resolve())],
                    workspace=workspace or Path.cwd(),
                    workdir=worktree,
                    image=image or sandbox_mod.DEFAULT_IMAGE,
                )
                result = runner(argv, capture=True)
            else:
                result = runner([str(gate_path.resolve())], cwd=worktree, capture=True)
        except OSError as exc:
            # The gate file is there but cannot be executed (lost +x on checkout,
            # bad shebang, docker not spawnable, ...). Count it as a gate failure
            # so the run retries and hands the issue to a human instead of
            # aborting the whole batch.
            logger.exception("Could not run quality gate %s", gate_path)
            return GateOutcome(
                passed=False,
                output=f"gate could not be launched: {exc}",
                exit_code=126,
                duration_seconds=time.monotonic() - started,
            )
        outcome = _gate_outcome_from_result(result)
        outcome.duration_seconds = time.monotonic() - started
        return outcome

    return _check


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
    issues: list[IssueOutcome] = field(default_factory=list)
    pr_opened: bool = False
    pr_ready: bool = False
    succeeded: bool = True
    stopping_point: str | None = None

    @property
    def completed(self) -> list[int]:
        """Issue numbers that merged cleanly into the run branch."""
        return [o.issue.number for o in self.issues if o.merged]

    @property
    def skipped(self) -> list[int]:
        """Selected issue numbers not folded into the Run branch."""
        completed = set(self.completed)
        return [number for number in self.selected if number not in completed]


@dataclass
class RunContext:
    """Host-side identity and command boundary for one active Run worktree."""

    run_id: str
    branch: str
    worktree: Path
    fixture_dir: Path
    runner: Runner
    remote_checkpoint_succeeded: bool = False


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
    """Format an issue as the preamble handed to the runtime each phase.

    The phase prompts tell the runtime to read the issue's "What to build" and
    "Acceptance criteria", so it must actually be handed the issue. This renders
    a ``# Issue #<n>: <title>`` header followed by the body when non-empty, then
    every author-attributed issue comment in source order. The title keeps its
    punctuation and markdown (unlike :func:`slugify`). Missing parts are omitted,
    so an issue with no comments renders byte-for-byte as it did before comments
    were added to :class:`~pycastle.models.IssueRef`.
    """
    header = f"# Issue #{issue.number}: {issue.title}".rstrip()
    body = issue.body.strip()
    parts = [header]
    if body:
        parts.append(body)
    if issue.comments:
        comments = "\n\n".join(
            f"### @{comment.author}\n\n{comment.body.strip()}"
            for comment in issue.comments
        )
        parts.append(f"## Issue Comments\n\n{comments}")
    return "\n\n".join(parts)


def _telemetry_dir(fixture_dir: Path, run_id: str) -> Path:
    """Return (and create) the ignored per-run telemetry/log directory."""
    run_dir = fixture_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_telemetry(
    fixture_dir: Path,
    run_id: str,
    issue: IssueRef,
    phase_results: list[PhaseResult],
) -> None:
    """Write per-phase telemetry for one issue into the Project fixture.

    Telemetry comes from each :class:`~pycastle.graph.PhaseResult`'s
    ``result.telemetry`` (a pydantic model dumped with ``model_dump``). Only the
    cost/duration/turns/token counts are recorded; the agent's prose output is
    not, so nothing credential-like is written.
    """
    run_dir = _telemetry_dir(fixture_dir, run_id)
    records = [pr.result.telemetry.model_dump(mode="json") for pr in phase_results]
    path = run_dir / f"issue-{issue.number}-telemetry.json"
    path.write_text(json.dumps(records, indent=2) + "\n")


def _transcript_sink(
    fixture_dir: Path, run_id: str, issue_number: int
) -> Callable[[str, str, str], None]:
    """Build a sink that persists the agent's transcript for one issue.

    The runtime surfaces both the agent's thinking and its output but does not
    know ``run_id`` or the issue number (its :meth:`run` only gets ``cwd`` and
    ``phase``), so the orchestrator — which owns both — binds this sink onto the
    runtime per issue (#48, #52). Each chunk is appended to
    ``.pycastle/runs/<run_id>/issue-<n>-transcript.log``, tagged with its stream
    and prefixed with its phase, so OUTPUT and THINKING interleave in one file in
    chronological order (matching the predecessor ralph runner's single-file
    model), beside the per-issue telemetry and the run log. Neither stream is
    credentials, so writing them to the (gitignored) run dir is fine; it makes a
    finished run auditable even if nobody watched it live.
    """
    run_dir = _telemetry_dir(fixture_dir, run_id)
    path = run_dir / f"issue-{issue_number}-transcript.log"

    def _sink(phase: str, tag: str, text: str) -> None:
        with path.open("a") as handle:
            handle.write(f"[{phase}] [{tag}] {text}\n")

    return _sink


def _run_transcript_sink(
    fixture_dir: Path, run_id: str, scope: str
) -> Callable[[str, str, str], None]:
    """Build a sink for one before-Run or after-Run phase graph."""
    path = _telemetry_dir(fixture_dir, run_id) / "run-phase-transcript.log"

    def _sink(phase: str, tag: str, text: str) -> None:
        with path.open("a") as handle:
            lines = text.splitlines() or [""]
            for line in lines:
                handle.write(f"[{scope}] [{phase}] [{tag}] {line}\n")

    return _sink


def _append_run_telemetry(
    fixture_dir: Path,
    run_id: str,
    scope: str,
    phase_results: list[PhaseResult],
) -> None:
    """Append scoped Run-phase telemetry in lifecycle order."""
    path = _telemetry_dir(fixture_dir, run_id) / "run-phase-telemetry.json"
    records = json.loads(path.read_text()) if path.exists() else []
    records.extend(
        {
            "scope": scope,
            **phase_result.result.telemetry.model_dump(mode="json"),
            "phase": phase_result.phase,
        }
        for phase_result in phase_results
    )
    path.write_text(json.dumps(records, indent=2) + "\n")


def _surface_gate(sink: Callable[[str, str, str], None], outcome: GateOutcome) -> None:
    """Persist the gate's captured output into the per-issue transcript.

    Tagged ``GATE`` under phase ``gate`` so lines read ``[gate] [GATE] <text>``,
    matching the runtime's transcript format. The caller decides *when* to call
    this — always on failure, only under ``--verbose`` on success (#28). Output
    is split per line so each line keeps the ``[gate] [GATE] `` prefix, and a
    gate that produced no output writes nothing (no empty tagged line).
    """
    if not outcome.output:
        return
    for line in outcome.output.splitlines():
        sink("gate", "GATE", line)


def _append_log(fixture_dir: Path, run_id: str, message: str) -> None:
    """Append one line to the run log and emit it through ``logging``."""
    logger.info(message)
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


_HANDOFF_PROMPT = (
    "A previous implement attempt left the quality gates red. Write a handoff "
    f"document at {HANDOFF_DOC} for the next attempt. Summarise, briefly: what "
    "you attempted, the current state of the code, which files you touched, and "
    "what to try next to fix the failing gates. Reference the issue and the diff "
    "by path; do not duplicate their content.\n\n"
    "## Failing gate output\n\n```\n{gate_output}\n```\n"
)


def generate_handoff(
    runtime: Runtime,
    *,
    worktree: Path,
    thread_id: str | None,
    gate_output: str,
) -> bool:
    """Have the runtime write a handoff document for the next attempt.

    The handoff captures what the failed attempt tried and what to fix next. For
    Codex (a runtime whose :meth:`run` accepts ``resume_thread_id``) the handoff
    resumes the thread that produced the failed attempt, so it keeps the original
    context; ``thread_id`` is the failed attempt's thread. Claude has no thread
    resume, so its handoff is a fresh ``run`` carrying the prior-attempt context
    in the prompt. Returns whether the document was created — a runtime that
    fails to write it degrades to a fresh retry rather than aborting the issue.
    """
    prompt = _HANDOFF_PROMPT.format(gate_output=gate_output)
    if _runtime_resumes_threads(runtime) and thread_id is not None:
        # Codex: resume the failed attempt's thread to keep its context.
        runtime.run(  # type: ignore[call-arg]
            prompt,
            cwd=worktree,
            phase=HANDOFF_PHASE,
            resume_thread_id=thread_id,
        )
    else:
        # Claude (or any non-resuming runtime): a fresh call with the context.
        runtime.run(prompt, cwd=worktree, phase=HANDOFF_PHASE)
    return (worktree / HANDOFF_DOC).is_file()


def _runtime_resumes_threads(runtime: Runtime) -> bool:
    """Whether a runtime can resume the thread that did the failed attempt.

    Thread resume (``run(..., resume_thread_id=...)``) is a Codex capability,
    not part of the Runtime Protocol, so we narrow on the Codex runtime — by
    concrete type, or by its ``name`` so a stand-in Codex runtime is recognised
    too — rather than forcing ``resume_thread_id`` onto every runtime.
    """
    return isinstance(runtime, CodexRuntime) or runtime.name == CodexRuntime.name


def _retry_context(attempt: int, gate_output: str, *, handoff_made: bool) -> str:
    """Build the prior-attempt context threaded into the next implement prompt.

    It points the next attempt at the handoff document and the failing gate
    output rather than duplicating either inline. The handoff path is named even
    when this attempt did not produce one (a crash skips the handoff) so the
    next attempt reads it if present.
    """
    handoff_note = (
        "the handoff document from the previous attempt"
        if handoff_made
        else "the handoff document, if the previous attempt left one"
    )
    lines = [
        "## Previous Attempt",
        "",
        f"Attempt {attempt} was made but a quality gate is still failing.",
        "",
        f"- Read {handoff_note}: `{HANDOFF_DOC}`",
        "- The failing gate output was:",
        "",
        "```",
        gate_output,
        "```",
        "",
        "Fix the failing gates before finishing.",
    ]
    return "\n".join(lines)


def _run_implement_phase(
    issue: IssueRef,
    implement: Phase,
    extra: str | None,
    *,
    executor: GraphExecutor,
    runtime: Runtime,
    fixture_dir: Path,
    run_id: str,
    issue_worktree: Path,
    impl_retries: int,
    gate_check: GateCheck,
    gate_sink: Callable[[str, str, str], None],
    verbose: bool,
) -> tuple[bool, list[PhaseResult]]:
    """Run the ``implement`` phase under #8's retry-with-handoff budget.

    This is the per-phase runner the walker calls for the ``implement`` node, so
    the branching graph drives the flow while the implement step keeps its
    bounded retry. Up to ``1 + impl_retries`` attempts are made in place on the
    same worktree. An attempt fails when the agent crashes
    (:class:`AgentCrashError`) or when the run is clean but :paramref:`gate_check`
    reports the gates red. On a failed attempt with retries left, a handoff
    document is generated (resuming the failed Codex thread, or a fresh Claude
    call) and the next attempt carries that context. ``extra`` is any
    prior-context the walker threaded into the implement prompt for the first
    attempt (none, today). Returns ``(passed, phase_results)`` — the walker maps
    ``passed`` onto the implement node's ``on_success`` / ``on_failure`` edge.
    """
    phase_results: list[PhaseResult] = []
    retry_context = extra or ""

    for attempt in range(impl_retries + 1):
        if attempt > 0:
            _append_log(
                fixture_dir,
                run_id,
                f"Retry {attempt}/{impl_retries} for #{issue.number}",
            )
        prompt = executor.render_prompt(implement, retry_context or None)
        try:
            result = runtime.run(prompt, cwd=issue_worktree, phase=implement.name)
        except AgentCrashError as crash:
            phase_results = []
            _append_log(
                fixture_dir,
                run_id,
                f"Attempt {attempt + 1} for #{issue.number} crashed "
                f"during {crash.phase} (exit {crash.exit_code}).",
            )
            if attempt < impl_retries:
                retry_context = _retry_context(
                    attempt + 1,
                    f"agent crashed during {crash.phase} (exit {crash.exit_code})",
                    handoff_made=False,
                )
                continue
            return False, phase_results

        phase_results = [PhaseResult(phase=implement.name, result=result)]
        outcome = gate_check(issue_worktree)
        # The handoff/next-attempt context gets the gate's real captured output
        # rather than a static string, so the agent sees the actual failing checks.
        gate_output = outcome.output or "quality gates reported a failure"
        if outcome.passed:
            # Surface a passing gate only under --verbose: persist its output and
            # note the pass in the run log.
            if verbose:
                _surface_gate(gate_sink, outcome)
                _append_log(fixture_dir, run_id, f"Gate passed for #{issue.number}.")
            return True, phase_results

        # A failing gate is surfaced ALWAYS (regardless of verbose): persist its
        # output into the transcript and warn-log it so a red run is auditable.
        _surface_gate(gate_sink, outcome)
        logger.warning("Gate failed for #%s:\n%s", issue.number, outcome.output)
        _append_log(
            fixture_dir,
            run_id,
            f"Gates red after attempt {attempt + 1} for #{issue.number}.",
        )
        if attempt >= impl_retries:
            return False, phase_results

        thread_id = _last_thread_id(phase_results)
        handoff_made = generate_handoff(
            runtime,
            worktree=issue_worktree,
            thread_id=thread_id,
            gate_output=gate_output,
        )
        _append_log(
            fixture_dir,
            run_id,
            f"Handoff {'generated' if handoff_made else 'skipped'} for "
            f"#{issue.number} (thread {thread_id or 'n/a'}).",
        )
        retry_context = _retry_context(
            attempt + 1, gate_output, handoff_made=handoff_made
        )

    return False, phase_results


#: The phase name that runs through #8's retry-with-handoff budget. Every other
#: phase runs once; a crash takes that phase's failure edge.
IMPLEMENT_PHASE = "implement"


def _walk_issue(
    issue: IssueRef,
    *,
    runtime: Runtime,
    fixture_dir: Path,
    run_id: str,
    issue_worktree: Path,
    impl_retries: int,
    gate_check: GateCheck,
    gate_sink: Callable[[str, str, str], None],
    verbose: bool,
    graph: PhaseGraph,
) -> WalkResult:
    """Walk the issue's phase graph, with the implement node under #8's retry.

    The graph is loaded from the Project fixture and walked from its ``start``
    (see :class:`~pycastle.graph.GraphExecutor`). The ``implement`` node is run
    through :func:`_run_implement_phase` (the retry-with-handoff budget plus the
    project gate), so "succeeded within budget" follows its ``on_success`` edge
    and "exhausted/failed" follows its ``on_failure`` edge. Every other phase
    runs once; an agent crash takes that phase's failure edge. The walk stops at
    a terminal — :data:`~pycastle.graph.DONE` (proceed to commit + merge) or
    :data:`~pycastle.graph.HUMAN` (hand the issue to a person).
    """
    executor = GraphExecutor(
        runtime, fixture_dir=fixture_dir, preamble=render_issue_context(issue)
    )
    default_runner = executor._default_runner(issue_worktree)

    def run_phase(phase: Phase, extra: str | None) -> tuple[bool, list[PhaseResult]]:
        if phase.name == IMPLEMENT_PHASE:
            return _run_implement_phase(
                issue,
                phase,
                extra,
                executor=executor,
                runtime=runtime,
                fixture_dir=fixture_dir,
                run_id=run_id,
                issue_worktree=issue_worktree,
                impl_retries=impl_retries,
                gate_check=gate_check,
                gate_sink=gate_sink,
                verbose=verbose,
            )
        return default_runner(phase, extra)

    return executor.execute(graph, cwd=issue_worktree, phase_runner=run_phase)


def _walk_explicit_item_graph(
    issue: IssueRef,
    *,
    runtime: Runtime,
    fixture_dir: Path,
    issue_worktree: Path,
    graph: PhaseGraph,
    execution: ExplicitItemExecution,
) -> WalkResult:
    """Walk a mixed Item Execution graph with Setup before every node."""
    executor = GraphExecutor(
        runtime, fixture_dir=fixture_dir, preamble=render_issue_context(issue)
    )
    results: list[PhaseResult] = []
    identity = f"item-{issue.number}"

    def visit(entry: Any) -> NodeOutcome:
        node = entry.node
        execution.invoke_setup(
            issue_worktree,
            scope="item",
            identity=f"{identity}-{node.name}",
            ordinal=entry.ordinal,
        )
        if isinstance(node, GateNode):
            return execution.invoke_gate(
                issue_worktree,
                identity=identity,
                node=node.name,
                ordinal=entry.ordinal,
            )
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
                cwd=issue_worktree,
                phase=node.name,
            )
        except AgentCrashError:
            return NodeOutcome(False)
        results.append(PhaseResult(node.name, result))
        return NodeOutcome(True)

    walked = walk_execution_graph(graph, visit)
    return WalkResult(results, walked.terminal)


def _last_thread_id(phase_results: list[PhaseResult]) -> str | None:
    """Return the thread id of the last phase that exposed one, if any.

    Codex records its resumable thread on each phase's telemetry; the handoff
    resumes the implement attempt's thread. Claude records ``None`` here.
    """
    for phase_result in reversed(phase_results):
        thread_id = phase_result.result.telemetry.thread_id
        if thread_id:
            return thread_id
    return None


def _work_issue(
    issue: IssueRef,
    *,
    runtime: Runtime,
    issue_source: IssueSource,
    run: RunContext,
    worktree_root: Path,
    assignee: str,
    workspace: Path,
    impl_retries: int,
    gate_check: GateCheck,
    setup: Setup,
    item_graph: PhaseGraph,
    explicit_execution: ExplicitItemExecution | None = None,
    cancellation: CancellationState | None = None,
    verbose: bool = False,
) -> IssueOutcome:
    """Work one issue in its own worktree and merge it into the run branch.

    The issue is claimed, branched off the run branch into its own worktree, and
    driven by *walking* its phase graph (#10): from ``start`` each phase runs and
    its success/failure outcome follows the phase's ``on_success`` /
    ``on_failure`` edge until a terminal (see :func:`_walk_issue`). The
    ``implement`` node keeps its bounded retry — a failed attempt (a crash, or
    clean-but-gates-red) is retried in place with a handoff document and
    prior-attempt context (see :func:`_run_implement_phase`). A walk that reaches
    :data:`~pycastle.graph.DONE` is committed and, on a clean merge, folded into
    the run; the issue worktree and branch are then removed.

    A walk that reaches :data:`~pycastle.graph.HUMAN` (an implement node that
    exhausted its retries, a crash on a non-retried phase, or a runaway cycle
    hitting the visit cap) labels the issue ``ready-for-human`` and skips it
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
    issue_source.claim(issue.number, assignee=assignee)
    _append_log(fixture_dir, run_id, f"Working #{issue.number} on {branch}")

    # The gate sink is built unconditionally — a failing gate surfaces its output
    # regardless of --verbose (#28), so unlike the runtime sink below (verbose-only
    # live transcript persistence) the gate needs its persistence target always.
    gate_sink = _transcript_sink(fixture_dir, run_id, issue.number)

    if verbose:
        # The runtime is shared across issues (built once so a Docker image builds
        # once), so its transcript sink is rebound per issue to point at this
        # issue's transcript log. The runtime keeps surfacing [THINKING:<phase>]
        # and [OUTPUT:<phase>] lines live regardless; the sink is only the run-dir
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

    if explicit_execution is not None:
        walk = _walk_explicit_item_graph(
            issue,
            runtime=runtime,
            fixture_dir=fixture_dir,
            issue_worktree=issue_worktree,
            graph=item_graph,
            execution=explicit_execution,
        )
    else:
        setup(issue_worktree)
        walk = _walk_issue(
            issue,
            runtime=runtime,
            fixture_dir=fixture_dir,
            run_id=run_id,
            issue_worktree=issue_worktree,
            impl_retries=impl_retries,
            gate_check=gate_check,
            gate_sink=gate_sink,
            verbose=verbose,
            graph=item_graph,
        )
    if walk.results:
        _write_telemetry(fixture_dir, run_id, issue, walk.results)

    if walk.terminal is HUMAN:
        # The walk routed to a human (retries exhausted, a non-retried phase
        # crashed, or a runaway cycle hit the visit cap): hand the issue over and
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
        # The walk reached DONE but the phase produced no change (e.g. a runtime
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
    An empty diff means the phase silently no-opped: merging it would be a clean
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
    run_id: str, selected: Sequence[IssueRef], outcomes: Sequence[IssueOutcome]
) -> str:
    """Render the bounded factual envelope supplied to each Run phase."""
    outcome_by_number = {o.issue.number: o for o in outcomes}
    rows = []
    for issue in selected:
        outcome = outcome_by_number.get(issue.number)
        state = (
            "pending"
            if outcome is None
            else ("completed" if outcome.merged else "skipped")
        )
        rows.append(f"- #{issue.number}: {issue.title} [{state}]")
    return f"# PyCastle Run {run_id}\n\n## Frozen Items\n\n" + "\n".join(rows)


def _checkpoint_run_phase(
    phase: Phase,
    *,
    run: RunContext,
    scope: str = "Run",
) -> None:
    """Commit a successful Run phase when dirty and attempt a durability push."""
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
                run, scope=scope, phase=phase.name, argv=add_argv, result=staged
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
                run, scope=scope, phase=phase.name, argv=diff_argv, result=dirty
            )
            raise RunCheckpointError(detail)
        if dirty_code == 1:
            commit_argv = [
                "git",
                "commit",
                "-m",
                f"chore: checkpoint Run phase {phase.name}",
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
                    phase=phase.name,
                    argv=commit_argv,
                    result=committed,
                )
                raise RunCheckpointError(detail)
    except OSError as exc:
        detail = _record_host_command_exception(
            run, scope=scope, phase=phase.name, argv=argv, exc=exc
        )
        raise RunCheckpointError(detail) from exc

    _push_run_branch(run=run, final=False, scope=scope, phase=phase.name)


def _walk_run_graph(
    graph: PhaseGraph,
    *,
    runtime: Runtime,
    run: RunContext,
    context: str,
    scope: str = "Run",
) -> WalkResult:
    """Walk one Run phase graph, checkpointing each successful visit."""
    executor = GraphExecutor(runtime, fixture_dir=run.fixture_dir, preamble=context)
    default = executor._default_runner(run.worktree)

    def run_phase(phase: Phase, extra: str | None) -> tuple[bool, list[PhaseResult]]:
        passed, results = default(phase, extra)
        if results:
            _append_run_telemetry(run.fixture_dir, run.run_id, scope, results)
        if passed:
            _checkpoint_run_phase(phase, run=run, scope=scope)
        else:
            _discard_run_commands(run, scope=scope, phase=phase.name)
        return passed, results

    return executor.execute(graph, cwd=run.worktree, phase_runner=run_phase)


def _discard_incomplete_run_scope(run: RunContext) -> None:
    """Restore the Run worktree to its last committed durable checkpoint."""
    _discard_run_commands(run, scope="after-Run", phase="Setup")


def _discard_run_commands(run: RunContext, *, scope: str, phase: str) -> None:
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
                run, scope=scope, phase=phase, argv=argv, result=result
            )
            raise RunCheckpointError(
                f"could not discard incomplete Run scope\n{detail}"
            )


def _record_host_command_failure(
    run: RunContext,
    *,
    scope: str,
    phase: str,
    argv: Sequence[str],
    result: Any,
) -> str:
    """Surface and retain all captured diagnostics from a boundary command."""
    return _record_host_command_diagnostics(
        run,
        scope=scope,
        phase=phase,
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
    phase: str,
    argv: Sequence[str],
    exc: OSError,
) -> str:
    """Surface command identity when a boundary command cannot be launched."""
    return _record_host_command_diagnostics(
        run,
        scope=scope,
        phase=phase,
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
    phase: str,
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
        sink(phase, "HOST-COMMAND", line)
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
    selected: Sequence[IssueRef],
    fixture_dir: Path,
    repo: str,
    base_branch: str,
    assignee: str,
    run_id: str,
    iterations: int = 1,
    impl_retries: int = 2,
    gate_check: GateCheck | None = None,
    setup: Setup | None = None,
    workspace: Path | None = None,
    worktree_root: Path | None = None,
    include_unassigned: bool = False,
    runner: Runner = run_cmd,
    verbose: bool = False,
) -> RunOutcome:
    """Work up to ``iterations`` ready issues into one integrated pull request.

    ``selected`` is the ordered batch frozen by readiness. A per-run branch is cut in its own
    worktree so the main checkout stays put; each selected issue is then worked
    in its own worktree off the run branch and, on a clean merge, folded into the
    run branch. One pull request is opened for the run, closing every issue that
    merged. ``run_id`` is injected (not read from a clock) to keep runs
    deterministic for tests.

    A failed implement attempt is retried up to ``impl_retries`` times
    (``1 + impl_retries`` attempts total) with a handoff document and
    prior-attempt context; ``gate_check`` decides whether an attempt's quality
    gates passed (default: every attempt passes, so no retry fires). An issue
    that exhausts its retries is labelled ``ready-for-human`` and the run
    continues to the next issue.

    ``setup`` prepares each newly-created issue worktree before its phase graph
    is walked. It defaults to a no-op for callers and older Project fixtures.

    ``verbose`` (#48, #52) turns on transcript persistence: before each issue is
    worked the runtime's transcript sink is bound to that issue's transcript log
    under ``.pycastle/runs/<run_id>/`` (live ``[THINKING:<phase>]`` and
    ``[OUTPUT:<phase>]`` surfacing is already on in the runtime itself). Off by
    default, so a normal run writes no transcript log and behaves exactly as
    before.
    """
    # Copy again at the orchestration boundary so callers cannot mutate the
    # active membership, order, or Item content during project execution.
    selected = tuple(issue.model_copy(deep=True) for issue in selected)
    run_branch = f"pycastle/run-{run_id}"
    outcome = RunOutcome(
        run_id=run_id,
        run_branch=run_branch,
        selected=[issue.number for issue in selected],
    )
    if not selected:
        return outcome

    gate_check = gate_check or _gates_always_pass
    setup = setup or (lambda _worktree: None)
    workspace = workspace or Path.cwd()
    worktree_root = worktree_root or (fixture_dir / "worktrees")
    worktree_root.mkdir(parents=True, exist_ok=True)

    # Import once: Runtime edits to the fixture are proposed changes and cannot
    # rewrite the active Run definition or weaken its graphs.
    run_definition: RunDefinition = load_run(fixture_dir)
    explicit_execution = (
        ExplicitItemExecution.freeze(fixture_dir, run_id)
        if any(
            isinstance(node, GateNode) for node in run_definition.item.nodes.values()
        )
        else None
    )

    # Per-run branch + worktree: the main checkout is left on its branch.
    run_worktree = worktree_root / f"run-{run_id}"
    create_branch(run_branch, base_branch, runner=runner, cwd=workspace)
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
        f"Run {run_id}: {len(selected)} issue(s) on {run_branch} (base {base_branch})",
    )

    cancellation = CancellationState()
    try:
        with _sigint_as_keyboard_interrupt():
            if explicit_execution is not None:
                explicit_execution.invoke_setup(
                    run_worktree,
                    scope="run",
                    identity="run-bootstrap",
                    ordinal=1,
                )
            else:
                setup(run_worktree)
            if run_definition.before is not None:
                if verbose:
                    runtime.transcript_sink = _run_transcript_sink(  # type: ignore[attr-defined]
                        fixture_dir, run_id, "before-Run"
                    )
                before = _walk_run_graph(
                    run_definition.before,
                    runtime=runtime,
                    run=run,
                    context=render_run_context(run_id, selected, []),
                    scope="before-Run",
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
        outcome.stopping_point = f"before-Run Setup: {exc}"
        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
        return outcome
    except RunCheckpointError as exc:
        outcome.succeeded = False
        outcome.stopping_point = f"before-Run checkpoint: {exc}"
        _append_log(fixture_dir, run_id, outcome.stopping_point)
        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
        return outcome
    if before is not None and before.terminal is HUMAN:
        outcome.succeeded = False
        outcome.stopping_point = "before-Run HUMAN"
        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
        return outcome

    # Track the issue currently in flight so an interrupt (SIGINT) or any
    # exception mid-issue can clean up that issue's worktree and restore its
    # ready state. SIGINT is turned into a KeyboardInterrupt so it unwinds here.
    with _sigint_as_keyboard_interrupt():
        try:
            for issue in selected:
                try:
                    item_outcome = _work_issue(
                        issue,
                        runtime=runtime,
                        issue_source=issue_source,
                        run=run,
                        worktree_root=worktree_root,
                        assignee=assignee,
                        workspace=workspace,
                        impl_retries=impl_retries,
                        gate_check=gate_check,
                        setup=setup,
                        item_graph=run_definition.item,
                        explicit_execution=explicit_execution,
                        cancellation=cancellation,
                        verbose=verbose,
                    )
                except Exception as exc:  # handled infrastructure boundary
                    cleanup_worktree(
                        worktree_root / f"issue-{issue.number}",
                        runner=runner,
                        cwd=workspace,
                    )
                    issue_source.release(issue.number)
                    cancellation.in_flight = None
                    if not outcome.completed:
                        cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
                        raise
                    outcome.succeeded = False
                    outcome.stopping_point = (
                        f"Item #{issue.number} infrastructure failure: {exc}"
                    )
                    _append_log(fixture_dir, run_id, outcome.stopping_point)
                    break
                outcome.issues.append(item_outcome)
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
        suppress_report_harvest = False
        try:
            with _sigint_as_keyboard_interrupt():
                if outcome.succeeded:
                    if explicit_execution is None or run_definition.after is not None:
                        setup(run_worktree)
                    if run_definition.after is not None:
                        if verbose:
                            runtime.transcript_sink = _run_transcript_sink(  # type: ignore[attr-defined]
                                fixture_dir, run_id, "after-Run"
                            )
                        after = _walk_run_graph(
                            run_definition.after,
                            runtime=runtime,
                            run=run,
                            context=render_run_context(
                                run_id, selected, outcome.issues
                            ),
                            scope="after-Run",
                        )
                        if after.terminal is HUMAN:
                            outcome.succeeded = False
                            outcome.stopping_point = "after-Run HUMAN"
                    if explicit_execution is None:
                        run_gate = gate_check(run_worktree)
                        (
                            _telemetry_dir(fixture_dir, run_id) / "run-gate.log"
                        ).write_text(run_gate.output)
                        if not run_gate.passed:
                            outcome.succeeded = False
                            outcome.stopping_point = "Run Gate"
        except SetupError as exc:
            suppress_report_harvest = True
            if outcome.succeeded:
                outcome.succeeded = False
                outcome.stopping_point = "after-Run Setup"
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
            selected=selected,
            skipped=outcome.skipped,
            gate=run_gate,
            report=report,
            publication_error=publication_error,
            successful=outcome.succeeded,
            stopping_point=outcome.stopping_point,
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
    return outcome


def _open_pull_request(
    *,
    repo: str,
    base_branch: str,
    run: RunContext,
    completed: list[int],
    selected: Sequence[IssueRef],
    skipped: list[int],
    gate: GateOutcome | None,
    report: str | None,
    publication_error: str | None,
    successful: bool,
    stopping_point: str | None,
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
        gate_line = (
            f"`{gate.command}` — {'PASS' if gate.passed else 'FAIL'} "
            f"(exit {gate.exit_code}, {gate.duration_seconds:.2f}s)"
        )
    selected_numbers = [issue.number for issue in selected]
    marker = f"<!-- pycastle-run-report:{run.run_id} -->"
    comment = (
        f"{marker}\n## PyCastle Run {run.run_id}\n\n"
        f"- State: **{state}**\n"
        f"- Selected Items: {', '.join(f'#{n}' for n in selected_numbers) or 'none'}\n"
        f"- Completed Items: {', '.join(f'#{n}' for n in completed) or 'none'}\n"
        f"- Skipped Items: {', '.join(f'#{n}' for n in skipped) or 'none'}\n"
        f"- Run Gate: {gate_line}\n"
    )
    if stopping_point:
        comment += f"- Stopping point: {stopping_point}\n"
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
    phase: str | None = None,
) -> bool:
    """Push the current Run checkpoint, logging failures without raising."""
    argv = ["git", "push", "-u", "origin", run.branch]
    phase_name = phase or ("final-push" if final else "durability-push")
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
                phase=phase_name,
                argv=argv,
                result=result,
            )
    except OSError as exc:
        succeeded = False
        _record_host_command_exception(
            run,
            scope=scope,
            phase=phase_name,
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
