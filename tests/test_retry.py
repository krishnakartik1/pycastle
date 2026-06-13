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
    # The failing-gate output reaches the next attempt's prompt verbatim.
    assert "quality gates reported a failure" in retry_prompt
    assert "Fix the failing gates before finishing." in retry_prompt
