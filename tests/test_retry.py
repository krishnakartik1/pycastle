"""Retry with handoff on failed implement attempts (#8).

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

import pytest

from pycastle import cli, orchestrator, sandbox
from pycastle.cli import main
from pycastle.models import IssueComment, IssueRef, RuntimeResult, Telemetry
from pycastle.readiness import (
    CHECK_IDS,
    EligibleItem,
    ReadinessCheck,
    ReadinessConfiguration,
    ReadinessReport,
    Status,
)
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
        if argv[:3] == ["git", "diff", "--quiet"]:
            # A non-empty diff (exit 1): the worked issue produced real changes.
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
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
    """A gate check returning ``GateOutcome``s for ``results``, then pass forever.

    Each ``results`` entry is the ``passed`` verdict; the remaining calls pass.
    A failing outcome carries a little output so the surfacing path has text.
    """
    seq = list(results)

    def side_effect(_cwd: Path) -> orchestrator.GateOutcome:
        passed = seq.pop(0) if seq else True
        return orchestrator.GateOutcome(
            passed=passed, output="" if passed else "gates red\n"
        )

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


def test_default_with_no_gate_makes_one_attempt_and_passes(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """Production parity: with no gate_check wired, the default always passes.

    The behaviour must be unchanged from before the retry slice — exactly one
    implement attempt, no handoff, no retry — even though gate_check is omitted
    entirely (so the default ``_gates_always_pass`` is selected).
    """
    issue = IssueRef(number=2, title="No gate", assignees=["krishna"])
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
        impl_retries=2,  # retries are budgeted but never used: gates always pass
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    assert len(impl_calls) == 1
    assert not any(c["phase"] == "handoff" for c in runtime.calls)
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()


# --------------------------------------------------------------------------- #
# Mixed failure sequences and end-to-end handoff threading through the loop.   #
# --------------------------------------------------------------------------- #


class _ScriptedRuntime:
    """A fake Runtime driven by a per-implement-attempt script.

    ``implement_script`` is consumed one entry per implement call: ``"crash"``
    raises :class:`AgentCrashError`, anything else is a clean run. The runtime
    writes a real handoff document on a ``handoff`` phase so the orchestrator's
    handoff path produces a true on-disk doc, and stamps ``thread_id`` on every
    telemetry record so the Codex-resume wiring can be exercised through the
    whole loop. Records every call for assertions; no real subprocess runs.
    """

    def __init__(
        self, implement_script: list[str], *, name: str = "stub", thread_id: str = ""
    ) -> None:
        self.name = name
        self._script = list(implement_script)
        self._thread_id = thread_id or None
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        phase: str,
        resume_thread_id: str | None = None,
    ) -> RuntimeResult:
        self.calls.append(
            {
                "prompt": prompt,
                "phase": phase,
                "cwd": cwd,
                "resume_thread_id": resume_thread_id,
            }
        )
        if phase == "implement":
            step = self._script.pop(0) if self._script else "ok"
            if step == "crash":
                raise AgentCrashError("boom", phase="implement", exit_code=1)
        if phase == "handoff":
            doc = cwd / orchestrator.HANDOFF_DOC
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text("handoff\n")
        (cwd / "PYCASTLE_STUB.md").write_text("stub\n")
        return RuntimeResult(
            output="ok",
            telemetry=Telemetry(
                runtime=self.name,
                phase=phase,
                num_turns=1,
                thread_id=self._thread_id,
            ),
        )


def test_crash_then_gate_fail_then_pass_is_a_single_worked_issue(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A crash and a gate-fail are both failed attempts within one issue's budget.

    Attempt 1 crashes (no handoff doc, but the next prompt notes the crash);
    attempt 2 runs clean but the gates are red (a handoff doc is written);
    attempt 3 passes. With ``impl_retries=2`` that is exactly the 3-attempt
    budget, so the issue is worked, not handed to a human.
    """
    issue = IssueRef(number=2, title="Crash then red", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _ScriptedRuntime(["crash", "ok", "ok"])
    runner = _git_aware_runner()
    # Attempt 1 crashes before the gate runs; attempt 2's gate is red; attempt 3
    # passes. Only the two non-crashing attempts reach the gate check.
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

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    handoff_calls = [c for c in runtime.calls if c["phase"] == "handoff"]
    # Three implement attempts: crash, gate-red, pass.
    assert len(impl_calls) == 3
    # A handoff doc is written only on the gate-red attempt, not on the crash.
    assert len(handoff_calls) == 1
    # The crash threaded a "Previous Attempt" block into attempt 2's prompt...
    assert "Previous Attempt" in impl_calls[1]["prompt"]
    assert "crash" in impl_calls[1]["prompt"].lower()
    # ...and the gate-red attempt threaded its own block into attempt 3.
    assert "Previous Attempt" in impl_calls[2]["prompt"]
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()


def test_codex_thread_id_from_telemetry_resumes_through_the_loop(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """End to end: the handoff resumes the failed implement attempt's thread id.

    A Codex-named runtime stamps a thread id on its implement telemetry. When
    the gates come back red, the orchestrator reads that id off the last phase
    result and the handoff call resumes it — proving the
    telemetry -> _last_thread_id -> generate_handoff wiring, not just the unit.
    """
    issue = IssueRef(number=2, title="Codex retry", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _ScriptedRuntime([], name="codex", thread_id="thread-42")
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

    handoff_calls = [c for c in runtime.calls if c["phase"] == "handoff"]
    assert len(handoff_calls) == 1
    # The handoff resumed the thread id stamped on the failed attempt's telemetry.
    assert handoff_calls[0]["resume_thread_id"] == "thread-42"


def test_missing_handoff_doc_degrades_to_a_plain_retry(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A runtime that never writes the handoff doc still gets a clean retry.

    ``generate_handoff`` returns ``False`` when no document lands, and the loop
    must not crash on that — it threads a softer "if the previous attempt left
    one" context block and retries. The next attempt still runs and passes.
    """
    issue = IssueRef(number=2, title="No handoff doc", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    # _RecordingRuntime never writes HANDOFF_DOC, so the handoff "fails" to land.
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()
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

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    # The handoff doc never landed, but the retry still ran and the issue worked.
    assert not (tmp_path / "wt" / "issue-2" / orchestrator.HANDOFF_DOC).exists()
    assert len(impl_calls) == 2
    # The softer "if the previous attempt left one" phrasing is used when no doc.
    assert "if the previous attempt left one" in impl_calls[1]["prompt"]
    assert outcome.completed == [2]


def test_prior_attempt_block_carries_the_failing_gate_info(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """The retry context names the handoff doc AND the failing-gate output.

    Review focus #5: the ``## Previous Attempt`` block threaded into the next
    implement prompt must point at the handoff document and carry the failing
    gate output, so the next attempt knows what to fix.
    """
    issue = IssueRef(number=2, title="Context block", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _ScriptedRuntime([])
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

    retry_prompt = [c for c in runtime.calls if c["phase"] == "implement"][1]["prompt"]
    assert "## Previous Attempt" in retry_prompt
    assert HANDOFF_REL in retry_prompt
    # The gate's REAL captured output reaches the next attempt's prompt verbatim
    # (the static "quality gates reported a failure" string is only a fallback for
    # a gate that produced no output) (#28).
    assert "gates red" in retry_prompt
    assert "Fix the failing gates before finishing." in retry_prompt


def test_retry_context_never_reaches_plan_or_review_phases(
    three_phase_fixture_dir: Path, tmp_path: Path
) -> None:
    """On the default graph the retry block lands in implement alone (#7, #8, #10).

    With the branching walker (#10), the implement retry is internal to the
    implement node: plan and review each run *once* on the walk, while implement
    retries in place. This drives a real gate-fail retry through the walk and
    proves the isolation end to end: every plan and review prompt is free of the
    "Previous Attempt" block, while only the retried implement prompt carries it.
    """
    issue = IssueRef(
        number=2,
        title="Three phase retry",
        assignees=["krishna"],
        comments=[IssueComment(author="maintainer", body="COMMENT-MARKER")],
    )
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()
    # First implement attempt's gates fail; the retry passes — implement runs
    # twice (retry is internal to the node), plan and review run once each.
    gate = _gate(False, True)

    outcome = orchestrator.run_batch(
        runtime=runtime,
        issue_source=source,
        fixture_dir=three_phase_fixture_dir,
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

    plan_calls = [c for c in runtime.calls if c["phase"] == "plan"]
    review_calls = [c for c in runtime.calls if c["phase"] == "review"]
    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    # The walk runs each non-implement phase once; implement retries in place, so
    # it ran twice (failed attempt + retry).
    assert len(plan_calls) == 1
    assert len(review_calls) == 1
    assert len(impl_calls) == 2
    # No plan or review prompt carried implement's retry context.
    assert not any("Previous Attempt" in c["prompt"] for c in plan_calls)
    assert not any("Previous Attempt" in c["prompt"] for c in review_calls)
    # The retried implement prompt is the only one that carried it.
    assert "Previous Attempt" not in impl_calls[0]["prompt"]
    assert "Previous Attempt" in impl_calls[1]["prompt"]
    assert all(
        "COMMENT-MARKER" in c["prompt"]
        for c in [*plan_calls, *impl_calls, *review_calls]
    )
    assert outcome.completed == [2]


# --------------------------------------------------------------------------- #
# The gate is sourced from the Project fixture, not injected as a kwarg (#14).  #
# --------------------------------------------------------------------------- #


def _write_gate(fixture_dir: Path) -> Path:
    """Drop a (no-op) gate file into the fixture and return its path.

    The contents never run — the gate subprocess is always mocked — but the file
    must exist so :func:`make_fixture_gate_check` decides the project opts into
    gating. Production runs this as an executable in the issue worktree.
    """
    gate = fixture_dir / orchestrator.FIXTURE_GATE
    gate.write_text("#!/usr/bin/env bash\nexit 0\n")
    gate.chmod(0o755)
    return gate


def _gating_runner(fixture_dir: Path, *gate_results: int) -> MagicMock:
    """A git-aware runner whose gate-command exit codes are scripted.

    Like :func:`_git_aware_runner`, but it also recognises the fixture's gate
    command (``[<fixture>/gate]``) and returns ``gate_results`` in order (then 0
    forever) as the gate's exit code. No real gate, git, or gh ever runs — the
    gate failing/passing is the scripted exit code, proving the gate is sourced
    from the fixture rather than injected as a ``gate_check=`` kwarg.
    """
    gate_path = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())
    codes = list(gate_results)

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == [gate_path]:
            code = codes.pop(0) if codes else 0
            return subprocess.CompletedProcess(args=argv, returncode=code, stdout="")
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        if argv[:3] == ["git", "diff", "--quiet"]:
            # A non-empty diff (exit 1): the worked issue produced real changes.
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        return _ok()

    return MagicMock(side_effect=side_effect)


def test_fixture_gate_failure_drives_the_retry_path_through_run_batch(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A failing fixture gate (not an injected kwarg) reaches the retry path.

    The gate command comes from ``make_fixture_gate_check(fixture_dir)``, exactly
    as the CLI wires it. The mocked gate exits non-zero on the first attempt and
    zero on the retry, so the run must make two implement attempts, write a
    handoff, and merge the issue — proving the fixture-sourced gate (#14), not a
    directly-injected ``gate_check=``, drives retry/handoff.
    """
    _write_gate(fixture_dir)
    issue = IssueRef(number=2, title="Fixture gate", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    # Gate fails on attempt 1, passes on the retry.
    runner = _gating_runner(fixture_dir, 1, 0)
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    handoff_calls = [c for c in runtime.calls if c["phase"] == "handoff"]
    # The failing fixture gate forced a second implement attempt with a handoff.
    assert len(impl_calls) == 2
    assert len(handoff_calls) == 1
    assert "Previous Attempt" in impl_calls[1]["prompt"]
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()


def test_fixture_gate_runs_in_the_issue_worktree(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """The fixture gate is run with the issue worktree as its cwd.

    The gate must see the attempt's code, not the fixture, so it is invoked with
    ``cwd`` set to the issue worktree. This pins that the wiring runs the gate in
    the right place.
    """
    _write_gate(fixture_dir)
    issue = IssueRef(number=7, title="Worktree cwd", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _gating_runner(fixture_dir, 0)
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    gate_path = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())
    gate_calls = [call for call in runner.call_args_list if call.args[0] == [gate_path]]
    assert len(gate_calls) == 2
    # The Gate ran at Item scope and again at mandatory integrated Run scope.
    assert gate_calls[0].kwargs["cwd"] == tmp_path / "wt" / "issue-7"
    assert gate_calls[0].kwargs["capture"] is True
    assert gate_calls[1].kwargs["cwd"] == tmp_path / "wt" / "run-20260613-101500"


def test_no_fixture_gate_makes_one_attempt_and_passes(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A fixture with no gate file falls back to always-pass (back-compat).

    ``make_fixture_gate_check`` on a fixture without a ``gate`` file behaves like
    the default ``_gates_always_pass``: exactly one implement attempt, no
    handoff, no retry — identical to the pre-#14 behaviour.
    """
    # No _write_gate(...) here: the fixture defines no gate.
    issue = IssueRef(number=2, title="No fixture gate", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _git_aware_runner()
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    assert len(impl_calls) == 1
    assert not any(c["phase"] == "handoff" for c in runtime.calls)
    assert outcome.completed == [2]
    source.mark_for_human.assert_not_called()


def test_cli_run_wires_the_fixture_gate_into_run_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pycastle run`` builds the gate from the fixture and passes it down.

    Driven through ``main(["run", ...])`` with every external mocked: the gate
    check handed to ``run_batch`` must be the one built from ``FIXTURE_DIR`` by
    ``make_fixture_gate_check`` — proving the CLI sources the gate from the
    project fixture rather than leaving the always-pass default in place (#14).
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(
        cli,
        "_evaluate_cli_readiness",
        lambda _args: ReadinessReport(
            1,
            True,
            "0.1.0",
            ReadinessConfiguration(
                "owner/repo", "main", "main", "stub", "host", None, "krishna", False, 1
            ),
            tuple(
                ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
            ),
            (EligibleItem(1, "One"),),
        ),
    )
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    sentinel = object()

    def fake_factory(fixture_dir: Path) -> object:
        # The CLI builds the gate from its fixture dir, not a hardcoded command.
        assert fixture_dir == cli.FIXTURE_DIR
        return sentinel

    monkeypatch.setattr(cli, "make_fixture_gate_check", fake_factory)

    captured: dict[str, object] = {}

    def fake_run_loop(*, gate_check: object, **_kwargs: object) -> MagicMock:
        captured["gate_check"] = gate_check
        outcome = MagicMock()
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--runtime", "stub"]) == 0
    # The fixture-sourced gate (not the always-pass default) reached run_batch.
    assert captured["gate_check"] is sentinel


def test_fixture_gate_exhaustion_marks_for_human_through_run_batch(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A fixture gate that never passes hands the issue to a human (exhaustion).

    The gate command comes from ``make_fixture_gate_check(fixture_dir)`` as the
    CLI wires it, and exits non-zero on every attempt. With ``impl_retries=2``
    that is three failing attempts (1 + 2), so the issue exhausts its retries and
    is marked ``ready-for-human`` — proving the exhaustion path (not just the
    failure-then-pass path) is reachable through the fixture-sourced gate (#14).
    """
    _write_gate(fixture_dir)
    issue = IssueRef(number=3, title="Always red gate", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    # 1 + 2 retries = 3 attempts, all failing the gate.
    runner = _gating_runner(fixture_dir, 1, 1, 1)
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    impl_calls = [c for c in runtime.calls if c["phase"] == "implement"]
    assert len(impl_calls) == 3
    source.mark_for_human.assert_called_once_with(3)
    assert outcome.completed == []


# --------------------------------------------------------------------------- #
# Exec safety: a gate that exists but cannot be launched fails sanely (#14).    #
# --------------------------------------------------------------------------- #


def test_gate_that_cannot_be_executed_is_a_failure_not_a_crash(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A gate that raises ``OSError`` when launched counts as a gate failure.

    A ``.pycastle/gate`` can exist yet be unlaunchable — it lost its executable
    bit on checkout (``PermissionError``) or has a bad interpreter line. The
    check must treat that as a *failing* gate (``False``), not let the OSError
    escape and abort the run.
    """
    _write_gate(fixture_dir)

    def exploding_runner(argv: list[str], **_kwargs: object) -> object:
        raise PermissionError(13, "Permission denied")

    gate_check = orchestrator.make_fixture_gate_check(
        fixture_dir, runner=exploding_runner
    )

    # No exception escapes; the unlaunchable gate is reported as failing.
    assert gate_check(tmp_path).passed is False


def test_unexecutable_fixture_gate_marks_for_human_not_crash_the_run(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """An unlaunchable fixture gate sends the issue to a human, not down the run.

    Driven through ``run_batch`` with the gate subprocess raising ``OSError`` on
    every attempt (as a real non-executable gate would). The run must survive:
    the issue exhausts its retries and is marked ``ready-for-human``, and the run
    completes normally rather than re-raising the OSError through the interrupt
    cleanup path.
    """
    _write_gate(fixture_dir)
    issue = IssueRef(number=5, title="Bad gate mode", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    gate_path = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())

    def runner_side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == [gate_path]:
            # The gate exists but cannot be launched (e.g. lost its +x bit).
            raise PermissionError(13, "Permission denied")
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        return _ok()

    runner = MagicMock(side_effect=runner_side_effect)
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    # The run did not crash: every attempt's gate failed, the issue was handed to
    # a human, and the batch finished cleanly.
    source.mark_for_human.assert_called_once_with(5)
    assert outcome.completed == []


def test_fixture_gate_path_is_resolved_absolute_regardless_of_cwd(
    fixture_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is invoked by an absolute path, even from a relative fixture_dir.

    The gate runs with the issue worktree as cwd, so a relative fixture path
    would not resolve against the worktree. Building the check from a *relative*
    fixture_dir and changing cwd, the command passed to the runner must still be
    the gate's absolute, resolved path.
    """
    _write_gate(fixture_dir)
    abs_gate = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())

    captured: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")

    # A relative fixture_dir, evaluated from a different cwd than the worktree.
    monkeypatch.chdir(fixture_dir.parent)
    rel_fixture = Path(fixture_dir.name)
    gate_check = orchestrator.make_fixture_gate_check(rel_fixture, runner=runner)

    worktree = tmp_path / "elsewhere"
    worktree.mkdir()
    assert gate_check(worktree).passed is True
    # The runner saw the absolute gate path, not a path relative to the worktree.
    assert captured["argv"] == [abs_gate]


# --------------------------------------------------------------------------- #
# Gate runs where the phases run: in-container under --sandbox docker (#28).    #
# --------------------------------------------------------------------------- #


def test_docker_gate_wraps_canonical_gate_through_build_run_command(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """The docker gate wraps the CANONICAL gate through ``build_run_command``.

    Under ``--sandbox docker`` the gate must run inside the same agent image as
    the phases, via the same wrapper the runtime uses: the resolved image, the
    issue worktree as ``-w``, the repo root bind-mounted at its own path (the #50
    contract), the runtime's auth mount reused as-is, and the inner argv running
    ``bash`` on the repo-root gate — NOT the worktree's copy, so an attempt cannot
    weaken its own gate.
    """
    _write_gate(fixture_dir)
    repo_root = tmp_path
    worktree = tmp_path / "wt" / "issue-9"
    worktree.mkdir(parents=True)

    captured: dict[str, object] = {}

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")

    gate_check = orchestrator.make_fixture_gate_check(
        fixture_dir,
        runner=runner,
        sandbox="docker",
        image="img:tag",
        runtime_name="claude",
        workspace=repo_root,
    )
    assert gate_check(worktree).passed is True

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "img:tag" in argv
    # -w is the issue worktree: the gate gets the same cwd it has on the host.
    assert argv[argv.index("-w") + 1] == str(worktree.resolve())
    # The repo root is mounted at its own path (mount = workspace, not worktree).
    assert f"{repo_root.resolve()}:{repo_root.resolve()}" in argv
    # The runtime's auth mount is reused verbatim (inert for the gate).
    assert f"{sandbox.auth_volume('claude')}:{sandbox.CLAUDE_CONFIG_DIR}" in argv
    # The inner argv runs bash on the CANONICAL repo-root gate, absolute.
    canonical = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())
    assert argv[-2:] == ["bash", canonical]
    # And NOT the worktree's own copy of the gate.
    assert canonical != str((worktree / orchestrator.FIXTURE_GATE).resolve())


def test_docker_gate_returns_outcome_with_captured_output(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """The docker gate carries the captured stdout+stderr back in the outcome."""
    _write_gate(fixture_dir)
    worktree = tmp_path / "wt" / "issue-1"
    worktree.mkdir(parents=True)

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="boom\n", stderr="err\n"
        )

    gate_check = orchestrator.make_fixture_gate_check(
        fixture_dir,
        runner=runner,
        sandbox="docker",
        image="img:tag",
        runtime_name="claude",
        workspace=tmp_path,
    )
    outcome = gate_check(worktree)
    assert outcome.passed is False
    assert "boom" in outcome.output
    assert "err" in outcome.output


def test_host_gate_unchanged(fixture_dir: Path, tmp_path: Path) -> None:
    """The host gate is byte-for-byte unchanged: bare argv, cwd=worktree, no docker."""
    _write_gate(fixture_dir)
    abs_gate = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())
    worktree = tmp_path / "wt" / "issue-1"
    worktree.mkdir(parents=True)

    captured: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")

    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)
    assert gate_check(worktree).passed is True
    assert captured["argv"] == [abs_gate]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["cwd"] == worktree
    assert "docker" not in captured["argv"]


def test_docker_gate_oserror_is_failing_outcome(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A docker gate whose ``docker run`` cannot be spawned is a failing outcome."""
    _write_gate(fixture_dir)
    worktree = tmp_path / "wt" / "issue-1"
    worktree.mkdir(parents=True)

    def runner(argv: list[str], **_kwargs: object) -> object:
        raise OSError("docker not found")

    gate_check = orchestrator.make_fixture_gate_check(
        fixture_dir,
        runner=runner,
        sandbox="docker",
        image="img:tag",
        runtime_name="claude",
        workspace=tmp_path,
    )
    outcome = gate_check(worktree)
    assert outcome.passed is False
    assert outcome.output


# --------------------------------------------------------------------------- #
# Surface gate output into the per-issue transcript (#28).                      #
# --------------------------------------------------------------------------- #


def _gating_runner_with_output(fixture_dir: Path, code: int, output: str) -> MagicMock:
    """A git-aware runner whose gate returns a fixed exit code and stdout."""
    gate_path = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == [gate_path]:
            return subprocess.CompletedProcess(
                args=argv, returncode=code, stdout=output
            )
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        if argv[:3] == ["git", "diff", "--quiet"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        return _ok()

    return MagicMock(side_effect=side_effect)


def _transcript_path(fixture_dir: Path, run_id: str, issue_number: int) -> Path:
    return fixture_dir / "runs" / run_id / f"issue-{issue_number}-transcript.log"


def test_gate_output_surfaced_on_failure_without_verbose(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A failing gate surfaces its output ALWAYS, even with verbose off (#28)."""
    _write_gate(fixture_dir)
    issue = IssueRef(number=4, title="Red gate", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    # Gate fails once (with output) then passes, so the run still merges.
    gate_path = str((fixture_dir / orchestrator.FIXTURE_GATE).resolve())
    codes = [1, 0]

    def side_effect(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if argv == [gate_path]:
            code = codes.pop(0) if codes else 0
            return subprocess.CompletedProcess(
                args=argv, returncode=code, stdout="lint broke\n" if code else ""
            )
        if argv[:3] == ["git", "worktree", "add"]:
            Path(argv[3]).mkdir(parents=True, exist_ok=True)
            return _ok()
        if argv[:3] == ["git", "diff", "--quiet"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        return _ok()

    runner = MagicMock(side_effect=side_effect)
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        verbose=False,
    )

    transcript = _transcript_path(fixture_dir, "20260613-101500", 4)
    assert transcript.is_file()
    assert "[gate] [GATE] lint broke" in transcript.read_text()


def test_gate_output_not_surfaced_on_success_without_verbose(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A passing gate is NOT surfaced unless --verbose (#28)."""
    _write_gate(fixture_dir)
    issue = IssueRef(number=6, title="Green gate", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _gating_runner_with_output(fixture_dir, 0, "all good\n")
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        verbose=False,
    )

    transcript = _transcript_path(fixture_dir, "20260613-101500", 6)
    # No transcript line tagged GATE: a green gate stays silent without verbose.
    assert not transcript.is_file() or "[GATE]" not in transcript.read_text()


def test_gate_output_surfaced_on_success_under_verbose(
    fixture_dir: Path, tmp_path: Path
) -> None:
    """A passing gate IS surfaced under --verbose (#28)."""
    _write_gate(fixture_dir)
    issue = IssueRef(number=8, title="Green verbose", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runtime = _RecordingRuntime()
    runner = _gating_runner_with_output(fixture_dir, 0, "all green\n")
    gate_check = orchestrator.make_fixture_gate_check(fixture_dir, runner=runner)

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
        gate_check=gate_check,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
        verbose=True,
    )

    transcript = _transcript_path(fixture_dir, "20260613-101500", 8)
    assert transcript.is_file()
    assert "[gate] [GATE] all green" in transcript.read_text()


# Project setup runs before the phase walk (#82).                               #


def test_fixture_setup_runs_in_worktree_before_any_phase(
    fixture_dir: Path, tmp_path: Path
) -> None:
    setup = fixture_dir / orchestrator.FIXTURE_SETUP
    setup.write_text("#!/usr/bin/env bash\nexit 0\n")
    setup.chmod(0o755)
    events: list[str] = []

    def runner(argv: list[str], **kwargs: object) -> MagicMock:
        if argv == [str(setup.resolve())]:
            events.append("setup")
            assert kwargs["cwd"] == tmp_path
        return MagicMock(returncode=0, stdout="", stderr="")

    run_setup = orchestrator.make_fixture_setup(fixture_dir, runner=runner)
    run_setup(tmp_path)
    events.append("phase")
    assert events == ["setup", "phase"]


def test_docker_setup_uses_the_same_sandbox_wrapper(
    fixture_dir: Path, tmp_path: Path
) -> None:
    setup = fixture_dir / orchestrator.FIXTURE_SETUP
    setup.write_text("#!/usr/bin/env bash\nexit 0\n")
    setup.chmod(0o755)
    captured: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> MagicMock:
        captured["argv"] = argv
        captured.update(kwargs)
        return MagicMock(returncode=0, stdout="", stderr="")

    worktree = tmp_path / "issue-82"
    run_setup = orchestrator.make_fixture_setup(
        fixture_dir,
        runner=runner,
        sandbox="docker",
        image="agent:test",
        runtime_name="codex",
        workspace=tmp_path,
    )
    run_setup(worktree)

    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[-2:] == ["bash", str(setup.resolve())]
    assert "agent:test" in argv
    assert str(worktree) in argv
    assert "cwd" not in captured


def test_fixture_setup_absent_is_a_noop(fixture_dir: Path, tmp_path: Path) -> None:
    runner = MagicMock()
    orchestrator.make_fixture_setup(fixture_dir, runner=runner)(tmp_path)
    runner.assert_not_called()


def test_fixture_setup_surfaces_nonzero_exit_and_combined_output(
    fixture_dir: Path, tmp_path: Path
) -> None:
    setup = fixture_dir / orchestrator.FIXTURE_SETUP
    setup.write_text("#!/usr/bin/env bash\nexit 7\n")
    setup.chmod(0o755)
    runner = MagicMock(
        return_value=MagicMock(returncode=7, stdout="install failed\n", stderr=None)
    )

    with pytest.raises(orchestrator.SetupError, match="install failed"):
        orchestrator.make_fixture_setup(fixture_dir, runner=runner)(tmp_path)


def test_fixture_setup_wraps_launch_errors(fixture_dir: Path, tmp_path: Path) -> None:
    setup = fixture_dir / orchestrator.FIXTURE_SETUP
    setup.write_text("#!/usr/bin/env bash\nexit 0\n")
    setup.chmod(0o755)
    runner = MagicMock(side_effect=OSError("interpreter unavailable"))

    with pytest.raises(orchestrator.SetupError, match="interpreter unavailable"):
        orchestrator.make_fixture_setup(fixture_dir, runner=runner)(tmp_path)
