"""The run lifecycle: turn a batch of ready issues into one pull request.

A run selects up to N ready, appropriately-assigned issues and works them as a
bounded batch. It cuts a per-run branch in its own worktree (the main checkout
stays untouched), then works each issue in its own worktree off that run branch:
run the graph, commit, and on a clean merge fold the issue branch back into the
run branch. Successful issues are accumulated and a single pull request is opened
for the whole run. Per-phase provider telemetry and a run log are written into
the Project fixture under ``.pycastle/runs/<run_id>/`` (an ignored path, so run
output is never committed).

Retries and handoff (#7) and the merge-conflict / interrupt restore paths (#8)
are out of scope here; this slice leaves clear seams for them and otherwise
records and skips an issue whose merge does not apply cleanly.
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
from .runtime import Runtime

logger = logging.getLogger(__name__)

Runner = Callable[..., Any]


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
    issue #8 and is not implemented here.
    """
    runner(
        ["git", "worktree", "remove", str(worktree), "--force"],
        capture=True,
        cwd=cwd,
    )
    runner(["git", "worktree", "prune"], capture=True, cwd=cwd)


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
) -> IssueOutcome:
    """Work one issue in its own worktree and merge it into the run branch.

    The issue is claimed, branched off the run branch into its own worktree, run
    through the graph, and committed. A clean merge of the issue branch into the
    run worktree folds it into the run; the issue worktree and branch are then
    removed. On a merge that does not apply cleanly the issue is recorded as not
    merged and skipped — the conflict -> ready-for-human handling is issue #8, so
    this is the seam for it (we abort the merge and leave the label untouched).
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

    graph = load_graph(fixture_dir)
    phase_results = GraphExecutor(runtime, fixture_dir=fixture_dir).execute(
        graph, cwd=issue_worktree
    )
    _write_telemetry(fixture_dir, run_id, issue, phase_results)

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
    issue. Turning that skip into a ready-for-human handoff is issue #8; this is
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

    # Seam for #8: a conflicting merge is aborted and the issue skipped here;
    # the ready-for-human label + restore is added in that slice.
    _append_log(
        fixture_dir,
        run_id,
        f"Merge of #{issue.number} did not apply cleanly; skipping (see #8).",
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
    """
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
