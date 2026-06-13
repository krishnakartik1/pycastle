"""Retry with handoff on failed implement attempts (#7).

A failed implement attempt (an agent crash, or a clean run whose gates come
back red) is retried in place on the same worktree, carrying context from the
previous attempt. Before each retry a handoff document is written summarising
what was tried and what to fix; for Codex the handoff resumes the thread that
did the failed attempt. An item that exhausts its retries is labelled for human
handling and the run moves on to the next item. Every agent / git / gh call is
mocked — no real subprocess runs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from pycastle import orchestrator
from pycastle.models import IssueRef, RuntimeResult, Telemetry
from pycastle.runtime import AgentCrashError

HANDOFF_REL = orchestrator.HANDOFF_DOC


def _ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="")


def _git_aware_runner() -> MagicMock:
    """A fake runner that creates worktree dirs and otherwise succeeds.

    ``git worktree add <path> <branch>`` makes the directory so the stub
    runtime has a real ``cwd``; everything else is a clean success. No real git
    or gh is ever invoked.
    """

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        return _ok()

    return MagicMock(side_effect=side_effect)


class _RecordingRuntime:
    """A fake Runtime that records every prompt/phase it is asked to run.

    ``gate_results`` is consumed one entry per implement attempt: ``False``
    means "this attempt's gates fail" (handled by the injected gate check in the
    test, not here) — this runtime only records calls and returns success.
    """

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        self.calls.append({"prompt": prompt, "phase": phase, "cwd": cwd})
        (cwd / "PYCASTLE_STUB.md").write_text("stub\n")
        return RuntimeResult(
            output="ok",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


def _gate(*results: bool) -> MagicMock:
    """A gate check that returns ``results`` in order, then ``True`` forever."""
    seq = list(results)

    def side_effect(_cwd: Path) -> bool:
        return seq.pop(0) if seq else True

    return MagicMock(side_effect=side_effect)


# --------------------------------------------------------------------------- #
# Retry path: a failed attempt retries with context from the previous attempt. #
# --------------------------------------------------------------------------- #


def test_failed_gates_retry_with_prior_attempt_context(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Flaky", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()
    # First implement attempt's gates fail; the retry passes.
    gate = _gate(False, True)

    outcome = orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        impl_retries=2,
        gate_check=gate,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # The implement phase ran twice: once for the failed attempt, once retried.
    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    assert len(impl_calls) == 2
    # The first attempt's prompt had no prior-attempt context; the retry does.
    assert "Previous Attempt" not in impl_calls[0]["prompt"]
    assert "Previous Attempt" in impl_calls[1]["prompt"]
    assert HANDOFF_REL in impl_calls[1]["prompt"]
    # The retried attempt passed its gates, so the issue is worked and merged.
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()


def test_agent_crash_counts_as_a_failed_attempt_and_retries(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Crashy", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = _git_aware_runner()
    gate = _gate()  # gates always pass; the failure is the crash itself

    crashing = MagicMock()
    good = RuntimeResult(
        output="ok",
        telemetry=Telemetry(runtime="stub", phase="implement", num_turns=1),
    )

    def run_side_effect(prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        (cwd / "PYCASTLE_STUB.md").write_text("stub\n")
        if crashing.run.call_count == 1:
            raise AgentCrashError("boom", phase="implement", exit_code=1)
        return good

    crashing.name = "stub"
    crashing.run.side_effect = run_side_effect

    outcome = orchestrator.run_batch(
        runtime=crashing,
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        impl_retries=2,
        gate_check=gate,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # The first call crashed; the second succeeded — the crash was a failed
    # attempt that got retried, not a fatal error.
    assert crashing.run.call_count == 2
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()


# --------------------------------------------------------------------------- #
# Handoff generation on gate failure.                                          #
# --------------------------------------------------------------------------- #


def test_handoff_document_is_generated_on_gate_failure(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issue = IssueRef(number=2, title="Needs handoff", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()
    gate = _gate(False, True)

    orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=1,
        impl_retries=2,
        gate_check=gate,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # A handoff phase was run against the failed attempt to write the document,
    # describing what was tried and what to fix next.
    handoff_calls = [c for c in runtime.calls if c["phase"] == "handoff"]
    assert len(handoff_calls) == 1
    prompt = handoff_calls[0]["prompt"]
    assert HANDOFF_REL in prompt
    assert "fix" in prompt.lower()


def test_codex_handoff_resumes_the_failed_attempts_thread(tmp_path: Path) -> None:
    """The Codex handoff resumes the thread id of the failed attempt."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    codex = MagicMock()
    codex.name = "codex"

    def run_side_effect(
        prompt: str, *, cwd: Path, phase: str, resume_thread_id: str | None = None
    ) -> RuntimeResult:
        (cwd / orchestrator.HANDOFF_DOC).parent.mkdir(parents=True, exist_ok=True)
        (cwd / orchestrator.HANDOFF_DOC).write_text("handoff\n")
        return RuntimeResult(
            output="",
            telemetry=Telemetry(runtime="codex", phase=phase),
        )

    codex.run.side_effect = run_side_effect

    created = orchestrator.generate_handoff(
        codex,
        worktree=worktree,
        thread_id="thread-789",
        gate_output="ruff failed",
    )

    assert created is True
    # The handoff resumed the failed attempt's thread, keeping its context.
    assert codex.run.call_args.kwargs["resume_thread_id"] == "thread-789"
    assert codex.run.call_args.kwargs["phase"] == "handoff"


def test_claude_handoff_is_a_fresh_call_without_thread_resume(
    tmp_path: Path,
) -> None:
    """Claude has no thread resume; the handoff is a fresh call with context."""
    worktree = tmp_path / "wt"
    worktree.mkdir()
    claude = MagicMock()
    claude.name = "claude"

    # A bare Runtime ``run`` signature has no ``resume_thread_id`` kwarg.
    def run_side_effect(prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        doc = cwd / orchestrator.HANDOFF_DOC
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("handoff\n")
        return RuntimeResult(
            output="", telemetry=Telemetry(runtime="claude", phase=phase)
        )

    claude.run.side_effect = run_side_effect

    created = orchestrator.generate_handoff(
        claude,
        worktree=worktree,
        thread_id="ignored",
        gate_output="black failed",
    )

    assert created is True
    # No resume kwarg was passed: Claude gets a fresh call carrying context.
    assert "resume_thread_id" not in claude.run.call_args.kwargs


# --------------------------------------------------------------------------- #
# Exhausted retries -> ready-for-human, and the loop continues.               #
# --------------------------------------------------------------------------- #


def test_exhausted_retries_mark_for_human_and_loop_continues(
    fixture_dir: Path, tmp_path: Path
) -> None:
    issues = [
        IssueRef(number=2, title="Always red", assignees=["krishna"]),
        IssueRef(number=4, title="Good one", assignees=["krishna"]),
    ]
    source = MagicMock()
    source.list_ready.return_value = issues
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()
    # Issue #2: every attempt's gate fails (3 attempts = 1 + 2 retries) -> False
    # thrice. Issue #4: passes first time.
    gate = _gate(False, False, False, True)

    outcome = orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-101500",
        iterations=5,
        impl_retries=2,
        gate_check=gate,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # #2 exhausted its retries and was handed to a human; #4 still completed.
    source.mark_for_human.assert_called_once_with(2)
    assert outcome.completed == [4]
    merged = {o.issue.number: o.merged for o in outcome.issues}
    assert merged == {2: False, 4: True}

    # #2 ran 1 + 2 = 3 implement attempts before giving up.
    impl_for_2 = [
        c
        for c in runtime.calls
        if c["phase"] == "implement" and "issue-2" in str(c["cwd"])
    ]
    assert len(impl_for_2) == 3
    # #2's worktree and branch were still cleaned up.
    removed = [
        call.args[0][3]
        for call in runner.call_args_list
        if call.args[0][:3] == ["git", "worktree", "remove"]
    ]
    assert str(tmp_path / "wt" / "issue-2") in removed


def test_default_runs_a_single_attempt_with_no_retries(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """With the default gate (always pass) one attempt is made, no handoff."""
    issue = IssueRef(number=2, title="Happy path", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()

    outcome = orchestrator.run_batch(
        runtime=runtime,
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

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    assert len(impl_calls) == 1
    assert not any(c["phase"] == "handoff" for c in runtime.calls)
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()
