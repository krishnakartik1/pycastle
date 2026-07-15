"""Batch run: ready issues become per-issue worktrees merged into one PR."""

from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pycastle import orchestrator
from pycastle.models import IssueComment, IssueRef, RuntimeResult, Telemetry
from pycastle.runtime import STUB_MARKER, AgentCrashError, StubRuntime


def _ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="")


def _calls_containing(runner: MagicMock, *needles: str) -> bool:
    for call in runner.call_args_list:
        argv = call.args[0]
        if all(needle in argv for needle in needles):
            return True
    return False


def test_prune_run_branches_deletes_only_branches_with_closed_or_merged_prs(
    tmp_path: Path,
) -> None:
    """Closed/merged PR heads are pruned while open PR heads stay intact (#69)."""
    runner = MagicMock(
        side_effect=[
            MagicMock(
                returncode=0,
                stdout=(
                    '[{"headRefName":"pycastle/run-open","state":"OPEN"},'
                    '{"headRefName":"pycastle/run-merged","state":"MERGED"},'
                    '{"headRefName":"pycastle/run-closed","state":"CLOSED"}]'
                ),
            ),
            MagicMock(
                returncode=0,
                stdout=(
                    "aaa\trefs/heads/pycastle/run-open\n"
                    "bbb\trefs/heads/pycastle/run-merged\n"
                    "ccc\trefs/heads/pycastle/run-closed\n"
                    "ddd\trefs/heads/pycastle/run-recovery\n"
                ),
            ),
            MagicMock(returncode=0),
            MagicMock(returncode=0),
        ]
    )

    deleted = orchestrator.prune_run_branches(
        repo="owner/repo", runner=runner, cwd=tmp_path
    )

    assert deleted == ["pycastle/run-merged", "pycastle/run-closed"]
    assert _calls_containing(runner, "--state", "all", "--limit", "10000")
    assert not _calls_containing(runner, "--delete", "pycastle/run-open")
    assert _calls_containing(runner, "--delete", "pycastle/run-merged")
    assert _calls_containing(runner, "--delete", "pycastle/run-closed")
    assert not _calls_containing(runner, "--delete", "pycastle/run-recovery")


def test_prune_run_branches_include_no_pr_deletes_recovery_branches(
    tmp_path: Path,
) -> None:
    runner = MagicMock(
        side_effect=[
            MagicMock(
                returncode=0,
                stdout='[{"headRefName":"pycastle/run-open","state":"OPEN"}]',
            ),
            MagicMock(
                returncode=0,
                stdout=(
                    "aaa\trefs/heads/pycastle/run-open\n"
                    "bbb\trefs/heads/pycastle/run-recovery\n"
                ),
            ),
            MagicMock(returncode=0),
        ]
    )

    deleted = orchestrator.prune_run_branches(
        repo="owner/repo", runner=runner, cwd=tmp_path, include_no_pr=True
    )

    assert deleted == ["pycastle/run-recovery"]
    assert not _calls_containing(runner, "--delete", "pycastle/run-open")


def test_prune_run_branches_aborts_before_deletion_when_open_pr_lookup_fails(
    tmp_path: Path,
) -> None:
    """An unknown open-PR set must never permit a destructive prune (#69)."""
    runner = MagicMock(
        return_value=MagicMock(returncode=1, stdout="", stderr="gh failed")
    )

    with pytest.raises(orchestrator.PruneError, match="open pull requests"):
        orchestrator.prune_run_branches(repo="owner/repo", runner=runner, cwd=tmp_path)

    assert not _calls_containing(runner, "git", "push", "origin", "--delete")


@pytest.mark.parametrize(
    "stdout",
    [None, "", "{}", '[{"headRefName":null,"state":"OPEN"}]'],
)
def test_prune_run_branches_aborts_on_invalid_open_pr_output(
    tmp_path: Path, stdout: str | None
) -> None:
    """Missing or malformed API fields cannot be mistaken for no open PRs."""
    runner = MagicMock(return_value=MagicMock(returncode=0, stdout=stdout))

    with pytest.raises(orchestrator.PruneError, match="parse open pull requests"):
        orchestrator.prune_run_branches(repo="owner/repo", runner=runner, cwd=tmp_path)

    runner.assert_called_once()


def test_prune_run_branches_handles_empty_remote(tmp_path: Path) -> None:
    runner = MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout="[]"),
            MagicMock(returncode=0, stdout=""),
        ]
    )

    assert (
        orchestrator.prune_run_branches(repo="owner/repo", runner=runner, cwd=tmp_path)
        == []
    )
    assert runner.call_count == 2


def test_prune_run_branches_aborts_when_remote_branch_lookup_fails(
    tmp_path: Path,
) -> None:
    runner = MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout="[]"),
            MagicMock(returncode=1, stdout="", stderr="git failed"),
        ]
    )

    with pytest.raises(orchestrator.PruneError, match="list remote run branches"):
        orchestrator.prune_run_branches(repo="owner/repo", runner=runner, cwd=tmp_path)

    assert not _calls_containing(runner, "--delete")


@pytest.mark.parametrize(
    "stdout",
    [
        None,
        "malformed ref output",
        "abc\trefs/heads/pycastle/run-\n",
        (
            "abc\trefs/heads/pycastle/run-duplicate\n"
            "abc\trefs/heads/pycastle/run-duplicate\n"
        ),
    ],
)
def test_prune_run_branches_aborts_on_invalid_remote_branch_output(
    tmp_path: Path, stdout: str | None
) -> None:
    runner = MagicMock(
        side_effect=[
            MagicMock(returncode=0, stdout="[]"),
            MagicMock(returncode=0, stdout=stdout),
        ]
    )

    with pytest.raises(orchestrator.PruneError, match="parse remote run branches"):
        orchestrator.prune_run_branches(repo="owner/repo", runner=runner, cwd=tmp_path)

    assert not _calls_containing(runner, "--delete")


def test_prune_run_branches_reports_deletion_failure(tmp_path: Path) -> None:
    branch = "pycastle/run-closed"
    runner = MagicMock(
        side_effect=[
            MagicMock(
                returncode=0,
                stdout=('[{"headRefName":"pycastle/run-closed",' '"state":"CLOSED"}]'),
            ),
            MagicMock(returncode=0, stdout=f"abc\trefs/heads/{branch}\n"),
            MagicMock(returncode=1, stderr="rejected"),
        ]
    )

    with pytest.raises(orchestrator.PruneError, match=branch):
        orchestrator.prune_run_branches(repo="owner/repo", runner=runner, cwd=tmp_path)


def _git_aware_runner(
    merge_fails_for: set[int] | None = None,
    empty_diff_for: set[int] | None = None,
) -> MagicMock:
    """A fake runner that simulates the git side effects the run depends on.

    ``git worktree add <path> <branch>`` creates the worktree directory so the
    graph's stub runtime has a real ``cwd`` to write its marker into. A merge of
    a branch whose issue number is in ``merge_fails_for`` returns non-zero so the
    conflict-skip seam can be exercised. ``git diff --quiet`` reports a non-empty
    diff (exit 1, "changes present") by default — the normal case where the phase
    wrote something — unless the branch's issue number is in ``empty_diff_for``,
    where it reports an empty diff (exit 0) to exercise the no-change seam (#35).
    Everything else is a clean success. No real git or gh is ever invoked.
    """
    merge_fails_for = merge_fails_for or set()
    empty_diff_for = empty_diff_for or set()

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        if argv[:3] == ["git", "diff", "--quiet"]:
            branch = argv[-1]
            number = int(branch.split("-")[1])
            # Exit 0 means "no diff"; exit 1 means "changes present". The normal
            # case has changes, so default to a non-empty diff.
            code = 0 if number in empty_diff_for else 1
            return subprocess.CompletedProcess(args=argv, returncode=code, stdout="")
        if argv[:2] == ["git", "merge"] and "--abort" not in argv:
            branch = argv[2]
            number = int(branch.split("-")[1])
            if number in merge_fails_for:
                return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="[]")
        return _ok()

    return MagicMock(side_effect=side_effect)


def _run_context(
    *,
    fixture_dir: Path,
    worktree: Path,
    runner: MagicMock,
    run_id: str = "run-86",
) -> orchestrator.RunContext:
    return orchestrator.RunContext(
        run_id=run_id,
        branch=f"pycastle/run-{run_id}",
        worktree=worktree,
        fixture_dir=fixture_dir,
        runner=runner,
    )


def _scoped_fixture(
    tmp_path: Path, *, before: bool = False, after: bool = False
) -> Path:
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    arguments = [
        "item=build(start='implement', phases=[phase('implement', 'item.md')])"
    ]
    if before:
        arguments.append(
            "before=build(start='before', phases=[phase('before', 'before.md')])"
        )
    if after:
        arguments.append(
            "after=build(start='after', phases=[phase('after', 'after.md')])"
        )
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, build_run, phase\n"
        f"run = build_run({', '.join(arguments)})\n"
    )
    for name in ("item", "before", "after"):
        (prompts / f"{name}.md").write_text(name)
    return fixture


def test_run_batch_walks_after_graph_runs_gate_and_publishes_draft_first(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, build_run, phase\n"
        "run = build_run(item=build(start='implement', phases=[phase('implement', 'item.md')]), "
        "after=build(start='integrated-review', phases=[phase('integrated-review', 'review.md')]))\n"
    )
    for name in ("item.md", "review.md"):
        (fixture / "prompts" / name).write_text(name)
    issue = IssueRef(number=101, title="Integrated review", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    timeline: list[tuple[str, Path | None]] = []
    run_worktree = tmp_path / "wt" / "run-run-101"
    authored_report = "# Integrated report\n\nRepaired and verified.\n"

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            timeline.append((phase, cwd))
            result = super().run(prompt, cwd=cwd, phase=phase)
            if phase == "integrated-review":
                (cwd / "integrated-review.txt").write_text("reviewed\n")
                report_path = cwd / orchestrator.RUN_REPORT
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(authored_report)
            return result

    def setup(cwd: Path) -> None:
        timeline.append(("setup", cwd))

    def gate(cwd: Path) -> orchestrator.GateOutcome:
        timeline.append(("gate", cwd))
        return orchestrator.GateOutcome(
            True,
            "secret raw output",
            exit_code=0,
            duration_seconds=1.25,
            command=".pycastle/gate --all",
        )

    base_runner = _git_aware_runner()
    publication_calls: list[list[str]] = []

    def external_boundary(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "diff", "--cached"]:
            return subprocess.CompletedProcess(argv, 1, stdout="")
        if argv[:3] == ["git", "commit", "-m"] and "Run phase" in argv[3]:
            timeline.append(("checkpoint", Path(str(kwargs["cwd"]))))
        if argv[:3] == ["gh", "pr", "list"]:
            publication_calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="[]")
        if argv[:3] == ["gh", "pr", "create"]:
            publication_calls.append(argv)
            timeline.append(("draft", None))
            return _ok()
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, stdout="314\n")
        if argv[:2] == ["gh", "api"]:
            publication_calls.append(argv)
            if "--paginate" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="[]")
            timeline.append(("comment", None))
            return _ok()
        if argv[:3] == ["gh", "pr", "ready"]:
            publication_calls.append(argv)
            timeline.append(("ready", None))
            return _ok()
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=external_boundary)

    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="run-101",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        gate_check=gate,
        setup=setup,
    )

    item_worktree = tmp_path / "wt" / "issue-101"
    assert timeline == [
        ("setup", run_worktree),
        ("setup", item_worktree),
        ("implement", item_worktree),
        ("gate", item_worktree),
        ("setup", run_worktree),
        ("integrated-review", run_worktree),
        ("checkpoint", run_worktree),
        ("gate", run_worktree),
        ("draft", None),
        ("comment", None),
        ("ready", None),
    ]
    assert outcome.pr_opened and outcome.pr_ready and outcome.succeeded
    assert (run_worktree / "integrated-review.txt").read_text() == "reviewed\n"

    run_phase_commits = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "commit", "-m"]
        and "Run phase" in call.args[0][3]
    ]
    assert run_phase_commits == [
        ["git", "commit", "-m", "chore: checkpoint Run phase integrated-review"]
    ]
    assert any(
        call[:3] == ["gh", "pr", "list"]
        and call[call.index("--head") + 1] == "pycastle/run-run-101"
        for call in publication_calls
    )
    comment_call = next(call for call in publication_calls if "--method" in call)
    comment = comment_call[comment_call.index("-f") + 1]
    assert comment_call[comment_call.index("--method") + 1] == "POST"
    assert "<!-- pycastle-run-report:run-101 -->" in comment
    assert "`.pycastle/gate --all` — PASS (exit 0, 1.25s)" in comment
    assert "secret raw output" not in comment
    assert comment.endswith("\n---\n\n" + authored_report)
    assert (fixture / "runs" / "run-101" / "run-report.md").read_text() == (
        authored_report
    )

    calls = [call.args[0] for call in runner.call_args_list]
    draft_index = next(
        i for i, call in enumerate(calls) if call[:3] == ["gh", "pr", "create"]
    )
    final_push_index = max(
        i
        for i, call in enumerate(calls[:draft_index])
        if call[:3] == ["git", "push", "-u"]
    )
    assert final_push_index < draft_index

    repeat_runner = MagicMock(
        side_effect=[
            _ok(),
            subprocess.CompletedProcess([], 0, stdout='[{"number":314}]'),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    '[{"id":2718,"body":"'
                    '<!-- pycastle-run-report:run-101 --> previous"}]'
                ),
            ),
            _ok(),
            _ok(),
        ]
    )
    assert orchestrator._open_pull_request(
        repo="owner/repo",
        base_branch="main",
        run=_run_context(
            fixture_dir=fixture,
            worktree=run_worktree,
            runner=repeat_runner,
            run_id="run-101",
        ),
        completed=[101],
        selected=[issue],
        skipped=[],
        gate=gate(run_worktree),
        report=None,
        publication_error=None,
        successful=True,
        stopping_point=None,
    ) == orchestrator.PublicationOutcome(True, True, True, True)
    repeat_calls = [call.args[0] for call in repeat_runner.call_args_list]
    assert not any(call[:3] == ["gh", "pr", "create"] for call in repeat_calls)
    assert repeat_calls[1][repeat_calls[1].index("--head") + 1] == (
        "pycastle/run-run-101"
    )
    assert repeat_calls[3][:5] == [
        "gh",
        "api",
        "--method",
        "PATCH",
        "repos/owner/repo/issues/comments/2718",
    ]


def test_before_run_prepares_frozen_batch_for_every_item_and_ready_pr(
    tmp_path: Path,
) -> None:
    """Preparation is durable, inherited, and cannot reselect the active Run."""
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    original_main = (
        "from pycastle.graph import DONE, build, build_run, phase\n"
        "run = build_run(\n"
        "    before=build(start='inventory', phases=[\n"
        "        phase('inventory', 'inventory.md', on_success='prepare'),\n"
        "        phase('prepare', 'prepare.md', on_success=DONE),\n"
        "    ]),\n"
        "    item=build(start='implement', phases=[phase('implement', 'item.md')]),\n"
        ")\n"
    )
    (fixture / "main.py").write_text(original_main)
    for name in ("inventory.md", "prepare.md", "item.md"):
        (fixture / "prompts" / name).write_text(name)

    issues = [
        IssueRef(number=103, title="Second selected", assignees=["krishna"]),
        IssueRef(number=101, title="First selected", assignees=["krishna"]),
        IssueRef(number=105, title="Not selected", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    selected_batch = [issues[1], issues[0]]
    run_worktree = tmp_path / "wt" / "run-frozen"
    item_starts: list[tuple[int, bool, str]] = []
    events: list[tuple[str, Path]] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            events.append((phase, cwd))
            if phase == "inventory":
                assert prompt.index("#101") < prompt.index("#103")
                assert "#105" not in prompt
                (cwd / "inventory.txt").write_text("frozen: 101,103\n")
                # A proposed fixture edit must not replace the already-loaded
                # before/Item graphs or alter the selected batch.
                (fixture / "main.py").write_text(
                    "raise RuntimeError('next Run only')\n"
                )
                source.list_ready.return_value = [issues[2]]
                selected_batch[:] = [issues[2]]
                issues[1].number = 999
                issues[1].title = "Mutated after selection"
            elif phase == "prepare":
                assert (cwd / "inventory.txt").is_file()
                (cwd / "prepared.txt").write_text("prepared\n")
            elif phase == "implement":
                number = int(cwd.name.removeprefix("issue-"))
                item_starts.append((number, (cwd / "prepared.txt").is_file(), prompt))
            return super().run(prompt, cwd=cwd, phase=phase)

    def setup(cwd: Path) -> None:
        events.append(("setup", cwd))

    base_runner = _git_aware_runner()

    def runner_side_effect(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "worktree", "add"] and "issue-" in argv[3]:
            destination = Path(argv[3])
            destination.mkdir(parents=True, exist_ok=True)
            for prepared in ("inventory.txt", "prepared.txt"):
                (destination / prepared).write_text(
                    (run_worktree / prepared).read_text()
                )
            return _ok()
        if argv[:3] == ["git", "diff", "--cached"]:
            return subprocess.CompletedProcess(argv, 1, stdout="")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout="[]")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, stdout="103\n")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]")
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=runner_side_effect)
    gate_paths: list[Path] = []

    def gate(cwd: Path) -> orchestrator.GateOutcome:
        gate_paths.append(cwd)
        return orchestrator.GateOutcome(True, "green", exit_code=0)

    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=selected_batch,
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="frozen",
        iterations=2,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        gate_check=gate,
        setup=setup,
    )

    assert events[:3] == [
        ("setup", run_worktree),
        ("inventory", run_worktree),
        ("prepare", run_worktree),
    ]
    assert [number for number, inherited, _prompt in item_starts] == [101, 103]
    assert all(inherited for _number, inherited, _prompt in item_starts)
    source.list_ready.assert_not_called()
    assert gate_paths[-1] == run_worktree
    assert outcome.completed == [101, 103]
    assert outcome.pr_opened and outcome.pr_ready and outcome.succeeded

    calls = [call.args[0] for call in runner.call_args_list]
    checkpoints = [
        call[3]
        for call in calls
        if call[:3] == ["git", "commit", "-m"] and "Run phase" in call[3]
    ]
    assert checkpoints == [
        "chore: checkpoint Run phase inventory",
        "chore: checkpoint Run phase prepare",
    ]
    first_item_branch = next(
        index
        for index, call in enumerate(calls)
        if call[:2] == ["git", "branch"]
        and call[-1] == "pycastle/run-frozen"
        and "issue-101" in call[2]
    )
    second_checkpoint_push = max(
        index
        for index, call in enumerate(calls[:first_item_branch])
        if call[:3] == ["git", "push", "-u"]
    )
    assert second_checkpoint_push < first_item_branch


def test_successful_run_phase_with_no_changes_pushes_without_empty_commit(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A successful no-op Run phase remains durable without a fake commit."""
    run_worktree = tmp_path / "run-worktree"
    runner = MagicMock(
        side_effect=[
            _ok(),
            subprocess.CompletedProcess([], 0, stdout=""),
            _ok(),
        ]
    )

    orchestrator._checkpoint_run_phase(
        orchestrator.Phase(name="integrated-review", prompt="review.md"),
        run=_run_context(
            fixture_dir=fixture_dir,
            worktree=run_worktree,
            runner=runner,
            run_id="run-101",
        ),
    )

    calls = [call.args[0] for call in runner.call_args_list]
    assert calls == [
        [
            "git",
            "add",
            "-A",
            "--",
            ".",
            ":(exclude,top).pycastle/run-review.md",
            ":(exclude,top).pycastle/run-report.md",
        ],
        ["git", "diff", "--cached", "--quiet"],
        ["git", "push", "-u", "origin", "pycastle/run-run-101"],
    ]


@pytest.mark.parametrize("failure", ["add", "diff", "commit"])
def test_run_phase_checkpoint_failure_never_pushes(
    fixture_dir: Path, tmp_path: Path, failure: str
) -> None:
    """A Run checkpoint is not durable until staging and commit both succeed."""

    def side_effect(argv: list[str], **_kwargs: object) -> object:
        if argv[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(argv, int(failure == "add"), stdout="")
        if argv[:3] == ["git", "diff", "--cached"]:
            code = 2 if failure == "diff" else 1
            return subprocess.CompletedProcess(argv, code, stdout="")
        if argv[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(
                argv, int(failure == "commit"), stdout=""
            )
        return _ok()

    runner = MagicMock(side_effect=side_effect)
    with pytest.raises(orchestrator.RunCheckpointError):
        orchestrator._checkpoint_run_phase(
            orchestrator.Phase(name="integrated-review", prompt="review.md"),
            run=_run_context(
                fixture_dir=fixture_dir,
                worktree=tmp_path / "run-worktree",
                runner=runner,
            ),
        )

    assert not _calls_containing(runner, "git", "push")


def test_failed_run_phase_removes_ignored_scratch_artifacts(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """Resetting a red Run phase also removes its ignored review/report files."""
    worktree = tmp_path / "run-worktree"
    worktree.mkdir()
    (fixture_dir / "prompts" / "run.md").write_text("Run phase")

    class FailingRuntime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            for path in (orchestrator.RUN_REVIEW, orchestrator.RUN_REPORT):
                target = cwd / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("partial")
            raise AgentCrashError("red", phase=phase, exit_code=1)

    def side_effect(argv: list[str], **_kwargs: object) -> object:
        if argv[:3] == ["git", "clean", "-fdX"]:
            for path in argv[4:]:
                (worktree / path).unlink(missing_ok=True)
        return _ok()

    runner = MagicMock(side_effect=side_effect)
    graph = orchestrator.PhaseGraph(
        start="review",
        phases={
            "review": orchestrator.Phase(name="review", prompt="run.md"),
        },
    )

    result = orchestrator._walk_run_graph(
        graph,
        runtime=FailingRuntime(),
        run=_run_context(
            fixture_dir=fixture_dir,
            worktree=worktree,
            runner=runner,
        ),
        context="Run context",
    )

    assert result.terminal is orchestrator.HUMAN
    assert not (worktree / orchestrator.RUN_REVIEW).exists()
    assert not (worktree / orchestrator.RUN_REPORT).exists()
    assert _calls_containing(
        runner,
        "git",
        "clean",
        "-fdX",
        orchestrator.RUN_REVIEW,
        orchestrator.RUN_REPORT,
    )


def _publish_with_runner(
    *, runner: MagicMock, fixture_dir: Path, worktree: Path
) -> orchestrator.PublicationOutcome:
    return orchestrator._open_pull_request(
        repo="owner/repo",
        base_branch="main",
        run=_run_context(
            fixture_dir=fixture_dir,
            worktree=worktree,
            runner=runner,
        ),
        completed=[86],
        selected=[IssueRef(number=86, title="Run phases")],
        skipped=[],
        gate=None,
        report=None,
        publication_error=None,
        successful=True,
        stopping_point=None,
    )


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, "[]"), (0, ""), (0, "{"), (0, "{}")],
)
def test_unknown_pr_lookup_never_creates_a_duplicate(
    fixture_dir: Path, tmp_path: Path, returncode: int, stdout: str
) -> None:
    runner = MagicMock(
        side_effect=[
            _ok(),
            subprocess.CompletedProcess([], returncode, stdout=stdout),
        ]
    )

    outcome = _publish_with_runner(
        runner=runner, fixture_dir=fixture_dir, worktree=tmp_path
    )

    assert outcome == orchestrator.PublicationOutcome(final_push_succeeded=True)
    assert not _calls_containing(runner, "gh", "pr", "create")


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, "[]"), (0, ""), (0, "{"), (0, "{}")],
)
def test_unknown_report_comment_lookup_never_posts_a_duplicate(
    fixture_dir: Path, tmp_path: Path, returncode: int, stdout: str
) -> None:
    runner = MagicMock(
        side_effect=[
            _ok(),
            subprocess.CompletedProcess([], 0, stdout='[{"number":86}]'),
            subprocess.CompletedProcess([], returncode, stdout=stdout),
        ]
    )

    outcome = _publish_with_runner(
        runner=runner, fixture_dir=fixture_dir, worktree=tmp_path
    )

    assert outcome == orchestrator.PublicationOutcome(
        pr_opened=True,
        final_push_succeeded=True,
    )
    calls = [call.args[0] for call in runner.call_args_list]
    assert not any("--method" in call for call in calls)


def test_before_run_human_stops_before_claim_or_pull_request(tmp_path: Path) -> None:
    fixture = _scoped_fixture(tmp_path, before=True)
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "before":
                (cwd / "tracked.txt").write_text("incomplete")
                (cwd / "untracked.txt").write_text("discard me")
                raise AgentCrashError("human", phase=phase, exit_code=1)
            return super().run(prompt, cwd=cwd, phase=phase)

    runner = _git_aware_runner()
    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="before-human",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    assert not outcome.succeeded
    assert outcome.selected == [1]
    assert outcome.issues == []
    assert outcome.stopping_point == "before-Run HUMAN"
    source.claim.assert_not_called()
    source.mark_for_human.assert_not_called()
    assert _calls_containing(runner, "git", "reset", "--hard", "HEAD")
    assert _calls_containing(runner, "git", "clean", "-fd")
    assert (fixture / "runs" / "before-human" / "run.log").is_file()
    assert not _calls_containing(runner, "git", "branch", "pycastle/issue-")
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_empty_supplied_batch_is_side_effect_free(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    source = MagicMock()
    runner = MagicMock()

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=(),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="empty",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    assert outcome.selected == []
    assert not (tmp_path / "wt").exists()
    assert not (fixture / "runs").exists()
    source.list_ready.assert_not_called()
    runner.assert_not_called()


def test_all_human_items_stop_before_second_run_setup_gate_or_publication(
    tmp_path: Path,
) -> None:
    fixture = _scoped_fixture(tmp_path, before=True, after=True)
    issues = [
        IssueRef(number=1, title="First", assignees=["krishna"]),
        IssueRef(number=2, title="Second", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    phases: list[tuple[str, str]] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            phases.append((phase, cwd.name))
            if phase == "before":
                (cwd / "preparation.txt").write_text("prepared")
                return super().run(prompt, cwd=cwd, phase=phase)
            if phase == "implement":
                raise AgentCrashError("human", phase=phase, exit_code=1)
            raise AssertionError(f"unexpected phase {phase}")

    setup_paths: list[Path] = []
    gate_paths: list[Path] = []
    runner = _git_aware_runner()
    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=issues[:2],
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="all-human",
        iterations=2,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        setup=setup_paths.append,
        gate_check=lambda cwd: (
            gate_paths.append(cwd) or orchestrator.GateOutcome(True, "green")
        ),
    )

    assert outcome.selected == [1, 2]
    assert outcome.completed == []
    assert outcome.skipped == [1, 2]
    assert not outcome.pr_opened
    assert phases == [
        ("before", "run-all-human"),
        ("implement", "issue-1"),
        ("implement", "issue-1"),
        ("implement", "issue-1"),
        ("implement", "issue-2"),
        ("implement", "issue-2"),
        ("implement", "issue-2"),
    ]
    assert [path.name for path in setup_paths] == [
        "run-all-human",
        "issue-1",
        "issue-2",
    ]
    assert gate_paths == []
    assert source.claim.call_count == 2
    assert source.mark_for_human.call_count == 2
    assert not _calls_containing(runner, "gh", "pr", "create")
    removed = [
        call.args[0][3]
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "worktree", "remove"]
    ]
    assert removed == [
        str(tmp_path / "wt" / "issue-1"),
        str(tmp_path / "wt" / "issue-2"),
        str(tmp_path / "wt" / "run-all-human"),
    ]


def test_after_run_human_runs_gate_and_keeps_pull_request_draft(
    tmp_path: Path,
) -> None:
    fixture = _scoped_fixture(tmp_path)
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, build_run, phase\n"
        "run = build_run("
        "item=build(start='implement', phases=[phase('implement', 'item.md')]), "
        "after=build(start='checkpoint', phases=["
        "phase('checkpoint', 'after.md', on_success='failing'), "
        "phase('failing', 'after.md')]))\n"
    )
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]

    visits: list[str] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "checkpoint":
                visits.append(phase)
                (cwd / "durable.txt").write_text("checkpoint")
            if phase == "failing":
                visits.append(phase)
                (cwd / "durable.txt").write_text("failing edit")
                (cwd / "untracked.txt").write_text("discard me")
                raise AgentCrashError("human", phase=phase, exit_code=1)
            return super().run(prompt, cwd=cwd, phase=phase)

    gate_paths: list[Path] = []

    def gate(cwd: Path) -> orchestrator.GateOutcome:
        gate_paths.append(cwd)
        if cwd.name.startswith("run-"):
            assert (cwd / "durable.txt").read_text() == "checkpoint"
            assert not (cwd / "untracked.txt").exists()
        return orchestrator.GateOutcome(True, "green")

    base_runner = _git_aware_runner()

    def side_effect(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "reset", "--hard"]:
            run_worktree = Path(str(kwargs["cwd"]))
            (run_worktree / "durable.txt").write_text("checkpoint")
        if argv[:3] == ["git", "clean", "-fd"]:
            (Path(str(kwargs["cwd"])) / "untracked.txt").unlink(missing_ok=True)
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=side_effect)
    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="after-human",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        gate_check=gate,
    )

    assert outcome.completed == [1]
    assert outcome.skipped == []
    assert outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert outcome.stopping_point == "after-Run HUMAN"
    assert gate_paths[-1].name == "run-after-human"
    assert visits == ["checkpoint", "failing"]


def test_red_run_gate_keeps_pull_request_draft(tmp_path: Path) -> None:
    fixture_dir = _scoped_fixture(tmp_path, after=True)
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"]),
        IssueRef(number=2, title="Second", assignees=["krishna"]),
    ]

    visits: list[str] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if cwd.name == "issue-2" and phase == "implement":
                raise AgentCrashError("skip", phase=phase, exit_code=1)
            if cwd.name.startswith("run-"):
                visits.append(phase)
            return super().run(prompt, cwd=cwd, phase=phase)

    gate_calls: list[Path] = []

    def gate(cwd: Path) -> orchestrator.GateOutcome:
        gate_calls.append(cwd)
        is_run = cwd.name.startswith("run-")
        return orchestrator.GateOutcome(
            not is_run,
            "raw stdout secret\nraw stderr secret" if is_run else "green",
            exit_code=23 if is_run else 0,
            duration_seconds=1.25 if is_run else 0,
            command="project-safe-gate",
        )

    bodies: list[str] = []
    base_runner = _git_aware_runner()

    def side_effect(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["gh", "pr", "comment"]:
            bodies.append(argv[argv.index("--body") + 1])
        return base_runner(argv, **kwargs)

    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="red-gate",
        iterations=2,
        impl_retries=0,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=MagicMock(side_effect=side_effect),
        gate_check=gate,
    )

    assert outcome.completed == [1]
    assert outcome.skipped == [2]
    assert outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert outcome.stopping_point == "Run Gate"
    assert visits == ["after"]
    assert sum(path.name.startswith("run-") for path in gate_calls) == 1
    source.mark_for_human.assert_called_once_with(2)
    source.release.assert_not_called()
    assert "Completed Items: #1" in bodies[0]
    assert "Skipped Items: #2" in bodies[0]
    assert "`project-safe-gate` — FAIL (exit 23, 1.25s)" in bodies[0]
    assert "raw stdout secret" not in bodies[0]
    assert "raw stderr secret" not in bodies[0]
    assert (fixture_dir / "runs" / "red-gate" / "run-gate.log").read_text() == (
        "raw stdout secret\nraw stderr secret"
    )


def test_second_run_setup_failure_discards_dirty_scope_and_keeps_draft(
    tmp_path: Path,
) -> None:
    fixture_dir = _scoped_fixture(tmp_path, before=True, after=True)
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]
    run_setup_count = 0
    gate_paths: list[Path] = []
    after_visits: list[str] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "before":
                (cwd / "tracked.txt").write_text("durable checkpoint\n")
            if phase == "after":
                after_visits.append(phase)
            return super().run(prompt, cwd=cwd, phase=phase)

    def setup(cwd: Path) -> None:
        nonlocal run_setup_count
        if cwd.name.startswith("run-"):
            run_setup_count += 1
            if run_setup_count == 2:
                assert (cwd / "tracked.txt").read_text() == "durable checkpoint\n"
                (cwd / "tracked.txt").write_text("incomplete setup\n")
                nested = cwd / "untracked" / "partial.txt"
                nested.parent.mkdir()
                nested.write_text("discard me\n")
                raise orchestrator.SetupError("broken")

    def gate(cwd: Path) -> orchestrator.GateOutcome:
        gate_paths.append(cwd)
        return orchestrator.GateOutcome(True, "green")

    base_runner = _git_aware_runner()

    def side_effect(argv: list[str], **kwargs: object) -> object:
        cwd = Path(str(kwargs.get("cwd", tmp_path)))
        if argv[:3] == ["git", "diff", "--cached"] and cwd.name == "run-setup-failure":
            return subprocess.CompletedProcess(argv, 1, stdout="")
        if argv[:3] == ["git", "reset", "--hard"]:
            (cwd / "tracked.txt").write_text("durable checkpoint\n")
        if argv[:3] == ["git", "clean", "-fd"]:
            partial = cwd / "untracked" / "partial.txt"
            partial.unlink(missing_ok=True)
            partial.parent.rmdir()
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=side_effect)
    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="setup-failure",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        gate_check=gate,
        setup=setup,
    )

    assert outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert outcome.stopping_point == "after-Run Setup"
    assert all(not path.name.startswith("run-") for path in gate_paths)
    assert after_visits == []
    assert _calls_containing(runner, "git", "reset", "--hard", "HEAD")
    assert _calls_containing(runner, "git", "clean", "-fd")
    assert _calls_containing(
        runner, "git", "commit", "-m", "chore: checkpoint Run phase before"
    )
    final_push = [
        call
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "push", "-u"]
    ][-1]
    assert final_push.kwargs["cwd"] == tmp_path / "wt" / "run-setup-failure"
    comment_call = next(
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "comment"]
    )
    comment = comment_call[comment_call.index("--body") + 1]
    assert "Completed Items: #1" in comment
    assert "Run Gate: not run" in comment
    assert "Stopping point: after-Run Setup" in comment


def test_second_run_setup_cleanup_failure_still_publishes_durable_work(
    tmp_path: Path,
) -> None:
    fixture_dir = _scoped_fixture(tmp_path)
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]
    run_setup_count = 0

    def setup(cwd: Path) -> None:
        nonlocal run_setup_count
        if cwd.name.startswith("run-"):
            run_setup_count += 1
            if run_setup_count == 2:
                report = cwd / orchestrator.RUN_REPORT
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("incomplete setup report")
                raise orchestrator.SetupError("broken")

    base_runner = _git_aware_runner()

    def side_effect(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["git", "reset", "--hard"]:
            return subprocess.CompletedProcess(argv, 1, stdout="restore failed")
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=side_effect)
    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="setup-cleanup-failure",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        setup=setup,
    )

    assert outcome.completed == [1]
    assert outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert outcome.stopping_point == "after-Run Setup"
    assert _calls_containing(
        runner, "git", "push", "origin", "pycastle/run-setup-cleanup-failure"
    )
    log = (fixture_dir / "runs" / "setup-cleanup-failure" / "run.log").read_text()
    assert "After-Run Setup failed: broken" in log
    assert "could not discard incomplete Run scope" in log
    publication_call = next(
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "comment"]
    )
    publication_body = publication_call[publication_call.index("--body") + 1]
    assert "incomplete setup report" not in publication_body


def test_handled_item_infrastructure_failure_stops_frozen_remainder(
    tmp_path: Path,
) -> None:
    fixture_dir = _scoped_fixture(tmp_path, after=True)
    issues = [
        IssueRef(number=n, title=f"Item {n}", assignees=["krishna"]) for n in (1, 2, 3)
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    after_visits: list[str] = []
    gate_paths: list[Path] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "after":
                after_visits.append(phase)
            return super().run(prompt, cwd=cwd, phase=phase)

    def setup(cwd: Path) -> None:
        if cwd.name == "issue-2":
            raise RuntimeError("offline")

    runner = _git_aware_runner()
    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="infra-failure",
        iterations=3,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        setup=setup,
        gate_check=lambda cwd: (
            gate_paths.append(cwd) or orchestrator.GateOutcome(True, "green")
        ),
    )

    assert outcome.completed == [1]
    assert outcome.skipped == [2, 3]
    assert outcome.stopping_point == "Item #2 infrastructure failure: offline"
    source.release.assert_called_once_with(2)
    assert [call.args[0] for call in source.claim.call_args_list] == [1, 2]
    source.mark_for_human.assert_not_called()
    assert outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert after_visits == []
    assert all(not path.name.startswith("run-") for path in gate_paths)

    comment_call = next(
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "comment"]
    )
    comment = comment_call[comment_call.index("--body") + 1]
    assert "Completed Items: #1" in comment
    assert "Skipped Items: #2, #3" in comment


def test_draft_creation_os_error_retains_pushed_branch_and_run_records(
    fixture_dir: Path, tmp_path: Path
) -> None:
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]
    base_runner = _git_aware_runner()

    def side_effect(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["gh", "pr", "create"]:
            raise OSError("GitHub unavailable")
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=side_effect)
    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="no-pr",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    assert outcome.completed == [1]
    assert not outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert outcome.stopping_point == "Pull request publication"
    assert _calls_containing(runner, "git", "push", "origin", "pycastle/run-no-pr")
    assert not _calls_containing(runner, "gh", "pr", "comment")
    assert not _calls_containing(runner, "gh", "pr", "ready")
    log = (fixture_dir / "runs" / "no-pr" / "run.log").read_text()
    assert "pushed branch origin/pycastle/run-no-pr" in log
    assert "retained records" in log


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xff", "Run report is not valid UTF-8."),
        (
            b"x" * (orchestrator.RUN_REPORT_LIMIT + 1),
            f"Run report exceeds the {orchestrator.RUN_REPORT_LIMIT}-byte publication limit.",
        ),
    ],
)
def test_invalid_run_report_is_visible_and_keeps_pull_request_draft(
    tmp_path: Path, raw: bytes, message: str
) -> None:
    fixture = _scoped_fixture(tmp_path, after=True)
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "after":
                report = cwd / orchestrator.RUN_REPORT
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_bytes(raw)
            return super().run(prompt, cwd=cwd, phase=phase)

    bodies: list[str] = []
    base_runner = _git_aware_runner()

    def side_effect(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["gh", "pr", "comment"]:
            bodies.append(argv[argv.index("--body") + 1])
        return base_runner(argv, **kwargs)

    outcome = orchestrator.run_batch(
        runtime=Runtime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="invalid-report",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=MagicMock(side_effect=side_effect),
    )

    assert outcome.stopping_point == "Run report validation"
    assert outcome.completed == [1]
    assert outcome.skipped == []
    assert outcome.pr_opened and not outcome.pr_ready and not outcome.succeeded
    assert message in bodies[0]
    assert "Completed Items: #1" in bodies[0]
    assert "Skipped Items: none" in bodies[0]
    assert (fixture / "runs" / "invalid-report" / "run-report.md").read_bytes() == raw


def test_interrupt_during_after_run_opens_no_pull_request(tmp_path: Path) -> None:
    fixture = _scoped_fixture(tmp_path, after=True)
    source = MagicMock()
    source.list_ready.return_value = [
        IssueRef(number=1, title="First", assignees=["krishna"])
    ]

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "after":
                raise KeyboardInterrupt
            return super().run(prompt, cwd=cwd, phase=phase)

    runner = _git_aware_runner()
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_batch(
            runtime=Runtime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="after-interrupt",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
        )

    assert not _calls_containing(runner, "gh", "pr", "create")
    assert not _calls_containing(
        runner, "git", "branch", "-D", "pycastle/run-after-interrupt"
    )


@pytest.mark.parametrize("size", [0, orchestrator.RUN_REPORT_LIMIT])
def test_harvest_report_accepts_boundary_sizes(tmp_path: Path, size: int) -> None:
    fixture = tmp_path / ".pycastle"
    worktree = tmp_path / "worktree"
    (worktree / ".pycastle").mkdir(parents=True)
    (worktree / orchestrator.RUN_REPORT).write_bytes(b"x" * size)

    report, error = orchestrator._harvest_report(
        _run_context(fixture_dir=fixture, worktree=worktree, runner=MagicMock())
    )

    assert report == "x" * size
    assert error is None


def test_harvest_report_accepts_missing_optional_report(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    report, error = orchestrator._harvest_report(
        _run_context(fixture_dir=fixture, worktree=worktree, runner=MagicMock())
    )

    assert report is None
    assert error is None
    assert not (fixture / "runs" / "run-86" / "run-report.md").exists()


def test_harvest_report_rejects_one_byte_over_limit_without_truncating(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / ".pycastle"
    worktree = tmp_path / "worktree"
    (worktree / ".pycastle").mkdir(parents=True)
    raw = b"x" * (orchestrator.RUN_REPORT_LIMIT + 1)
    (worktree / orchestrator.RUN_REPORT).write_bytes(raw)

    report, error = orchestrator._harvest_report(
        _run_context(fixture_dir=fixture, worktree=worktree, runner=MagicMock())
    )

    assert report is None
    assert error == (
        f"Run report exceeds the {orchestrator.RUN_REPORT_LIMIT}-byte "
        "publication limit."
    )
    assert (fixture / "runs" / "run-86" / "run-report.md").read_bytes() == raw


@pytest.mark.parametrize("kind", ["directory", "symlink", "broken-symlink"])
def test_harvest_report_rejects_non_regular_files(tmp_path: Path, kind: str) -> None:
    fixture = tmp_path / ".pycastle"
    worktree = tmp_path / "worktree"
    report_path = worktree / orchestrator.RUN_REPORT
    report_path.parent.mkdir(parents=True)
    if kind == "directory":
        report_path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "outside.md"
        target.write_text("must not be published")
        report_path.symlink_to(target)
    else:
        report_path.symlink_to(tmp_path / "missing.md")

    report, error = orchestrator._harvest_report(
        _run_context(fixture_dir=fixture, worktree=worktree, runner=MagicMock())
    )

    assert report is None
    assert error == "Run report must be a regular file."
    assert not (fixture / "runs" / "run-86" / "run-report.md").exists()


def test_ready_transition_failure_keeps_publication_success_distinct(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=86, title="Integrated review", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    base_runner = _git_aware_runner()

    def fail_ready(argv: list[str], **kwargs: object) -> object:
        if argv[:3] == ["gh", "pr", "ready"]:
            return subprocess.CompletedProcess(argv, 1, stdout="not ready")
        return base_runner(argv, **kwargs)

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="run-86",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=MagicMock(side_effect=fail_ready),
    )

    assert outcome.pr_opened is True
    assert outcome.pr_ready is False
    assert outcome.succeeded is False
    assert outcome.stopping_point == "Pull request ready transition"


def test_transcript_sink_interleaves_tagged_lines(
    fixture_dir: Path,
) -> None:
    # The per-issue sink writes each chunk, tagged with its stream and prefixed
    # with its phase, to one .pycastle/runs/<run_id>/issue-<n>-transcript.log, so
    # THINKING and OUTPUT interleave in chronological (append) order in one file.
    sink = orchestrator._transcript_sink(fixture_dir, "20260613-101500", 7)
    sink("implement", "THINKING", "first thought")
    sink("implement", "OUTPUT", "did the thing")

    path = fixture_dir / "runs" / "20260613-101500" / "issue-7-transcript.log"
    assert path.is_file()
    contents = path.read_text()
    assert "[implement] [THINKING] first thought" in contents
    assert "[implement] [OUTPUT] did the thing" in contents
    assert contents.index("[THINKING] first thought") < contents.index(
        "[OUTPUT] did the thing"
    )


def test_run_transcript_sink_interleaves_scoped_phase_lines(
    fixture_dir: Path,
) -> None:
    before = orchestrator._run_transcript_sink(
        fixture_dir, "20260715-090949", "before-Run"
    )
    after = orchestrator._run_transcript_sink(
        fixture_dir, "20260715-090949", "after-Run"
    )

    before("review", "THINKING", "inspect the batch")
    after("review", "OUTPUT", "two integrated findings")

    path = fixture_dir / "runs" / "20260715-090949" / "run-phase-transcript.log"
    assert path.read_text().splitlines() == [
        "[before-Run] [review] [THINKING] inspect the batch",
        "[after-Run] [review] [OUTPUT] two integrated findings",
    ]


def test_run_phase_telemetry_appends_scoped_records(fixture_dir: Path) -> None:
    before = orchestrator.PhaseResult(
        phase="review",
        result=RuntimeResult(
            output="before",
            telemetry=Telemetry(runtime="stub", phase="review", num_turns=1),
        ),
    )
    after = orchestrator.PhaseResult(
        phase="review",
        result=RuntimeResult(
            output="after",
            telemetry=Telemetry(runtime="stub", phase="review", num_turns=2),
        ),
    )

    orchestrator._append_run_telemetry(
        fixture_dir, "20260715-090949", "before-Run", [before]
    )
    orchestrator._append_run_telemetry(
        fixture_dir, "20260715-090949", "after-Run", [after]
    )

    records = json.loads(
        (
            fixture_dir / "runs" / "20260715-090949" / "run-phase-telemetry.json"
        ).read_text()
    )
    assert [(record["scope"], record["phase"]) for record in records] == [
        ("before-Run", "review"),
        ("after-Run", "review"),
    ]
    assert [record["num_turns"] for record in records] == [1, 2]


def test_verbose_run_binds_a_per_issue_transcript_sink(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A verbose run binds the runtime's transcript_sink before working each issue
    # so the runtime can persist its transcript to that issue's log without
    # knowing run_id.
    issue = IssueRef(number=2, title="Walking skeleton", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()
    runtime = StubRuntime()

    orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        verbose=True,
    )

    # The runtime now carries a sink bound to issue #2's transcript log.
    assert runtime.transcript_sink is not None
    runtime.transcript_sink("implement", "OUTPUT", "bound output")
    path = fixture_dir / "runs" / "20260613-101500" / "issue-2-transcript.log"
    assert path.is_file()
    assert "bound output" in path.read_text()


def test_non_verbose_run_does_not_bind_a_transcript_sink(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # Without verbose, no sink is bound, so the runtime's transcript_sink stays
    # None and no transcript log is written — behaviour unchanged.
    issue = IssueRef(number=2, title="Walking skeleton", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()
    runtime = StubRuntime()

    orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        selected=source.list_ready(),
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

    assert getattr(runtime, "transcript_sink", None) is None
    assert not (
        fixture_dir / "runs" / "20260613-101500" / "issue-2-transcript.log"
    ).is_file()


def test_verbose_run_scopes_before_item_and_after_transcripts(
    tmp_path: Path,
) -> None:
    fixture = _scoped_fixture(tmp_path, before=True, after=True)
    issue = IssueRef(number=2, title="Scoped transcripts", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]

    class SinkAwareRuntime(StubRuntime):
        transcript_sink = None

        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            assert self.transcript_sink is not None
            self.transcript_sink(phase, "THINKING", f"thinking-{phase}")
            self.transcript_sink(phase, "OUTPUT", f"output-{phase}")
            return super().run(prompt, cwd=cwd, phase=phase)

    orchestrator.run_batch(
        runtime=SinkAwareRuntime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="scoped",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=_git_aware_runner(),
        verbose=True,
    )

    run_dir = fixture / "runs" / "scoped"
    run_transcript = (run_dir / "run-phase-transcript.log").read_text()
    item_transcript = (run_dir / "issue-2-transcript.log").read_text()
    assert "[before-Run] [before] [THINKING] thinking-before" in run_transcript
    assert "[after-Run] [after] [OUTPUT] output-after" in run_transcript
    assert "output-implement" in item_transcript
    assert "output-after" not in item_transcript


def test_failed_after_run_checkpoint_retains_transcript_and_git_diagnostics(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fixture = _scoped_fixture(tmp_path, after=True)
    issue = IssueRef(number=2, title="Checkpoint evidence", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    run_worktree = tmp_path / "wt" / "run-checkpoint-failure"
    after_finished = False

    class Runtime(StubRuntime):
        transcript_sink = None

        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            nonlocal after_finished
            if self.transcript_sink is not None:
                self.transcript_sink(phase, "THINKING", f"thinking-{phase}")
                self.transcript_sink(phase, "OUTPUT", f"output-{phase}")
            result = super().run(prompt, cwd=cwd, phase=phase)
            if phase == "after":
                review = cwd / orchestrator.RUN_REVIEW
                review.parent.mkdir(parents=True, exist_ok=True)
                review.write_text("two findings")
                after_finished = True
            return result

    base_runner = _git_aware_runner()

    def fail_after_add(argv: list[str], **kwargs: object) -> object:
        if (
            after_finished
            and argv[:2] == ["git", "add"]
            and kwargs.get("cwd") == run_worktree
        ):
            return subprocess.CompletedProcess(
                argv,
                23,
                stdout="index stdout detail",
                stderr="fatal: distinctive add error",
            )
        if argv[:3] == ["git", "worktree", "remove"] and argv[3] == str(run_worktree):
            (run_worktree / orchestrator.RUN_REVIEW).unlink(missing_ok=True)
        return base_runner(argv, **kwargs)

    with caplog.at_level("ERROR"):
        outcome = orchestrator.run_batch(
            runtime=Runtime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="checkpoint-failure",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=MagicMock(side_effect=fail_after_add),
            verbose=True,
        )

    assert outcome.stopping_point is not None
    assert outcome.stopping_point.startswith("after-Run checkpoint:")
    assert not (run_worktree / orchestrator.RUN_REVIEW).exists()
    run_dir = fixture / "runs" / "checkpoint-failure"
    transcript = (run_dir / "run-phase-transcript.log").read_text()
    assert "[after-Run] [after] [OUTPUT] output-after" in transcript
    assert 'argv: ["git", "add", "-A"' in transcript
    assert "exit code: 23" in transcript
    assert "stdout: 'index stdout detail'" in transcript
    assert "stderr: 'fatal: distinctive add error'" in transcript
    assert "output-after" not in (run_dir / "issue-2-transcript.log").read_text()
    assert "fatal: distinctive add error" in caplog.text


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
        selected=issues[:2],
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
        selected=source.list_ready(),
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

    # Staging excludes both historical planning paths from #68 even if a Runtime
    # ignores the canonical .pycastle/plan.md destination in the plan prompt.
    add_argv = next(
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "add", "-A"]
    )
    assert ":(exclude,top)PLAN.md" in add_argv
    assert ":(exclude,top,glob).pycastle/plan-issue-*.md" in add_argv

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
        selected=source.list_ready(),
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


def test_each_successful_issue_merge_checkpoints_the_run_branch(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issues = [
        IssueRef(number=2, title="First", assignees=["krishna"]),
        IssueRef(number=4, title="Second", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner()

    orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
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

    events = [call.args[0] for call in runner.call_args_list]
    merges = [i for i, argv in enumerate(events) if argv[:2] == ["git", "merge"]]
    pushes = [i for i, argv in enumerate(events) if argv[:2] == ["git", "push"]]
    assert len(pushes) == 3  # two durability checkpoints plus finalization
    assert merges[0] < pushes[0] < merges[1] < pushes[1] < pushes[2]


def test_incremental_push_failure_is_logged_and_later_checkpoint_retries(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issues = [
        IssueRef(number=2, title="First", assignees=["krishna"]),
        IssueRef(number=4, title="Second", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    base_runner = _git_aware_runner()
    push_count = 0

    def side_effect(argv: list[str], **kwargs: object) -> object:
        nonlocal push_count
        if argv[:2] == ["git", "push"]:
            push_count += 1
            if push_count == 1:
                return subprocess.CompletedProcess(argv, 1, stdout="offline")
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=side_effect)
    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
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

    assert outcome.completed == [2, 4]
    assert outcome.pr_opened is True
    assert outcome.pr_ready is True
    assert outcome.succeeded is True
    assert push_count == 3
    pushes = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:2] == ["git", "push"]
    ]
    assert pushes == [
        ["git", "push", "-u", "origin", outcome.run_branch],
        ["git", "push", "-u", "origin", outcome.run_branch],
        ["git", "push", "-u", "origin", outcome.run_branch],
    ]
    assert (
        "Durability push failed"
        in (fixture_dir / "runs" / "20260613-101500" / "run.log").read_text()
    )


def test_incremental_push_os_error_is_logged_without_aborting_run(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="First", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    base_runner = _git_aware_runner()
    push_count = 0

    def side_effect(argv: list[str], **kwargs: object) -> object:
        nonlocal push_count
        if argv[:2] == ["git", "push"]:
            push_count += 1
            if push_count == 1:
                raise OSError("origin is unreachable")
        return base_runner(argv, **kwargs)

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=MagicMock(side_effect=side_effect),
    )

    assert outcome.completed == [2]
    assert outcome.pr_opened is True
    assert push_count == 2
    assert (
        "Durability push failed"
        in (fixture_dir / "runs" / "20260613-101500" / "run.log").read_text()
    )


@pytest.mark.parametrize("gate_passed", [True, False])
@pytest.mark.parametrize("failure_kind", ["nonzero", "os-error"])
def test_failed_final_push_prevents_ready_or_draft_pull_request_creation(
    fixture_dir: Path, tmp_path: Path, gate_passed: bool, failure_kind: str
) -> None:
    issue = IssueRef(number=2, title="First", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    base_runner = _git_aware_runner()
    push_count = 0

    def side_effect(argv: list[str], **kwargs: object) -> object:
        nonlocal push_count
        if argv[:2] == ["git", "push"]:
            push_count += 1
            if push_count == 2:
                if failure_kind == "os-error":
                    raise OSError("origin is unreachable")
                return subprocess.CompletedProcess(argv, 1, stdout="offline")
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=side_effect)

    def check_gate(cwd: Path) -> orchestrator.GateOutcome:
        passed = True if cwd.name.startswith("issue-") else gate_passed
        return orchestrator.GateOutcome(
            passed,
            "gate output",
            exit_code=0 if passed else 1,
            duration_seconds=0.1,
            command=".pycastle/gate",
        )

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        gate_check=check_gate,
    )

    assert outcome.pr_opened is False
    assert outcome.pr_ready is False
    assert outcome.succeeded is False
    assert outcome.stopping_point == "Final push"
    calls = [call.args[0] for call in runner.call_args_list]
    push_indexes = [i for i, call in enumerate(calls) if call[:2] == ["git", "push"]]
    assert len(push_indexes) == 2
    assert push_indexes[0] < push_indexes[1]
    assert not any(call[:2] == ["gh", "pr"] for call in calls)
    assert not _calls_containing(runner, "git", "branch", "-D", outcome.run_branch)
    assert (fixture_dir / "runs" / "20260613-101500" / "run.log").exists()
    assert (
        "Final push failed"
        in (fixture_dir / "runs" / "20260613-101500" / "run.log").read_text()
    )


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
        selected=source.list_ready(),
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
    pushes = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:2] == ["git", "push"]
    ]
    assert pushes == [
        ["git", "push", "-u", "origin", outcome.run_branch],
        ["git", "push", "-u", "origin", outcome.run_branch],
        ["git", "push", "-u", "origin", outcome.run_branch],
    ]

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
        selected=source.list_ready(),
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


def test_empty_diff_routes_to_human_no_phantom_success(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A walk that reaches DONE but leaves the issue branch identical to the run
    # branch (the runtime silently no-opped, e.g. codex stuck read-only #35) must
    # not report a phantom success: the issue is handed to a human, recorded as
    # not merged, kept out of `completed`, and no PR is opened.
    issues = [
        IssueRef(number=2, title="No change", assignees=["krishna"]),
        IssueRef(number=4, title="Real change", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runner = _git_aware_runner(empty_diff_for={2})

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
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

    # The empty-diff issue is handed to a human and is not counted completed; the
    # real-change issue still merges, so one no-op item does not sink the batch.
    source.mark_for_human.assert_called_once_with(2)
    merged = {o.issue.number: o.merged for o in outcome.issues}
    assert merged == {2: False, 4: True}
    assert outcome.completed == [4]

    # The no-change branch is never merged: no `git merge` of issue-2 ran.
    assert not _calls_containing(runner, "git", "merge", "pycastle/issue-2-no-change")

    # A PR opens for the real change only; the empty-diff issue is absent from it.
    pr_calls = [
        call.args[0]
        for call in runner.call_args_list
        if call.args[0][:3] == ["gh", "pr", "create"]
    ]
    body = pr_calls[0][pr_calls[0].index("--body") + 1]
    assert "- Closes #4" in body
    assert "#2" not in body


def test_empty_diff_on_only_issue_opens_no_pull_request(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # When the only issue produces no change, the run opens no PR at all (avoiding
    # the `gh pr create` failure "No commits between main and <run-branch>") and
    # the issue is left ready-for-human rather than claimed-but-open (#35).
    issue = IssueRef(number=2, title="No change", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner(empty_diff_for={2})

    outcome = orchestrator.run_batch(
        runtime=StubRuntime(),
        issue_source=source,
        selected=source.list_ready(),
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

    assert outcome.completed == []
    assert outcome.pr_opened is False
    source.mark_for_human.assert_called_once_with(2)
    # No pull request is even attempted.
    assert not _calls_containing(runner, "gh", "pr", "create")


def _worktree_failing_runner(
    *, fail_run: bool = False, fail_issues: set[int] | None = None
) -> MagicMock:
    """A git-aware runner whose ``git worktree add`` fails selectively (#64).

    The run worktree add fails when ``fail_run`` is set; an issue worktree add
    fails when its issue number is in ``fail_issues``. A failing add returns
    git's real shape — a non-zero exit with stderr — and creates no directory;
    every other add creates its directory like :func:`_git_aware_runner`.
    """
    fail_issues = fail_issues or set()

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            name = Path(argv[3]).name
            failing = (name.startswith("run-") and fail_run) or (
                name.startswith("issue-") and int(name.split("-")[1]) in fail_issues
            )
            if failing:
                return subprocess.CompletedProcess(
                    args=argv, returncode=128, stdout="", stderr="fatal: boom"
                )
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        return _ok()

    return MagicMock(side_effect=side_effect)


def test_run_worktree_add_failure_raises_and_works_no_issue(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A failed run-worktree add is fatal: no issue can be worked without it, so
    # the run raises instead of silently driving the batch against a directory
    # that was never created (#64). Nothing is claimed and no PR is attempted.
    issue = IssueRef(number=2, title="Slice", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _worktree_failing_runner(fail_run=True)

    with pytest.raises(orchestrator.WorktreeError):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
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

    source.claim.assert_not_called()
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_run_branch_failure_aborts_before_worktree_add(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A stale run branch cannot be silently reused as the worktree base (#64)."""
    issue = IssueRef(number=2, title="Slice", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]

    def fail_run_branch(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "branch", "pycastle/run-20260613-101500"]:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=128,
                stdout="",
                stderr="fatal: a branch named 'pycastle/run-20260613-101500' exists",
            )
        return _ok()

    runner = MagicMock(side_effect=fail_run_branch)

    with pytest.raises(orchestrator.BranchError, match="branch named"):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture_dir,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="20260613-101500",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
        )

    assert not _calls_containing(runner, "git", "worktree", "add")
    source.claim.assert_not_called()


def test_issue_branch_failure_releases_issue_before_worktree_add(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A stale issue branch cannot be checked out from the wrong base (#64)."""
    issue = IssueRef(number=2, title="Slice", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]

    def fail_issue_branch(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "branch", "pycastle/issue-2-slice"]:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=128,
                stdout="",
                stderr="fatal: a branch named 'pycastle/issue-2-slice' exists",
            )
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
        return _ok()

    runner = MagicMock(side_effect=fail_issue_branch)

    with pytest.raises(orchestrator.BranchError, match="branch named"):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture_dir,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="20260613-101500",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
        )

    issue_add = str(tmp_path / "wt" / "issue-2")
    assert not _calls_containing(runner, "git", "worktree", "add", issue_add)
    source.release.assert_called_once_with(2)


def test_create_branch_success_issues_captured_git_command(tmp_path: Path) -> None:
    runner = MagicMock(return_value=_ok())

    orchestrator.create_branch("topic", "main", runner=runner, cwd=tmp_path)

    runner.assert_called_once_with(
        ["git", "branch", "topic", "main"], capture=True, cwd=tmp_path
    )


def test_issue_worktree_add_failure_releases_issue_and_aborts_run(
    fixture_dir: Path, tmp_path: Path
) -> None:
    # A failed issue-worktree add is an infra fault, not an issue-content fault:
    # rather than drive the Runtime against a directory that was never created (or
    # mislabel the issue ready-for-human), it is raised so run_batch's interrupt
    # teardown releases the claimed issue back to ready-for-agent and aborts (#64).
    issue = IssueRef(number=2, title="Slice", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _worktree_failing_runner(fail_issues={2})

    with pytest.raises(orchestrator.WorktreeError):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
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

    # The claimed issue is released back to ready-for-agent, never mislabelled
    # ready-for-human, and no PR is attempted for the aborted run.
    source.claim.assert_called_once_with(2, assignee="krishna")
    source.release.assert_called_once_with(2)
    source.mark_for_human.assert_not_called()
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_add_worktree_success_issues_the_add_and_does_not_raise(
    tmp_path: Path,
) -> None:
    # The happy path: a zero exit means git created the worktree, so the helper
    # returns quietly having issued exactly the captured add at the given cwd (#64).
    runner = MagicMock(return_value=_ok())
    worktree = tmp_path / "wt" / "issue-7"

    orchestrator.add_worktree(worktree, "br", runner=runner, cwd=tmp_path)

    runner.assert_called_once_with(
        ["git", "worktree", "add", str(worktree), "br"],
        capture=True,
        cwd=tmp_path,
    )


def test_add_worktree_failure_surfaces_git_stderr(tmp_path: Path) -> None:
    # A non-zero exit means no worktree was created; the helper raises and carries
    # git's stderr so the operator sees why the add failed rather than a downstream
    # ghost error (#64).
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal: '/x' already exists"
        )
    )

    with pytest.raises(orchestrator.WorktreeError) as excinfo:
        orchestrator.add_worktree(tmp_path / "x", "br", runner=runner, cwd=tmp_path)

    assert "fatal: '/x' already exists" in str(excinfo.value)
    assert "br" in str(excinfo.value)


def test_add_worktree_failure_falls_back_to_stdout_when_stderr_empty(
    tmp_path: Path,
) -> None:
    # git usually reports on stderr, but a failure that only wrote stdout must not
    # be dropped: the helper falls back to stdout for the raised detail (#64).
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="could not create work tree dir", stderr=""
        )
    )

    with pytest.raises(orchestrator.WorktreeError) as excinfo:
        orchestrator.add_worktree(tmp_path / "x", "br", runner=runner, cwd=tmp_path)

    assert "could not create work tree dir" in str(excinfo.value)


def test_add_worktree_failure_with_no_output_has_no_dangling_detail(
    tmp_path: Path,
) -> None:
    # A silent failure (no stderr/stdout, only a non-zero exit) still raises, and
    # the message stays clean — no trailing ": " with nothing after it (#64).
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr=""
        )
    )

    with pytest.raises(orchestrator.WorktreeError) as excinfo:
        orchestrator.add_worktree(
            tmp_path / "issue-9", "br", runner=runner, cwd=tmp_path
        )

    message = str(excinfo.value)
    assert message == f"git worktree add failed for br at {tmp_path / 'issue-9'}"


def test_add_worktree_failure_tolerates_none_captured_streams(tmp_path: Path) -> None:
    # A runner that leaves stderr/stdout as ``None`` (an uncaptured CompletedProcess
    # shape) must not blow up on ``.strip()``; the helper still raises cleanly (#64).
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout=None, stderr=None
        )
    )

    with pytest.raises(orchestrator.WorktreeError) as excinfo:
        orchestrator.add_worktree(tmp_path / "x", "br", runner=runner, cwd=tmp_path)

    assert str(excinfo.value) == f"git worktree add failed for br at {tmp_path / 'x'}"


def test_add_worktree_missing_returncode_is_treated_as_failure(
    tmp_path: Path,
) -> None:
    # Defensive default: a runner result without a ``returncode`` (an odd/mocked
    # shape) is treated as a failure rather than a silent success, so a worktree is
    # never assumed created on ambiguous output (#64).
    runner = MagicMock(return_value=object())

    with pytest.raises(orchestrator.WorktreeError):
        orchestrator.add_worktree(tmp_path / "x", "br", runner=runner, cwd=tmp_path)


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
        selected=source.list_ready(),
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

    setup = MagicMock()
    gate = MagicMock()
    runtime = MagicMock(spec=StubRuntime)
    outcome = orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=3,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        setup=setup,
        gate_check=gate,
    )

    assert outcome.selected == []
    assert outcome.issues == []
    assert outcome.completed == []
    assert outcome.pr_opened is False
    assert outcome.succeeded is True
    source.claim.assert_not_called()
    setup.assert_not_called()
    gate.assert_not_called()
    runtime.run.assert_not_called()
    # No branches, worktrees, or PRs when there is nothing to do.
    assert not _calls_containing(runner, "git", "branch")
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
        selected=source.list_ready(),
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
        selected=source.list_ready(),
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
        elif argv[:3] == ["git", "diff", "--quiet"]:
            # A non-empty diff (exit 1): the phases wrote real changes.
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
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
        selected=source.list_ready(),
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
        selected=source.list_ready(),
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
        selected=source.list_ready(),
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
# A non-implement phase crash takes its failure edge to HUMAN, end to end (#10).#
# --------------------------------------------------------------------------- #


class _PhaseCrashingRuntime:
    """A fake Runtime that crashes on a given phase for one issue's worktree.

    Stands in for an agent that dies mid-phase on a single issue: when the
    ``crash_phase`` runs inside a worktree path containing ``crash_in`` it raises
    :class:`AgentCrashError` (so the walker takes that phase's ``on_failure``
    edge); every other call runs clean and records its phase. This keeps the
    crash scoped to one issue while a sibling issue runs to completion on the
    same shared runtime. No real subprocess runs.
    """

    name = "stub"

    def __init__(self, *, crash_phase: str, crash_in: str) -> None:
        self.crash_phase = crash_phase
        self.crash_in = crash_in
        self.calls: list[str] = []

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        self.calls.append(phase)
        if phase == self.crash_phase and self.crash_in in str(cwd):
            raise AgentCrashError("boom", phase=phase, exit_code=1)
        (cwd / STUB_MARKER).write_text(f"phase {phase}\n")
        return RuntimeResult(
            output=f"ran {phase}",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


def test_plan_crash_routes_to_human_and_run_continues(
    three_phase_fixture_dir: Path, tmp_path: Path
) -> None:
    """A crash on a non-retried phase takes its failure edge to HUMAN (#10).

    On the default plan → implement → review graph every failure edge is HUMAN.
    A crash in ``plan`` (which runs once, not under #8's retry) must take that
    failure edge straight to the HUMAN terminal: the issue is marked
    ready-for-human, never reaches implement or review, is not committed or
    merged, and the run carries on to the next issue. This pins the failure-edge
    application through the orchestrator, not just the bare walker.
    """
    issues = [
        IssueRef(number=2, title="Plan crashes", assignees=["krishna"]),
        IssueRef(number=4, title="Clean one", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    # #2's plan crashes; #4 is fully clean. The crash is scoped to issue-2's
    # worktree, so the shared runtime crashes only #2's plan and #4 runs clean.
    runtime = _PhaseCrashingRuntime(crash_phase="plan", crash_in="issue-2")
    runner = _git_aware_runner()

    outcome = orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=three_phase_fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=5,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # #2's plan crashed -> HUMAN terminal -> marked ready-for-human, not merged.
    source.mark_for_human.assert_called_once_with(2)
    merged = {o.issue.number: o.merged for o in outcome.issues}
    assert merged == {2: False, 4: True}
    assert outcome.completed == [4]
    # The crashed walk never advanced past plan: implement and review never ran
    # for #2, and that issue branch was not committed or merged.
    assert runtime.calls[0] == "plan"
    assert not _calls_containing(
        runner, "git", "merge", "pycastle/issue-2-plan-crashes"
    )
    # The whole #4 cycle still ran after #2 was handed off — one stuck phase does
    # not sink the batch.
    assert runtime.calls[-3:] == ["plan", "implement", "review"]


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


def test_cancellation_during_before_run_setup_retains_records_without_remote_state(
    tmp_path: Path,
) -> None:
    fixture = _scoped_fixture(tmp_path)
    issue = IssueRef(number=2, title="Interrupted", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="before-setup-cancel",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
            setup=lambda _worktree: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    source.claim.assert_not_called()
    source.release.assert_not_called()
    records = fixture / "runs" / "before-setup-cancel"
    assert records.is_dir()
    log = (records / "run.log").read_text()
    assert "No remote checkpoint survived" in log
    assert f"Retained records: {records}" in log
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_cancellation_during_after_run_preserves_completed_item_checkpoint(
    tmp_path: Path,
) -> None:
    fixture = _scoped_fixture(tmp_path, after=True)
    issue = IssueRef(number=2, title="Completed", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
            if phase == "after":
                raise KeyboardInterrupt
            return super().run(prompt, cwd=cwd, phase=phase)

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_batch(
            runtime=Runtime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="after-cancel",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
        )

    source.claim.assert_called_once_with(2, assignee="krishna")
    source.release.assert_not_called()
    source.mark_for_human.assert_not_called()
    log = (fixture / "runs" / "after-cancel" / "run.log").read_text()
    assert "Remote checkpoint: origin/pycastle/run-after-cancel" in log
    assert not _calls_containing(runner, "gh", "pr", "create")
    assert not _calls_containing(runner, "gh", "pr", "ready")


def test_cancellation_as_claim_returns_releases_only_that_item(tmp_path: Path) -> None:
    fixture = _scoped_fixture(tmp_path)
    issues = [
        IssueRef(number=2, title="Claimed", assignees=["krishna"]),
        IssueRef(number=4, title="Untouched", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    source.claim.side_effect = KeyboardInterrupt
    runner = _git_aware_runner()

    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="claim-cancel",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
        )

    source.claim.assert_called_once_with(2, assignee="krishna")
    source.release.assert_called_once_with(2)
    source.mark_for_human.assert_not_called()
    assert not _calls_containing(runner, "gh", "pr", "create")


def test_interrupt_after_item_settles_does_not_release_completed_item(
    tmp_path: Path,
) -> None:
    fixture = _scoped_fixture(tmp_path)
    issue = IssueRef(number=2, title="Completed", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    base_runner = _git_aware_runner()

    def interrupt_on_item_cleanup(argv: list[str], **kwargs: object) -> object:
        if argv[:4] == ["git", "worktree", "remove", str(tmp_path / "wt" / "issue-2")]:
            raise KeyboardInterrupt
        return base_runner(argv, **kwargs)

    runner = MagicMock(side_effect=interrupt_on_item_cleanup)
    with pytest.raises(KeyboardInterrupt):
        orchestrator.run_batch(
            runtime=StubRuntime(),
            issue_source=source,
            selected=source.list_ready(),
            fixture_dir=fixture,
            repo="owner/repo",
            base_branch="main",
            assignee="krishna",
            run_id="settled-boundary",
            workspace=tmp_path,
            worktree_root=tmp_path / "wt",
            runner=runner,
        )

    source.release.assert_not_called()
    assert _calls_containing(
        runner, "git", "push", "origin", "pycastle/run-settled-boundary"
    )
    assert not _calls_containing(runner, "gh", "pr", "create")


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
            selected=source.list_ready(),
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
            selected=source.list_ready(),
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
            selected=source.list_ready(),
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
    log = (fixture_dir / "runs" / "20260613-101500" / "run.log").read_text()
    assert f"Manual worktree cleanup required: {tmp_path / 'wt' / 'issue-2'}" in log
    assert str(tmp_path / "wt" / "run-20260613-101500") in log


def test_cancellation_cleanup_reports_nonzero_results_and_release_failure(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Interrupted", assignees=["krishna"])
    source = MagicMock()
    source.release.side_effect = RuntimeError("GitHub unavailable")
    runner = MagicMock(return_value=MagicMock(returncode=1))
    run = orchestrator.RunContext(
        run_id="cleanup-failure",
        branch="pycastle/run-cleanup-failure",
        worktree=tmp_path / "wt" / "run-cleanup-failure",
        fixture_dir=fixture_dir,
        runner=runner,
    )

    orchestrator._cleanup_cancelled(
        issue=issue,
        issue_source=source,
        worktree_root=tmp_path / "wt",
        workspace=tmp_path,
        run=run,
    )

    source.release.assert_called_once_with(2)
    assert runner.call_count == 4
    log = (fixture_dir / "runs" / "cleanup-failure" / "run.log").read_text()
    assert str(tmp_path / "wt" / "issue-2") in log
    assert str(run.worktree) in log
    assert "In-flight Item #2 could not be released" in log


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
        selected=source.list_ready(),
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


# --------------------------------------------------------------------------- #
# render_issue_context: the preamble handed to the runtime each phase.        #
# --------------------------------------------------------------------------- #


def test_render_issue_context_header_carries_number_and_title_verbatim() -> None:
    # The header names the issue by number and keeps the title's punctuation and
    # markdown intact (unlike slugify), so the runtime sees the real title.
    issue = IssueRef(number=65, title="Hand the agent its `issue` context!")

    rendered = orchestrator.render_issue_context(issue)

    assert rendered.startswith("# Issue #65: Hand the agent its `issue` context!")


def test_render_issue_context_includes_the_body_after_the_header() -> None:
    issue = IssueRef(number=7, title="Do the thing", body="## What to build\nA gizmo.")

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == "# Issue #7: Do the thing\n\n## What to build\nA gizmo."


def test_render_issue_context_preserves_a_multiline_body_verbatim() -> None:
    body = "Line one\n\n- bullet\n- bullet two\n"
    issue = IssueRef(number=3, title="Multi", body=body)

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == f"# Issue #3: Multi\n\n{body.strip()}"


def test_render_issue_context_appends_author_attributed_comments() -> None:
    issue = IssueRef(
        number=87,
        title="Include comments",
        body="## What to build\nUse the discussion.",
        comments=[
            IssueComment(author="alice", body="First line\n\n- detail"),
            IssueComment(author="bob", body="Second clarification"),
        ],
    )

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == (
        "# Issue #87: Include comments\n\n"
        "## What to build\nUse the discussion.\n\n"
        "## Issue Comments\n\n"
        "### @alice\n\nFirst line\n\n- detail\n\n"
        "### @bob\n\nSecond clarification"
    )


def test_render_issue_context_with_no_body_is_header_only() -> None:
    # An empty body yields the header alone, with no dangling blank block.
    issue = IssueRef(number=9, title="No body")

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == "# Issue #9: No body"


def test_render_issue_context_treats_a_whitespace_only_body_as_empty() -> None:
    issue = IssueRef(number=9, title="Blank body", body="   \n\t \n")

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == "# Issue #9: Blank body"


def test_render_issue_context_empty_title_leaves_no_dangling_colon_space() -> None:
    # An issue source defaults an absent title to "" (``item.get("title", "")``),
    # so an empty title is reachable. The header ``rstrip`` keeps it clean: a bare
    # ``# Issue #5:`` with no trailing space, not ``# Issue #5: ``.
    issue = IssueRef(number=5, title="")

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == "# Issue #5:"


def test_render_issue_context_empty_title_with_body_still_carries_the_body() -> None:
    issue = IssueRef(number=5, title="", body="## What to build\nA thing.")

    rendered = orchestrator.render_issue_context(issue)

    assert rendered == "# Issue #5:\n\n## What to build\nA thing."


class _PromptRecordingRuntime:
    """A fake Runtime that records the prompt handed to each phase.

    Like the graph's stub it writes ``STUB_MARKER`` into the worktree so the
    git-aware runner reports a non-empty diff, but it also stores the exact
    prompt string per phase so a test can assert the issue-context preamble
    reached each phase.
    """

    name = "stub"

    def __init__(self) -> None:
        self.prompts: dict[str, str] = {}

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        self.prompts[phase] = prompt
        (cwd / STUB_MARKER).write_text(f"phase {phase}\n")
        return RuntimeResult(
            output=f"ran {phase}",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


def test_issue_context_reaches_every_phase_prompt(
    three_phase_fixture_dir: Path, tmp_path: Path
) -> None:
    # The whole point of the issue: each phase's prompt must carry the issue's
    # number, title, and body so the runtime is not working the issue blind.
    issue = IssueRef(
        number=65,
        title="Hand the agent its issue context",
        body="MARKER-BODY: build the preamble.",
        comments=[
            IssueComment(author="maintainer", body="MARKER-COMMENT: use the brief.")
        ],
        assignees=["krishna"],
    )
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _PromptRecordingRuntime()

    orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        selected=source.list_ready(),
        fixture_dir=three_phase_fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=_git_aware_runner(),
    )

    assert set(runtime.prompts) == {"plan", "implement", "review"}
    for phase_name, prompt in runtime.prompts.items():
        assert prompt.startswith(
            "# Issue #65: Hand the agent its issue context"
        ), phase_name
        assert "MARKER-BODY: build the preamble." in prompt
        assert "### @maintainer\n\nMARKER-COMMENT: use the brief." in prompt
