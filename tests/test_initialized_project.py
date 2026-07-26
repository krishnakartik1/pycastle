"""The newly initialized Project fixture works as one complete Run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle import cli
from pycastle.cli import main
from pycastle.fixture_validation import validate_project_fixture_structure
from pycastle.issues import IssueRef
from pycastle.orchestrator import run_batch
from pycastle.readiness import FrozenReadinessInputs, _freeze_project_fixture
from pycastle.runtime import StubRuntime


def test_initialized_project_selects_claims_completes_and_publishes_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    assert main(["init", "--sandbox", "host"]) == 0
    fixture = tmp_path / ".pycastle"

    # Initialization deliberately leaves verification fail-closed. Configuring
    # that project-owned policy is the only prerequisite for this test Run.
    gate = fixture / "gate"
    gate.write_text("#!/bin/sh\nexit 0\n")
    gate.chmod(0o755)
    selection = fixture / "prompts/select-item.md"
    selection.write_text(
        selection.read_text() + "\nPrefer Items in the project's release milestone.\n"
    )

    definition = validate_project_fixture_structure(fixture)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initialize PyCastle"], cwd=tmp_path, check=True
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    item = IssueRef(
        number=7,
        title="Exercise the initialized project",
        body="Prove the generated Project fixture works end to end.",
        labels=["ready-for-agent"],
        assignees=["krishna"],
    )
    frozen = FrozenReadinessInputs(
        base_commit,
        _freeze_project_fixture(fixture, definition),
        (item.model_copy(deep=True),),
        "host",
        "stub",
        None,
    )
    calls: list[list[str]] = []

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "73\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=capture,
            text=True,
            check=False,
        )

    prompts: dict[str, str] = {}

    class RecordingStubRuntime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            prompts[node] = prompt
            return super().run(prompt, cwd=cwd, node=node)

    source = MagicMock()
    source.is_still_eligible.return_value = True
    outcome = run_batch(
        runtime=RecordingStubRuntime(),
        issue_source=source,
        candidates=(item,),
        fixture_dir=fixture,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="initialized-project-173",
        iterations=1,
        workspace=tmp_path,
        runner=runner,
        frozen_inputs=frozen,
    )

    assert outcome.succeeded
    assert outcome.selected == [7]
    assert outcome.completed == [7]
    assert outcome.pr_opened
    source.claim.assert_called_once_with(7, assignee="krishna")
    assert "release milestone" in prompts["item-selection"]
    assert any(call[:3] == ["gh", "pr", "create"] for call in calls)
    assert any(call[:3] == ["gh", "pr", "ready"] for call in calls)
