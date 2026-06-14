"""Batch run: ready issues become per-issue worktrees merged into one PR."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    # A clean run never enters the interrupt path: nothing is released back to
    # ready-for-agent and no issue is handed to a human (cancellation ≠ handoff).
    source.release.assert_not_called()
    source.mark_for_human.assert_not_called()


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


def test_merge_conflict_marks_for_human_and_run_continues(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A merge conflict on one issue must not sink the batch: the conflicting
    # issue is handed to a human (ready-for-human) and the run keeps going with
    # the remaining items (#9).
    issues = [
        IssueRef(number=2, title="Clean", assignees=["krishna"]),
        IssueRef(number=4, title="Conflicts", assignees=["krishna"]),
        IssueRef(number=6, title="Also clean", assignees=["krishna"]),
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

    # The clean issues merged; the conflicting one was aborted and recorded.
    assert outcome.completed == [2, 6]
    merged = {o.issue.number: o.merged for o in outcome.issues}
    assert merged == {2: True, 4: False, 6: True}
    assert _calls_containing(runner, "git", "merge", "--abort")

    # The conflicting issue is marked for human handling; the clean ones are not.
    source.mark_for_human.assert_called_once_with(4)

    # The PR closes only the merged issues, never the conflicting one.
    pr_calls = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "create"]
    ]
    body = pr_calls[0][pr_calls[0].index("--body") + 1]
    assert "- Closes #2" in body
    assert "- Closes #6" in body
    assert "#4" not in body


def test_merge_conflict_on_the_first_issue_still_runs_the_rest(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A conflict on the *first* issue must not abort the batch before it starts:
    # only that issue is handed to a human, and the later issues still merge into
    # one PR (#9). Pairs with the mid-batch conflict test above.
    issues = [
        IssueRef(number=2, title="Conflicts", assignees=["krishna"]),
        IssueRef(number=4, title="Clean", assignees=["krishna"]),
        IssueRef(number=6, title="Also clean", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner(merge_fails_for={2})

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

    # Only the first issue conflicted; the rest merged and the run carried on.
    assert outcome.completed == [4, 6]
    merged = {o.issue.number: o.merged for o in outcome.issues}
    assert merged == {2: False, 4: True, 6: True}

    # The conflicting first issue is the only one handed to a human; cancellation
    # is not in play, so nothing was released back to ready-for-agent.
    source.mark_for_human.assert_called_once_with(2)
    source.release.assert_not_called()

    # The PR closes the clean issues only, never the conflicting first one.
    pr_calls = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "create"]
    ]
    body = pr_calls[0][pr_calls[0].index("--body") + 1]
    assert "- Closes #4" in body
    assert "- Closes #6" in body
    assert "#2" not in body


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


# --------------------------------------------------------------------------- #
# Interrupt (SIGINT) cleanup and ready-state restore (#9).                     #
# --------------------------------------------------------------------------- #


class _InterruptingRuntime:
    """A fake Runtime that raises ``KeyboardInterrupt`` while working an issue.

    Stands in for a SIGINT arriving mid-issue: the graph calls ``run`` and the
    interrupt propagates out of the per-issue work so the run's cleanup/restore
    path is exercised without sending a real signal.
    """

    name = "stub"

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        raise KeyboardInterrupt


def test_interrupt_mid_issue_cleans_worktrees_and_restores_ready_state(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A SIGINT (KeyboardInterrupt) while an issue is in flight must leave no mess:
    # the in-flight worktree and the run worktree are removed, and the claimed
    # issue is released back to ready-for-agent so it is not stuck claimed (#9).
    issue = IssueRef(number=2, title="Interrupted", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()

    raised = False
    try:
        orchestrator.run_batch(
            runtime=_InterruptingRuntime(),
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
    except KeyboardInterrupt:
        raised = True

    # The interrupt propagates so the caller learns the run was cancelled.
    assert raised

    # The in-flight issue was claimed, then released back to ready-for-agent.
    source.claim.assert_called_once_with(2, assignee="krishna")
    source.release.assert_called_once_with(2)
    # The interrupt never reaches a human handoff: that is for conflicts, not
    # cancellation. The issue simply returns to the ready pool.
    source.mark_for_human.assert_not_called()

    # No orphaned worktrees: the in-flight issue worktree and the run worktree
    # are both removed exactly once each — no double-removal from both the
    # interrupt path and the normal end-of-run teardown.
    removed = [
        call.args[0][3]
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "worktree", "remove"]
    ]
    assert removed.count(str(tmp_path / "wt" / "issue-2")) == 1
    assert removed.count(str(tmp_path / "wt" / "run-20260613-101500")) == 1
    assert _calls_containing(runner, "git", "worktree", "prune")

    # A cancelled run opens no PR.
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_interrupt_restores_only_the_in_flight_issue(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # With several ready issues, an interrupt on the first one must release only
    # that claimed issue; later issues were never claimed, so nothing to restore.
    issues = [
        IssueRef(number=2, title="First", assignees=["krishna"]),
        IssueRef(number=4, title="Second", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner()

    try:
        orchestrator.run_batch(
            runtime=_InterruptingRuntime(),
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
    except KeyboardInterrupt:
        pass

    # Only the in-flight issue (#2) was claimed and released; #4 was never reached.
    source.claim.assert_called_once_with(2, assignee="krishna")
    source.release.assert_called_once_with(2)


def test_interrupt_cleanup_still_releases_when_worktree_removal_errors(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # Teardown is best-effort: if removing a worktree itself raises, the run must
    # still release the issue back to ready-for-agent rather than leaving it
    # stuck claimed. The removal error is swallowed (logged), not propagated.
    issue = IssueRef(number=2, title="Interrupted", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        if argv[:3] == ["git", "worktree", "remove"]:
            raise RuntimeError("worktree busy")
        return _ok()

    runner = MagicMock(side_effect=side_effect)

    raised = False
    try:
        orchestrator.run_batch(
            runtime=_InterruptingRuntime(),
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
    except KeyboardInterrupt:
        raised = True

    # The interrupt still propagates even though teardown hit an error.
    assert raised
    # A failed worktree removal did not stop the restore: the issue is released.
    source.release.assert_called_once_with(2)


def test_interrupt_after_all_issues_complete_is_a_clean_run(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # The interrupt seam only fires while an issue is in flight. Once every issue
    # has merged, ``in_flight`` is cleared, so finishing the batch is a clean run:
    # nothing is released and the PR is opened normally.
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

    # Both issues merged into one PR; no cancellation path was taken.
    assert outcome.completed == [2, 4]
    assert outcome.pr_opened is True
    source.release.assert_not_called()
    source.mark_for_human.assert_not_called()


def test_sigint_handler_is_restored_after_the_context_exits() -> None:
    # Signal-handler hygiene: the previous SIGINT handler is restored on exit, so
    # the run does not leave its KeyboardInterrupt-raising handler installed.
    # signal.signal is mocked, so no real handler is ever touched.
    sentinel = object()
    with patch("pycastle.orchestrator.signal.signal", return_value=sentinel) as sig:
        with orchestrator._sigint_as_keyboard_interrupt():
            pass

    # First call installs the handler and returns the previous one; the last call
    # restores exactly that previous handler.
    assert sig.call_args_list[0].args[0] == signal.SIGINT
    assert sig.call_args_list[-1].args == (signal.SIGINT, sentinel)


def test_sigint_handler_off_main_thread_value_error_is_swallowed() -> None:
    # Off the main thread, signal.signal raises ValueError. The context manager
    # must swallow it and still yield (so a threaded run is not broken) and must
    # not attempt to restore a handler it never installed.
    with patch("pycastle.orchestrator.signal.signal", side_effect=ValueError) as sig:
        entered = False
        with orchestrator._sigint_as_keyboard_interrupt():
            entered = True

    # The body still ran, and only the (failed) install was attempted — no
    # restore call, since there is no previous handler to put back.
    assert entered
    assert sig.call_count == 1
