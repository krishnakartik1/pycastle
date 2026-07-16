import json
import subprocess
from pathlib import Path

import pytest

from pycastle import orchestrator
from pycastle.graph import DONE, HUMAN, execution_graph, runtime_node
from pycastle.models import IssueRef, RuntimeResult, Telemetry
from pycastle.orchestrator import (
    FrozenRunExecution,
    SetupError,
    SetupFailure,
    _walk_execution_graph,
)
from pycastle.runtime import AgentCrashError


def test_setup_error_requires_structured_failure_facts() -> None:
    failure = SetupFailure(".pycastle/setup", {"kind": "exited", "code": 17})

    error = SetupError(failure)

    assert error.failure is failure
    with pytest.raises(TypeError, match="SetupFailure"):
        SetupError("legacy setup failure")  # type: ignore[arg-type]


def test_freeze_requires_both_mandatory_executables(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    setup = fixture / "setup"
    setup.write_text("#!/bin/sh\n")
    setup.chmod(0o755)

    with pytest.raises(FileNotFoundError):
        FrozenRunExecution.freeze(fixture, "missing-gate")


@pytest.mark.parametrize("identity", ["", "///", "x" * 500])
def test_execution_record_identity_is_safe_and_bounded(identity: str) -> None:
    component = FrozenRunExecution._record_identity(identity)

    assert component
    assert "/" not in component
    assert len(component) <= 61


def _execution(tmp_path: Path, *, setup: bytes | None = None) -> FrozenRunExecution:
    return FrozenRunExecution(
        tmp_path / "frozen" / "setup",
        tmp_path / "frozen" / "gate",
        tmp_path / "records",
        setup or b"#!/bin/sh\nexit 0\n",
        0o755,
        b"#!/bin/sh\nexit 0\n",
        0o755,
        {"fail.md": "fail prompt", "repair.md": "repair prompt"},
    )


def test_setup_record_persistence_failure_is_an_orchestration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    execution = _execution(tmp_path)

    def fail_persistence(*_args: object, **_kwargs: object) -> object:
        raise OSError("record store unavailable")

    monkeypatch.setattr(orchestrator, "execute_hook", fail_persistence)

    with pytest.raises(OSError, match="record store unavailable"):
        execution.invoke_setup(tmp_path, identity="bootstrap", ordinal=1)


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (AgentCrashError("crashed", node="fail", exit_code=17), "exited"),
        (FileNotFoundError(2, "missing runtime"), "launch_error"),
        (subprocess.TimeoutExpired("runtime", 3), "timeout"),
        (ValueError("malformed provider result"), "malformed_result"),
        (object(), "malformed_result"),
    ],
)
def test_runtime_failure_supplies_typed_one_hop_context(
    tmp_path: Path, failure: object, kind: str
) -> None:
    repair_prompts: list[str] = []

    class Runtime:
        name = "test"

        def run(self, prompt: str, *, cwd: Path, node: str) -> RuntimeResult:
            if node == "fail":
                if isinstance(failure, BaseException):
                    raise failure
                return failure  # type: ignore[return-value]
            repair_prompts.append(prompt)
            return RuntimeResult(
                output="repaired",
                telemetry=Telemetry(runtime=self.name, node=node),
            )

    graph = execution_graph(
        start="fail",
        nodes=[
            runtime_node("fail", "fail.md", on_success=HUMAN, on_failure="repair"),
            runtime_node("repair", "repair.md", on_success=DONE),
        ],
    )

    walked, _ = _walk_execution_graph(
        IssueRef(number=1, title="One"),
        runtime=Runtime(),
        worktree=tmp_path,
        graph=graph,
        execution=_execution(tmp_path),
    )

    assert walked.terminal is DONE
    evidence = json.loads(repair_prompts[0].rsplit("\n\n", 1)[-1])
    assert evidence["source"] == "fail"
    assert evidence["success"] is False
    assert evidence["termination"]["kind"] == kind


def test_runtime_cannot_change_later_frozen_prompts_or_setup(tmp_path: Path) -> None:
    trace = tmp_path / "setup-trace"
    setup = f"#!/bin/sh\nprintf 'original\\n' >> {trace}\n".encode()
    execution = _execution(tmp_path, setup=setup)
    repair_prompts: list[str] = []

    class Runtime:
        name = "test"

        def run(self, prompt: str, *, cwd: Path, node: str) -> RuntimeResult:
            if node == "fail":
                execution.setup.parent.mkdir(parents=True, exist_ok=True)
                execution.setup.write_text(
                    f"#!/bin/sh\nprintf 'mutated\\n' >> {trace}\n"
                )
                execution.setup.chmod(0o755)
                materialized = tmp_path / "runs" / "run" / "project" / "prompts"
                materialized.mkdir(parents=True)
                (materialized / "repair.md").write_text("mutated prompt")
                raise AgentCrashError("crashed", node=node, exit_code=1)
            repair_prompts.append(prompt)
            return RuntimeResult(
                output="repaired",
                telemetry=Telemetry(runtime=self.name, node=node),
            )

    graph = execution_graph(
        start="fail",
        nodes=[
            runtime_node("fail", "fail.md", on_failure="repair"),
            runtime_node("repair", "repair.md", on_success=DONE),
        ],
    )

    walked, _ = _walk_execution_graph(
        IssueRef(number=1, title="One"),
        runtime=Runtime(),
        worktree=tmp_path,
        graph=graph,
        execution=execution,
    )

    assert walked.terminal is DONE
    assert "repair prompt" in repair_prompts[0]
    assert "mutated prompt" not in repair_prompts[0]
    assert trace.read_text().splitlines() == ["original", "original"]
