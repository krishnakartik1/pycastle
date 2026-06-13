"""The run lifecycle: turn a ready issue into one pull request.

v0.1 is the walking skeleton. It works a single issue end to end — select,
claim, branch, run the graph, commit the change, open a PR — through whatever
Runtime it is handed (the stub today). Batch runs, retries, review, merge, and
cleanup arrive in later slices.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commands import run_cmd
from .graph import GraphExecutor, load_graph
from .issues import IssueSource, select_next
from .models import IssueRef
from .runtime import Runtime

logger = logging.getLogger(__name__)

Runner = Callable[..., Any]


@dataclass
class RunOutcome:
    """What a single-issue run produced."""

    issue: IssueRef | None
    branch: str | None
    pr_opened: bool


def slugify(title: str, *, max_words: int = 6) -> str:
    """Turn an issue title into a short, branch-safe slug."""
    words = re.sub(r"[^a-z0-9\s-]", "", title.lower()).split()
    return "-".join(words[:max_words]) or "issue"


def run(
    *,
    runtime: Runtime,
    issue_source: IssueSource,
    fixture_dir: Path,
    repo: str,
    base_branch: str,
    assignee: str,
    workspace: Path | None = None,
    include_unassigned: bool = False,
    runner: Runner = run_cmd,
) -> RunOutcome:
    """Work a single ready issue and open one pull request for it."""
    workspace = workspace or Path.cwd()

    issues = issue_source.list_ready()
    issue = select_next(
        issues, assignee=assignee, include_unassigned=include_unassigned
    )
    if issue is None:
        logger.info("No ready issues to work.")
        return RunOutcome(issue=None, branch=None, pr_opened=False)

    issue_source.claim(issue.number, assignee=assignee)
    branch = f"pycastle/issue-{issue.number}-{slugify(issue.title)}"
    logger.info("Working #%s on %s", issue.number, branch)
    runner(["git", "checkout", "-b", branch], capture=True, cwd=workspace)

    graph = load_graph(fixture_dir)
    GraphExecutor(runtime, fixture_dir=fixture_dir).execute(graph, cwd=workspace)

    runner(["git", "add", "-A"], capture=True, cwd=workspace)
    runner(
        ["git", "commit", "-m", f"feat: address #{issue.number} ({runtime.name})"],
        capture=True,
        cwd=workspace,
    )
    runner(["git", "push", "-u", "origin", branch], capture=True, cwd=workspace)

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
            branch,
            "--title",
            f"pycastle: #{issue.number} {issue.title}",
            "--body",
            f"Closes #{issue.number}\n\nAutomated PyCastle walking-skeleton run.",
        ],
        capture=True,
    )
    pr_opened = getattr(pr, "returncode", 1) == 0
    return RunOutcome(issue=issue, branch=branch, pr_opened=pr_opened)
