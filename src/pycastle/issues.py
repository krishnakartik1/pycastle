"""The Issue source boundary: where work items come from.

v0.1 ships GitHub Issues via the ``gh`` CLI. The selection and assignee-filter
logic is pure and lives here, behind the interface, so a different source can
be added later without touching the runner.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .commands import run_cmd
from .models import IssueComment, IssueRef

Runner = Callable[..., Any]


def assignee_logins(issue: dict[str, Any]) -> list[str]:
    """Return assignee logins from gh JSON, accepting raw or simplified shapes."""
    logins: list[str] = []
    for assignee in issue.get("assignees") or []:
        if isinstance(assignee, str):
            logins.append(assignee)
        elif isinstance(assignee, dict) and assignee.get("login"):
            logins.append(str(assignee["login"]))
    return logins


def comment_author(comment: dict[str, Any]) -> str:
    """Return a comment author's login, or a stable deleted-user fallback."""
    author = comment.get("author")
    if isinstance(author, str) and author:
        return author
    if isinstance(author, dict) and author.get("login"):
        return str(author["login"])
    return "unknown"


def filter_for_assignee(
    issues: list[IssueRef],
    assignee: str,
    *,
    include_unassigned: bool = False,
) -> list[IssueRef]:
    """Keep issues assigned to ``assignee``, optionally including unassigned ones."""
    kept: list[IssueRef] = []
    for issue in issues:
        if assignee in issue.assignees or (include_unassigned and not issue.assignees):
            kept.append(issue)
    return kept


def select_next(
    issues: list[IssueRef],
    *,
    assignee: str,
    include_unassigned: bool = False,
) -> IssueRef | None:
    """Return the lowest-numbered eligible issue, or ``None`` if there is none."""
    eligible = filter_for_assignee(
        issues, assignee, include_unassigned=include_unassigned
    )
    return min(eligible, key=lambda issue: issue.number, default=None)


def select_batch(
    issues: list[IssueRef],
    *,
    assignee: str,
    include_unassigned: bool = False,
    limit: int,
) -> list[IssueRef]:
    """Return up to ``limit`` eligible issues, lowest-numbered first.

    The batch generalises :func:`select_next`: it filters to the issues this
    assignee may work (optionally including unassigned ones) and returns them in
    ascending issue-number order, capped at ``limit``. A ``limit`` of zero or
    less yields an empty batch. This stays a pure function so the selection,
    assignee-filter, and ready-state logic can be tested without mocks.
    """
    eligible = filter_for_assignee(
        issues, assignee, include_unassigned=include_unassigned
    )
    ordered = sorted(eligible, key=lambda issue: issue.number)
    return ordered[:limit] if limit > 0 else []


class IssueSource(ABC):
    """Lists, claims, and labels work items behind a stable interface."""

    @abstractmethod
    def list_ready(self) -> list[IssueRef]:
        """Return the open work items ready for an agent."""

    @abstractmethod
    def claim(self, number: int, *, assignee: str) -> None:
        """Claim an issue so a second run does not collide on it."""

    @abstractmethod
    def mark_for_human(self, number: int) -> None:
        """Label an issue for a human after the agent could not finish it."""

    @abstractmethod
    def release(self, number: int) -> None:
        """Return a claimed issue to the ready pool after an interrupted run."""


class GitHubIssueSource(IssueSource):
    """An Issue source backed by GitHub Issues via the ``gh`` CLI."""

    def __init__(
        self,
        repo: str,
        *,
        label: str = "ready-for-agent",
        human_label: str = "ready-for-human",
        runner: Runner = run_cmd,
    ) -> None:
        self.repo = repo
        self.label = label
        self.human_label = human_label
        self._run = runner

    def list_ready(self) -> list[IssueRef]:
        """Return open issues carrying the ready label."""
        result = self._run(
            [
                "gh",
                "issue",
                "list",
                "-R",
                self.repo,
                "--state",
                "open",
                "--label",
                self.label,
                "--limit",
                "100",
                "--json",
                "number,title,body,labels,assignees,comments",
            ],
            capture=True,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        issues: list[IssueRef] = []
        for item in json.loads(raw):
            labels = [
                lbl["name"] if isinstance(lbl, dict) else lbl
                for lbl in item.get("labels", [])
            ]
            issues.append(
                IssueRef(
                    number=item["number"],
                    title=item.get("title", ""),
                    body=item.get("body", ""),
                    labels=labels,
                    assignees=assignee_logins(item),
                    comments=[
                        IssueComment(
                            author=comment_author(comment),
                            body=comment.get("body", ""),
                        )
                        for comment in sorted(
                            item.get("comments") or [],
                            key=lambda value: value.get("createdAt", ""),
                        )
                    ],
                )
            )
        return issues

    def claim(self, number: int, *, assignee: str) -> None:
        """Assign the issue and drop the ready label so other runs skip it."""
        self._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "-R",
                self.repo,
                "--add-assignee",
                assignee,
                "--remove-label",
                self.label,
            ],
            capture=True,
        )

    def mark_for_human(self, number: int) -> None:
        """Add the ready-for-human label so a person picks the issue up.

        Used when an issue exhausts its implement retries: the run leaves the
        label behind and moves on, so one stuck item does not sink the batch.
        """
        self._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "-R",
                self.repo,
                "--add-label",
                self.human_label,
            ],
            capture=True,
        )

    def release(self, number: int) -> None:
        """Re-add the ready label so a claimed issue returns to the agent pool.

        The mirror of :meth:`claim`'s label drop: when a run is interrupted
        (SIGINT) with an issue in flight, the run restores ``ready-for-agent``
        so the issue is not left stuck in a claimed state and the next run can
        pick it up again.
        """
        self._run(
            [
                "gh",
                "issue",
                "edit",
                str(number),
                "-R",
                self.repo,
                "--add-label",
                self.label,
            ],
            capture=True,
        )
