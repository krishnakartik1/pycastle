"""The walking skeleton: one ready issue becomes one pull request."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pycastle import orchestrator
from pycastle.models import IssueRef
from pycastle.runtime import STUB_MARKER, StubRuntime


def _ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="")


def _calls_containing(runner: MagicMock, *needles: str) -> bool:
    for call in runner.call_args_list:
        argv = call.args[0]
        if all(needle in argv for needle in needles):
            return True
    return False


def test_run_works_one_issue_into_one_pr(fixture_dir: Path, tmp_path: Path) -> None:
    issue = IssueRef(number=2, title="Walking skeleton", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = MagicMock(side_effect=_ok)

    outcome = orchestrator.run(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        workspace=tmp_path,
        runner=runner,
    )

    assert outcome.issue is not None and outcome.issue.number == 2
    assert outcome.branch == "pycastle/issue-2-walking-skeleton"
    assert outcome.pr_opened is True
    source.claim.assert_called_once_with(2, assignee="krishna")
    assert (tmp_path / STUB_MARKER).is_file()
    assert _calls_containing(runner, "git", "checkout", "-b", outcome.branch)
    assert _calls_containing(runner, "gh", "pr", "create")


def test_run_is_a_noop_when_no_issue_is_ready(
    fixture_dir: Path, tmp_path: Path
) -> None:
    source = MagicMock()
    source.list_ready.return_value = []
    runner = MagicMock(side_effect=_ok)

    outcome = orchestrator.run(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        workspace=tmp_path,
        runner=runner,
    )

    assert outcome.issue is None
    assert outcome.pr_opened is False
    source.claim.assert_not_called()
    runner.assert_not_called()
