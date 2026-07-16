from pathlib import Path

import pytest

from pycastle.orchestrator import FrozenRunExecution, SetupError, SetupFailure


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
