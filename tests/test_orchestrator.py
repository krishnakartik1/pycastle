"""Batch run: ready issues become per-issue worktrees merged into one PR."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pycastle import orchestrator
from pycastle.models import IssueRef, RuntimeResult, Telemetry
from pycastle.runtime import STUB_MARKER, StubRuntime


def _ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="")


def _calls_containing(runner: MagicMock, *needles: str) -> bool:
    for call in runner.call_args_list:
        argv = call.args[0]
        if all(needle in argv for needle in needles):
            return True
    return False


def _git_aware_runner(merge_fails_for: set[int] | None = None) -> MagicMock:
    """A fake runner that simulates the git side effects the run depends on.

    ``git worktree add <path> <branch>`` creates the worktree directory so the
    graph's stub runtime has a real ``cwd`` to write its marker into. A merge of
    a branch whose issue number is in ``merge_fails_for`` returns non-zero so the
    conflict-skip seam can be exercised. Everything else is a clean success. No
    real git or gh is ever invoked.
    """
    merge_fails_for = merge_fails_for or set()

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        if argv[:2] == ["git", "merge"] and "--abort" not in argv:
            branch = argv[2]
            number = int(branch.split("-")[1])
            if number in merge_fails_for:
                return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        return _ok()

    return MagicMock(side_effect=side_effect)


def test_batch_works_up_to_n_issues_into_one_pr(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issues = [
        IssueRef(number=2, title="First slice", assignees=["krishna"]),
        IssueRef(number=4, title="Second slice", assignees=["krishna"]),
        IssueRef(number=6, title="Third slice", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner()

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=2,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # Up to N (2) of the 3 ready issues are worked, lowest-numbered first.
    assert [o.issue.number for o in outcome.issues] == [2, 4]
    assert outcome.completed == [2, 4]
    assert outcome.pr_opened is True
    assert outcome.run_branch == "pycastle/run-20260613-101500"

    # Each issue is claimed before work.
    assert source.claim.call_count == 2
    source.claim.assert_any_call(2, assignee="krishna")
    source.claim.assert_any_call(4, assignee="krishna")


def test_per_run_branch_and_worktrees_leave_main_checkout_untouched(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Walking skeleton", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    run_branch = "pycastle/run-20260613-101500"
    issue_branch = "pycastle/issue-2-walking-skeleton"

    # The run branch is cut off the base and added as its own worktree.
    assert _calls_containing(runner, "git", "branch", run_branch, "main")
    assert _calls_containing(runner, "git", "worktree", "add", run_branch)
    # The issue is branched off the *run* branch into its own worktree.
    assert _calls_containing(runner, "git", "branch", issue_branch, run_branch)
    assert _calls_containing(runner, "git", "worktree", "add", issue_branch)

    # The main checkout is never switched: no `git checkout` in the workspace.
    for call in runner.call_args_list:
        assert call.args[0][:2] != ["git", "checkout"]

    # The stub wrote into the per-issue worktree, not the main checkout.
    assert not (tmp_path / STUB_MARKER).is_file()
    assert (tmp_path / "wt" / "issue-2" / STUB_MARKER).is_file()
    assert outcome.issues[0].branch == issue_branch


def test_successful_branches_merge_and_one_pr_is_opened(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issues = [
        IssueRef(number=2, title="First", assignees=["krishna"]),
        IssueRef(number=4, title="Second", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner()

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=5,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # Both issue branches were merged into the run branch.
    assert _calls_containing(runner, "git", "merge", "pycastle/issue-2-first")
    assert _calls_containing(runner, "git", "merge", "pycastle/issue-4-second")

    # Exactly one PR is opened, for the run branch, closing every merged issue.
    pr_calls = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "create"]
    ]
    assert len(pr_calls) == 1
    pr_argv = pr_calls[0]
    assert "pycastle/run-20260613-101500" in pr_argv
    body = pr_argv[pr_argv.index("--body") + 1]
    assert "- Closes #2" in body
    assert "- Closes #4" in body
    assert outcome.pr_opened is True


def test_failed_merge_is_skipped_and_left_for_issue_8(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issues = [
        IssueRef(number=2, title="Clean", assignees=["krishna"]),
        IssueRef(number=4, title="Conflicts", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    # Issue 4's merge does not apply cleanly.
    runner = _git_aware_runner(merge_fails_for={4})

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=5,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # The clean issue merged; the conflicting one was aborted and recorded.
    assert outcome.completed == [2]
    merged = {o.issue.number: o.merged for o in outcome.issues}
    assert merged == {2: True, 4: False}
    assert _calls_containing(runner, "git", "merge", "--abort")
    # The PR closes only the merged issue.
    pr_calls = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "create"]
    ]
    body = pr_calls[0][pr_calls[0].index("--body") + 1]
    assert "- Closes #2" in body
    assert "#4" not in body


def test_telemetry_and_run_log_are_written_into_the_fixture(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Telemetry", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()

    orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    run_dir = fixture_dir / "runs" / "20260613-101500"
    # Per-phase telemetry lands under the ignored .pycastle/runs/<run_id>/ path.
    telemetry_path = run_dir / "issue-2-telemetry.json"
    assert telemetry_path.is_file()
    records = json.loads(telemetry_path.read_text())
    assert records[0]["runtime"] == "stub"
    assert records[0]["phase"] == "implement"
    assert records[0]["num_turns"] == 1

    # Only the telemetry numbers are written: the agent's prose output never
    # reaches disk, so nothing credential-like leaks into the run directory.
    raw_telemetry = telemetry_path.read_text()
    assert "output" not in raw_telemetry
    assert STUB_MARKER not in raw_telemetry  # the stub's prose mentions the marker

    # A human-readable run log is written too.
    log_text = (run_dir / "run.log").read_text()
    assert "Working #2" in log_text
    assert "Merged #2" in log_text


def test_batch_is_a_noop_when_no_issue_is_ready(
    fixture_dir: Path, tmp_path: Path
) -> None:
    source = MagicMock()
    source.list_ready.return_value = []
    runner = MagicMock(side_effect=_ok)

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=3,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    assert outcome.issues == []
    assert outcome.completed == []
    assert outcome.pr_opened is False
    source.claim.assert_not_called()
    # No branches, worktrees, or PRs when there is nothing to do.
    assert not _calls_containing(runner, "git", "worktree", "add")
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_worktrees_are_cleaned_up_after_the_run(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Cleanup", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()

    orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # Both the issue worktree and the run worktree are removed and pruned.
    assert _calls_containing(runner, "git", "worktree", "remove")
    assert _calls_containing(runner, "git", "worktree", "prune")
    # The per-issue branch is deleted; the run branch stays (it carries the PR).
    assert _calls_containing(runner, "git", "branch", "-D", "pycastle/issue-2-cleanup")


def test_worktrees_are_cleaned_up_even_when_an_issue_is_skipped(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A skipped (conflicting) issue must not leave its worktree behind, and the
    # run worktree is still torn down once the batch finishes.
    issues = [
        IssueRef(number=2, title="Clean", assignees=["krishna"]),
        IssueRef(number=4, title="Conflicts", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner(merge_fails_for={4})

    orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=5,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # Every issue worktree is removed, including the skipped issue's, plus the
    # run worktree; both per-issue branches are deleted regardless of merge.
    removed = [
        call.args[0][3]
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "worktree", "remove"]
    ]
    assert str(tmp_path / "wt" / "issue-2") in removed
    assert str(tmp_path / "wt" / "issue-4") in removed
    assert str(tmp_path / "wt" / "run-20260613-101500") in removed
    assert _calls_containing(
        runner, "git", "branch", "-D", "pycastle/issue-4-conflicts"
    )


# --------------------------------------------------------------------------- #
# Default plan -> implement -> review graph, end to end (#7).                  #
# --------------------------------------------------------------------------- #


class _TimelineRuntime:
    """A fake Runtime that appends each phase it runs to a shared timeline.

    Sharing one ``timeline`` list with the timeline-aware runner lets a test
    assert the interleaved order of agent phases and git operations — e.g. that
    the ``review`` phase ran before the commit and merge.
    """

    name = "stub"

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        self._timeline.append(f"phase:{phase}")
        (cwd / STUB_MARKER).write_text(f"phase {phase}\n")
        return RuntimeResult(
            output=f"ran {phase}",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


def _timeline_runner(timeline: list[str]) -> MagicMock:
    """A git-aware runner that records ``commit`` and ``merge`` on a timeline.

    Like :func:`_git_aware_runner` it creates worktree directories so the stub
    has a real ``cwd``; in addition it appends a marker to the shared timeline
    when an issue branch is committed or merged, so a test can pin their order
    relative to the agent phases. No real git or gh is ever invoked.
    """

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        elif argv[:2] == ["git", "commit"]:
            timeline.append("git:commit")
        elif argv[:2] == ["git", "merge"] and "--abort" not in argv:
            timeline.append("git:merge")
        return _ok()

    return MagicMock(side_effect=side_effect)


def test_default_run_completes_plan_implement_review_in_order(
    three_phase_fixture_dir: Path, tmp_path: Path
) -> None:
    """A run drives the full plan → implement → review cycle on an issue, in order."""
    issue = IssueRef(number=2, title="Full cycle", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    timeline: list[str] = []
    runner = _timeline_runner(timeline)

    outcome = orchestrator.run_batch(
        runtime=_TimelineRuntime(timeline),
        issue_source=source,
        fixture_dir=three_phase_fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    phases = [
        event[len("phase:") :] for event in timeline if event.startswith("phase:")
    ]
    assert phases == ["plan", "implement", "review"]
    assert outcome.completed == [2]


def test_review_changes_are_committed_before_the_merge(
    three_phase_fixture_dir: Path, tmp_path: Path
) -> None:
    """The review phase runs and its changes are committed before ``git merge``.

    Acceptance criterion: review tests edge cases and commits improvements
    before merge. The single commit after the graph captures plan + implement +
    review changes; this pins that the review phase ran, then the issue branch
    was committed, then merged — review's work lands on the branch before it is
    folded into the run branch.
    """
    issue = IssueRef(number=2, title="Review before merge", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    timeline: list[str] = []
    runner = _timeline_runner(timeline)

    orchestrator.run_batch(
        runtime=_TimelineRuntime(timeline),
        issue_source=source,
        fixture_dir=three_phase_fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # The review phase ran, then the branch was committed, then merged — in that
    # order, so review's improvements are on the branch before the merge.
    assert "phase:review" in timeline
    assert "git:commit" in timeline
    assert "git:merge" in timeline
    review_at = timeline.index("phase:review")
    commit_at = timeline.index("git:commit")
    merge_at = timeline.index("git:merge")
    assert review_at < commit_at < merge_at


def test_full_run_interleaves_phases_then_commit_then_merge(
    three_phase_fixture_dir: Path, tmp_path: Path
) -> None:
    """One timeline pins plan → implement → review → commit → merge, in order.

    The phase-ordering check and the review-before-merge check share a single
    timeline here, so the whole sequence is asserted at once rather than split
    across two tests. Because every event is read off one ordered list — not from
    dict iteration or call counts — the guarantee does not depend on luck: the
    issue branch is committed only after review runs, and merged only after the
    commit, so review's improvements are always on the branch at merge time.
    """
    issue = IssueRef(number=2, title="Whole cycle", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    timeline: list[str] = []
    runner = _timeline_runner(timeline)

    outcome = orchestrator.run_batch(
        runtime=_TimelineRuntime(timeline),
        issue_source=source,
        fixture_dir=three_phase_fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # The full interleaved order of agent phases and git operations for the issue.
    assert timeline == [
        "phase:plan",
        "phase:implement",
        "phase:review",
        "git:commit",
        "git:merge",
    ]
    assert outcome.completed == [2]
