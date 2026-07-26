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
    author = comment.get("author", comment.get("user"))
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


def candidate_pool(
    issues: list[IssueRef],
    *,
    assignee: str,
    include_unassigned: bool = False,
) -> list[IssueRef]:
    """Return every eligible Item in canonical Item-number order.

    Run capacity does not participate in readiness: it caps claimed attempts,
    while the project-owned selection policy must see the complete mechanically
    eligible pool.
    """
    eligible = filter_for_assignee(
        issues, assignee, include_unassigned=include_unassigned
    )
    return sorted(eligible, key=lambda issue: issue.number)


class IssueSource(ABC):
    """Lists, claims, and labels work items behind a stable interface."""

    @abstractmethod
    def list_ready(self, *, timeout: float | None = None) -> list[IssueRef]:
        """Return the open work items ready for an agent."""

    @abstractmethod
    def is_still_eligible(
        self,
        frozen_item: IssueRef,
        *,
        assignee: str,
        include_unassigned: bool,
    ) -> bool:
        """Recheck one frozen Item's current ownership eligibility."""

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

    def _paginated_rows(
        self,
        endpoint: str,
        *,
        fields: tuple[str, ...] = (),
        timeout: float | None,
        failure: str,
    ) -> list[dict[str, Any]]:
        """Return every object from a paginated GitHub REST collection."""
        options: dict[str, Any] = {"capture": True}
        if timeout is not None:
            options["timeout"] = timeout
        argv = [
            "gh",
            "api",
            "--method",
            "GET",
            "--paginate",
            "--slurp",
            endpoint,
        ]
        for field in fields:
            argv.extend(("-f", field))
        result = self._run(argv, **options)
        if getattr(result, "returncode", 1) != 0:
            raise OSError(failure)
        raw = (getattr(result, "stdout", "") or "").strip()
        if not raw:
            return []
        document = json.loads(raw)
        if not isinstance(document, list):
            raise TypeError("GitHub paginated response is not a list")
        pages = (
            document if all(isinstance(page, list) for page in document) else [document]
        )
        rows = [row for page in pages for row in page]
        if not all(isinstance(row, dict) for row in rows):
            raise TypeError("GitHub paginated response contains invalid entries")
        return rows

    def _ready_rows(self, *, timeout: float | None) -> list[dict[str, Any]]:
        rows = self._paginated_rows(
            f"repos/{self.repo}/issues",
            fields=("state=open", f"labels={self.label}", "per_page=100"),
            timeout=timeout,
            failure="GitHub ready-Item listing failed",
        )
        # GitHub's REST Issues endpoint also returns pull requests.
        return [row for row in rows if "pull_request" not in row]

    def _comments(
        self, item: dict[str, Any], *, timeout: float | None
    ) -> list[IssueComment]:
        embedded = item.get("comments")
        if isinstance(embedded, list):
            rows = embedded
        else:
            rows = self._paginated_rows(
                f"repos/{self.repo}/issues/{item['number']}/comments",
                fields=("per_page=100",),
                timeout=timeout,
                failure="GitHub Item-comment listing failed",
            )
        return [
            IssueComment(
                author=comment_author(comment),
                body=comment.get("body") or "",
            )
            for comment in sorted(
                rows,
                key=lambda value: value.get("created_at", value.get("createdAt", "")),
            )
        ]

    def list_ready(self, *, timeout: float | None = None) -> list[IssueRef]:
        """Return every open Issue carrying the ready label, with full facts."""
        issues: list[IssueRef] = []
        for item in self._ready_rows(timeout=timeout):
            labels = [
                lbl["name"] if isinstance(lbl, dict) else lbl
                for lbl in item.get("labels", [])
            ]
            issues.append(
                IssueRef(
                    number=item["number"],
                    title=item.get("title", ""),
                    body=item.get("body") or "",
                    labels=labels,
                    assignees=assignee_logins(item),
                    comments=self._comments(item, timeout=timeout),
                )
            )
        return issues

    def list_ready_metadata(self, *, timeout: float | None = None) -> list[IssueRef]:
        """List ready Items without fetching bodies, comments, or other content."""
        issues: list[IssueRef] = []
        try:
            rows = self._ready_rows(timeout=timeout)
        except OSError as exc:
            raise OSError("GitHub ready-Item metadata listing failed") from exc
        for item in rows:
            labels = [
                label["name"] if isinstance(label, dict) else label
                for label in item.get("labels", [])
            ]
            issues.append(
                IssueRef(
                    number=item["number"],
                    title=item.get("title", ""),
                    body="",
                    labels=labels,
                    assignees=assignee_logins(item),
                    comments=[],
                )
            )
        return issues

    def is_still_eligible(
        self,
        frozen_item: IssueRef,
        *,
        assignee: str,
        include_unassigned: bool,
    ) -> bool:
        """Recheck only the mutable eligibility facts for one frozen Item."""
        result = self._run(
            [
                "gh",
                "issue",
                "view",
                str(frozen_item.number),
                "-R",
                self.repo,
                "--json",
                "number,state,labels,assignees",
            ],
            capture=True,
        )
        if getattr(result, "returncode", 1) != 0:
            raise OSError("GitHub Item eligibility recheck failed")
        try:
            item = json.loads((result.stdout or "").strip())
            if not isinstance(item, dict) or item.get("number") != frozen_item.number:
                raise ValueError("GitHub returned a different Item")
            state = item["state"]
            raw_labels = item["labels"]
            raw_assignees = item["assignees"]
            if not isinstance(state, str) or not isinstance(raw_labels, list):
                raise ValueError("GitHub returned malformed eligibility facts")
            if not isinstance(raw_assignees, list):
                raise ValueError("GitHub returned malformed eligibility facts")
            labels = [
                label["name"] if isinstance(label, dict) else label
                for label in raw_labels
            ]
            current_assignees = [
                entry["login"] if isinstance(entry, dict) else entry
                for entry in raw_assignees
            ]
            if not all(isinstance(label, str) for label in labels) or not all(
                isinstance(login, str) for login in current_assignees
            ):
                raise ValueError("GitHub returned malformed eligibility facts")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise OSError("GitHub Item eligibility recheck failed") from exc

        ownership_matches = assignee in current_assignees or (
            include_unassigned and not current_assignees
        )
        return state.upper() == "OPEN" and self.label in labels and ownership_matches

    def claim(self, number: int, *, assignee: str) -> None:
        """Assign the issue and drop the ready label so other runs skip it."""
        result = self._run(
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
        if getattr(result, "returncode", 1) != 0:
            raise OSError("GitHub Item claim failed")

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
