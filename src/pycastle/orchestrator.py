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

The merge-conflict / interrupt restore paths (#9) are out of scope here; this
slice leaves clear seams for them and otherwise records and skips an issue whose
merge does not apply cleanly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .commands import run_cmd
from .graph import GraphExecutor, PhaseResult, load_graph
from .issues import IssueSource, select_batch
from .models import IssueRef
from .runtime import AgentCrashError, CodexRuntime, Runtime

logger = logging.getLogger(__name__)

Runner = Callable[..., Any]

#: A gate check decides whether an implement attempt's quality gates passed.
#: It takes the issue worktree and returns ``True`` when the gates are green. It
#: is injectable so a "gates red" outcome can drive a retry without hardcoding a
#: specific project gate command here; the default treats every attempt as
#: passing (the real gate is the project's own and is wired by the caller).
GateCheck = Callable[[Path], bool]

#: Where a handoff document is written inside the issue worktree (ignored path).
HANDOFF_DOC = ".pycastle/handoff.md"

#: The phase name used for the handoff invocation's telemetry.
HANDOFF_PHASE = "handoff"


def _gates_always_pass(_worktree: Path) -> bool:
    """Default gate check: treat every attempt as passing.

    The real gate is the project's own quality-gate command; it is injected by
    the caller. With no gate wired, a single implement attempt is made and no
    retry/handoff is triggered.
    """
    return True


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
    issues: list[IssueOutcome] = field(default_factory=list)
    pr_opened: bool = False

    @property
    def completed(self) -> list[int]:
        """Issue numbers that merged cleanly into the run branch."""
        return [o.issue.number for o in self.issues if o.merged]


def slugify(title: str, *, max_words: int = 6) -> str:
    """Turn an issue title into a short, branch-safe slug."""
    words = re.sub(r"[^a-z0-9\s-]", "", title.lower()).split()
    return "-".join(words[:max_words]) or "issue"


def issue_branch_name(issue: IssueRef) -> str:
    """Return the per-issue branch name PyCastle works an issue on."""
    return f"pycastle/issue-{issue.number}-{slugify(issue.title)}"


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


def _append_log(fixture_dir: Path, run_id: str, message: str) -> None:
    """Append one line to the run log and emit it through ``logging``."""
    logger.info(message)
    run_dir = _telemetry_dir(fixture_dir, run_id)
    with (run_dir / "run.log").open("a") as handle:
        handle.write(message + "\n")


def cleanup_worktree(worktree: Path, *, runner: Runner, cwd: Path) -> None:
    """Remove a worktree and prune the registry.

    Provided so a run leaves no worktrees behind; the interrupt-driven
    cleanup-and-restore path (restoring the ready label, aborting mid-issue) is
    issue #9 and is not implemented here.
    """
    runner(
        ["git", "worktree", "remove", str(worktree), "--force"],
        capture=True,
        cwd=cwd,
    )
    runner(["git", "worktree", "prune"], capture=True, cwd=cwd)


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


def _run_implement_attempts(
    issue: IssueRef,
    *,
    runtime: Runtime,
    fixture_dir: Path,
    run_id: str,
    issue_worktree: Path,
    impl_retries: int,
    gate_check: GateCheck,
    runner: Runner,
) -> tuple[bool, list[PhaseResult]]:
    """Run the graph for one issue, retrying with handoff while gates stay red.

    Up to ``1 + impl_retries`` attempts are made in place on the same worktree.
    An attempt fails when the agent crashes (:class:`AgentCrashError`) or when
    the run is clean but :paramref:`gate_check` reports the gates red. On a
    failed attempt with retries left, a handoff document is generated (resuming
    the failed Codex thread, or a fresh Claude call) and the next attempt carries
    that context. Returns ``(passed, phase_results)`` where ``phase_results`` is
    the last attempt's results (empty if every attempt crashed).
    """
    graph = load_graph(fixture_dir)
    executor = GraphExecutor(runtime, fixture_dir=fixture_dir)
    phase_results: list[PhaseResult] = []
    retry_context = ""

    for attempt in range(impl_retries + 1):
        if attempt > 0:
            _append_log(
                fixture_dir,
                run_id,
                f"Retry {attempt}/{impl_retries} for #{issue.number}",
            )
        try:
            phase_results = executor.execute(
                graph,
                cwd=issue_worktree,
                phase_context={"implement": retry_context} if retry_context else None,
            )
        except AgentCrashError as crash:
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

        gate_output = "quality gates reported a failure"
        if gate_check(issue_worktree):
            return True, phase_results

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
    fixture_dir: Path,
    run_id: str,
    run_branch: str,
    run_worktree: Path,
    worktree_root: Path,
    assignee: str,
    workspace: Path,
    runner: Runner,
    impl_retries: int,
    gate_check: GateCheck,
) -> IssueOutcome:
    """Work one issue in its own worktree and merge it into the run branch.

    The issue is claimed, branched off the run branch into its own worktree, and
    run through the graph with a bounded implement retry: a failed attempt (a
    crash, or clean-but-gates-red) is retried in place with a handoff document
    and prior-attempt context (see :func:`_run_implement_attempts`). When the
    gates finally pass the work is committed and a clean merge folds it into the
    run; the issue worktree and branch are then removed.

    An issue that exhausts its retries is labelled ``ready-for-human`` and
    skipped (recorded as not merged) so the run continues to the next issue — one
    stuck item does not sink the batch. On a merge that does not apply cleanly
    the issue is likewise recorded as not merged and skipped; turning *that* skip
    into a ready-for-human handoff is issue #9, so this leaves that seam (we abort
    the merge and leave the label untouched there).
    """
    branch = issue_branch_name(issue)
    issue_source.claim(issue.number, assignee=assignee)
    _append_log(fixture_dir, run_id, f"Working #{issue.number} on {branch}")

    issue_worktree = worktree_root / f"issue-{issue.number}"
    runner(["git", "branch", branch, run_branch], capture=True, cwd=workspace)
    runner(
        ["git", "worktree", "add", str(issue_worktree), branch],
        capture=True,
        cwd=workspace,
    )

    passed, phase_results = _run_implement_attempts(
        issue,
        runtime=runtime,
        fixture_dir=fixture_dir,
        run_id=run_id,
        issue_worktree=issue_worktree,
        impl_retries=impl_retries,
        gate_check=gate_check,
        runner=runner,
    )
    if phase_results:
        _write_telemetry(fixture_dir, run_id, issue, phase_results)

    if not passed:
        # Retries exhausted: hand the issue to a human and move on. Cleaning up
        # the worktree and branch here keeps the batch tidy for the next issue.
        issue_source.mark_for_human(issue.number)
        _append_log(
            fixture_dir,
            run_id,
            f"#{issue.number} exhausted its retries; marked ready-for-human.",
        )
        cleanup_worktree(issue_worktree, runner=runner, cwd=workspace)
        runner(["git", "branch", "-D", branch], capture=True, cwd=workspace)
        return IssueOutcome(issue=issue, branch=branch, merged=False)

    runner(["git", "add", "-A"], capture=True, cwd=issue_worktree)
    runner(
        ["git", "commit", "-m", f"feat: address #{issue.number} ({runtime.name})"],
        capture=True,
        cwd=issue_worktree,
    )

    merged = _merge_issue_branch(
        branch,
        issue=issue,
        fixture_dir=fixture_dir,
        run_id=run_id,
        run_worktree=run_worktree,
        runner=runner,
    )

    cleanup_worktree(issue_worktree, runner=runner, cwd=workspace)
    runner(["git", "branch", "-D", branch], capture=True, cwd=workspace)
    return IssueOutcome(issue=issue, branch=branch, merged=merged)


def _merge_issue_branch(
    branch: str,
    *,
    issue: IssueRef,
    fixture_dir: Path,
    run_id: str,
    run_worktree: Path,
    runner: Runner,
) -> bool:
    """Merge an issue branch into the run worktree; return True on a clean merge.

    A clean merge is enough for this slice. On a merge that does not apply
    cleanly the merge is aborted and ``False`` returned so the caller skips the
    issue. Turning that skip into a ready-for-human handoff is issue #9; this is
    the seam — do not add the label here.
    """
    merge = runner(
        ["git", "merge", branch, "--no-edit"],
        capture=True,
        cwd=run_worktree,
    )
    if getattr(merge, "returncode", 1) == 0:
        _append_log(fixture_dir, run_id, f"Merged #{issue.number} into {run_id}")
        return True

    # Seam for #9: a conflicting merge is aborted and the issue skipped here;
    # the ready-for-human label + restore is added in that slice.
    _append_log(
        fixture_dir,
        run_id,
        f"Merge of #{issue.number} did not apply cleanly; skipping (see #9).",
    )
    runner(["git", "merge", "--abort"], capture=True, cwd=run_worktree)
    return False


def run_batch(
    *,
    runtime: Runtime,
    issue_source: IssueSource,
    fixture_dir: Path,
    repo: str,
    base_branch: str,
    assignee: str,
    run_id: str,
    iterations: int = 1,
    impl_retries: int = 2,
    gate_check: GateCheck | None = None,
    workspace: Path | None = None,
    worktree_root: Path | None = None,
    include_unassigned: bool = False,
    runner: Runner = run_cmd,
) -> RunOutcome:
    """Work up to ``iterations`` ready issues into one integrated pull request.

    Selection is a pure function behind the Issue source boundary
    (:func:`~pycastle.issues.select_batch`). A per-run branch is cut in its own
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
    """
    gate_check = gate_check or _gates_always_pass
    workspace = workspace or Path.cwd()
    worktree_root = worktree_root or (fixture_dir / "worktrees")
    worktree_root.mkdir(parents=True, exist_ok=True)

    issues = issue_source.list_ready()
    selected = select_batch(
        issues,
        assignee=assignee,
        include_unassigned=include_unassigned,
        limit=iterations,
    )
    run_branch = f"pycastle/run-{run_id}"
    outcome = RunOutcome(run_id=run_id, run_branch=run_branch)
    if not selected:
        _append_log(fixture_dir, run_id, "No ready issues to work.")
        return outcome

    # Per-run branch + worktree: the main checkout is left on its branch.
    run_worktree = worktree_root / f"run-{run_id}"
    runner(["git", "branch", run_branch, base_branch], capture=True, cwd=workspace)
    runner(
        ["git", "worktree", "add", str(run_worktree), run_branch],
        capture=True,
        cwd=workspace,
    )
    _append_log(
        fixture_dir,
        run_id,
        f"Run {run_id}: {len(selected)} issue(s) on {run_branch} (base {base_branch})",
    )

    for issue in selected:
        outcome.issues.append(
            _work_issue(
                issue,
                runtime=runtime,
                issue_source=issue_source,
                fixture_dir=fixture_dir,
                run_id=run_id,
                run_branch=run_branch,
                run_worktree=run_worktree,
                worktree_root=worktree_root,
                assignee=assignee,
                workspace=workspace,
                runner=runner,
                impl_retries=impl_retries,
                gate_check=gate_check,
            )
        )

    completed = outcome.completed
    if completed:
        outcome.pr_opened = _open_pull_request(
            repo=repo,
            base_branch=base_branch,
            run_branch=run_branch,
            run_id=run_id,
            completed=completed,
            run_worktree=run_worktree,
            fixture_dir=fixture_dir,
            runner=runner,
        )
    else:
        _append_log(fixture_dir, run_id, "No issues merged; opening no pull request.")

    cleanup_worktree(run_worktree, runner=runner, cwd=workspace)
    return outcome


def _open_pull_request(
    *,
    repo: str,
    base_branch: str,
    run_branch: str,
    run_id: str,
    completed: list[int],
    run_worktree: Path,
    fixture_dir: Path,
    runner: Runner,
) -> bool:
    """Push the run branch and open one pull request closing every merged issue.

    The body carries a ``- Closes #N`` line per completed issue so merging the
    single run PR closes the whole batch.
    """
    runner(
        ["git", "push", "-u", "origin", run_branch],
        capture=True,
        cwd=run_worktree,
    )
    closes = "\n".join(f"- Closes #{number}" for number in completed)
    body = (
        f"Automated PyCastle run {run_id} completing {len(completed)} issue(s).\n\n"
        f"{closes}\n"
    )
    pr = runner(
        [
            "gh",
            "pr",
            "create",
            "-R",
            repo,
            "--base",
            base_branch,
            "--head",
            run_branch,
            "--title",
            f"pycastle: run {run_id}",
            "--body",
            body,
        ],
        capture=True,
    )
    opened = getattr(pr, "returncode", 1) == 0
    _append_log(
        fixture_dir,
        run_id,
        f"Pull request {'opened' if opened else 'failed'} for {run_branch}",
    )
    return opened
