"""Issue-source selection is pure; the gh boundary is mocked."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

from pycastle.issues import (
    GitHubIssueSource,
    assignee_logins,
    filter_for_assignee,
    select_batch,
    select_next,
)
from pycastle.models import IssueRef


def _issue(number: int, assignees: list[str]) -> IssueRef:
    return IssueRef(number=number, title=f"issue {number}", assignees=assignees)


def test_assignee_logins_accepts_raw_and_simplified_shapes() -> None:
    issue = {"assignees": ["alice", {"login": "bob"}, {"nope": 1}]}
    assert assignee_logins(issue) == ["alice", "bob"]


def test_filter_for_assignee_keeps_only_matching_by_default() -> None:
    issues = [_issue(1, []), _issue(2, ["krishna"]), _issue(3, ["someone"])]
    assert [i.number for i in filter_for_assignee(issues, "krishna")] == [2]


def test_filter_for_assignee_can_include_unassigned() -> None:
    issues = [_issue(1, []), _issue(2, ["krishna"]), _issue(3, ["someone"])]
    kept = filter_for_assignee(issues, "krishna", include_unassigned=True)
    assert [i.number for i in kept] == [1, 2]


def test_select_next_returns_lowest_numbered_eligible() -> None:
    issues = [_issue(5, ["krishna"]), _issue(3, ["krishna"]), _issue(9, ["other"])]
    chosen = select_next(issues, assignee="krishna")
    assert chosen is not None and chosen.number == 3


def test_select_next_returns_none_when_nothing_eligible() -> None:
    issues = [_issue(1, ["other"])]
    assert select_next(issues, assignee="krishna") is None


def test_select_batch_returns_up_to_limit_lowest_first() -> None:
    issues = [_issue(5, ["krishna"]), _issue(3, ["krishna"]), _issue(9, ["krishna"])]
    chosen = select_batch(issues, assignee="krishna", limit=2)
    assert [i.number for i in chosen] == [3, 5]


def test_select_batch_filters_by_assignee_without_mocks() -> None:
    issues = [_issue(1, ["other"]), _issue(2, ["krishna"]), _issue(4, [])]
    chosen = select_batch(issues, assignee="krishna", limit=10)
    assert [i.number for i in chosen] == [2]


def test_select_batch_can_include_unassigned() -> None:
    issues = [_issue(1, []), _issue(2, ["krishna"]), _issue(3, ["other"])]
    chosen = select_batch(issues, assignee="krishna", include_unassigned=True, limit=10)
    assert [i.number for i in chosen] == [1, 2]


def test_select_batch_returns_all_when_limit_exceeds_eligible() -> None:
    issues = [_issue(2, ["krishna"]), _issue(7, ["krishna"])]
    chosen = select_batch(issues, assignee="krishna", limit=99)
    assert [i.number for i in chosen] == [2, 7]


def test_select_batch_is_empty_for_nonpositive_limit() -> None:
    issues = [_issue(2, ["krishna"])]
    assert select_batch(issues, assignee="krishna", limit=0) == []


def test_select_batch_returns_all_when_limit_equals_eligible() -> None:
    # The boundary where limit is exactly the eligible count: take all, in order,
    # with no off-by-one trimming.
    issues = [_issue(7, ["krishna"]), _issue(2, ["krishna"]), _issue(5, ["krishna"])]
    chosen = select_batch(issues, assignee="krishna", limit=3)
    assert [i.number for i in chosen] == [2, 5, 7]


def test_select_batch_caps_unassigned_interaction_at_limit() -> None:
    # include_unassigned widens the eligible set, but the limit still caps the
    # batch and selection stays lowest-numbered first across both kinds.
    issues = [
        _issue(1, []),
        _issue(2, ["krishna"]),
        _issue(3, []),
        _issue(4, ["other"]),
    ]
    chosen = select_batch(issues, assignee="krishna", include_unassigned=True, limit=2)
    assert [i.number for i in chosen] == [1, 2]


def test_github_source_parses_list_output() -> None:
    payload = json.dumps(
        [
            {
                "number": 7,
                "title": "do a thing",
                "body": "details",
                "labels": [{"name": "ready-for-agent"}],
                "assignees": [{"login": "krishna"}],
            }
        ]
    )
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=payload)
    )
    source = GitHubIssueSource("owner/repo", runner=runner)

    issues = source.list_ready()

    assert len(issues) == 1
    assert issues[0].number == 7
    assert issues[0].labels == ["ready-for-agent"]
    assert issues[0].assignees == ["krishna"]


def test_github_source_handles_empty_output() -> None:
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="")
    )
    source = GitHubIssueSource("owner/repo", runner=runner)
    assert source.list_ready() == []


def test_github_source_claim_assigns_and_drops_label() -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
    source = GitHubIssueSource("owner/repo", runner=runner)

    source.claim(42, assignee="krishna")

    runner.assert_called_once_with(
        [
            "gh",
            "issue",
            "edit",
            "42",
            "-R",
            "owner/repo",
            "--add-assignee",
            "krishna",
            "--remove-label",
            "ready-for-agent",
        ],
        capture=True,
    )


def test_github_source_mark_for_human_adds_the_label() -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
    source = GitHubIssueSource("owner/repo", runner=runner)

    source.mark_for_human(42)

    runner.assert_called_once_with(
        [
            "gh",
            "issue",
            "edit",
            "42",
            "-R",
            "owner/repo",
            "--add-label",
            "ready-for-human",
        ],
        capture=True,
    )


def test_github_source_release_restores_the_ready_label() -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0))
    source = GitHubIssueSource("owner/repo", runner=runner)

    source.release(42)

    runner.assert_called_once_with(
        [
            "gh",
            "issue",
            "edit",
            "42",
            "-R",
            "owner/repo",
            "--add-label",
            "ready-for-agent",
        ],
        capture=True,
    )
