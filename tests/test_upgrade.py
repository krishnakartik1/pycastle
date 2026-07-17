"""Transactional forward migration of Project fixtures."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from pycastle.graph import GateNode, RuntimeNode, load_run
from pycastle.migrations import MIGRATIONS, dockerfile_declares_host_identity
from pycastle.scaffold import scaffold_fixture
from pycastle.upgrade import (
    FixtureMigration,
    FixtureUpgradeError,
    upgrade_fixture,
    validate_fixture,
)


def _project(tmp_path: Path, marker: str = "1.0\n") -> tuple[Path, Path]:
    project = tmp_path / "project"
    scaffold_fixture(project, sandbox="host")
    fixture = project / ".pycastle"
    (fixture / "version").write_text(marker)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=project,
        check=True,
    )
    return project, fixture


def _migration(version: str, old: str, new: str, seen: list[str]) -> FixtureMigration:
    def target(fixture: Path) -> bool:
        return (fixture / "contract").read_text() == new

    def transform(fixture: Path) -> None:
        path = fixture / "contract"
        if path.read_text() != old:
            raise FixtureUpgradeError("unsafe customized contract")
        path.write_text(new)
        seen.append(version)

    return FixtureMigration(version, target, transform, lambda fixture: target(fixture))


def test_direct_multi_release_upgrade_runs_in_release_order(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    (fixture / "contract").write_text("old")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )
    seen: list[str] = []
    migrations = (
        _migration("1.2", "middle", "new", seen),
        _migration("1.1", "old", "middle", seen),
    )

    result = upgrade_fixture(project, runner_version="1.2", migrations=migrations)

    assert seen == ["1.1", "1.2"]
    assert (fixture / "contract").read_text() == "new"
    assert (fixture / "version").read_text() == "1.2\n"
    assert result.applied_versions == ("1.1", "1.2")
    assert subprocess.run(
        ["git", "status", "--short"], cwd=project, text=True, capture_output=True
    ).stdout.splitlines() == [" M .pycastle/contract", " M .pycastle/version"]


def test_target_condition_skips_manually_corrected_migration(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    (fixture / "contract").write_text("new")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )
    seen: list[str] = []

    result = upgrade_fixture(
        project,
        runner_version="1.1",
        migrations=(_migration("1.1", "old", "new", seen),),
    )

    assert seen == []
    assert result.applied_versions == ()
    assert (fixture / "version").read_text() == "1.1\n"


def test_no_applicable_migration_is_a_byte_for_byte_noop(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    before = {
        p.relative_to(fixture): p.read_bytes()
        for p in fixture.rglob("*")
        if p.is_file()
    }

    result = upgrade_fixture(project, runner_version="1.2", migrations=())

    after = {
        p.relative_to(fixture): p.read_bytes()
        for p in fixture.rglob("*")
        if p.is_file()
    }
    assert result.applied_versions == ()
    assert before == after
    assert (fixture / "version").read_text() == "1.0\n"


@pytest.mark.parametrize("marker", [None, "bad\n", "2.0\n"])
def test_invalid_marker_and_downgrade_leave_fixture_unchanged(
    tmp_path: Path, marker: str | None
) -> None:
    project, fixture = _project(tmp_path)
    if marker is None:
        (fixture / "version").unlink()
    else:
        (fixture / "version").write_text(marker)
    before = sorted(
        (p.relative_to(fixture), p.read_bytes())
        for p in fixture.rglob("*")
        if p.is_file()
    )

    with pytest.raises(FixtureUpgradeError):
        upgrade_fixture(project, runner_version="1.1", migrations=())

    after = sorted(
        (p.relative_to(fixture), p.read_bytes())
        for p in fixture.rglob("*")
        if p.is_file()
    )
    assert after == before


def test_dirty_worktree_is_refused_before_migration(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    (project / "dirty.txt").write_text("dirty")

    with pytest.raises(FixtureUpgradeError, match="dirty"):
        upgrade_fixture(project, runner_version="1.1", migrations=())

    assert (fixture / "version").read_text() == "1.0\n"


def test_unsafe_shape_and_write_failure_roll_back_every_file(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    (fixture / "contract").write_text("custom")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )
    migration = _migration("1.1", "old", "new", [])

    with pytest.raises(FixtureUpgradeError, match="unsafe"):
        upgrade_fixture(project, runner_version="1.1", migrations=(migration,))
    assert (fixture / "contract").read_text() == "custom"
    assert (fixture / "version").read_text() == "1.0\n"

    (fixture / "contract").write_text("old")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )

    def fail_marker(source: Path, destination: Path) -> None:
        if destination.name == "version":
            raise OSError("disk full")
        destination.write_bytes(source.read_bytes())

    with pytest.raises(FixtureUpgradeError, match="disk full"):
        upgrade_fixture(
            project, runner_version="1.1", migrations=(migration,), writer=fail_marker
        )
    assert (fixture / "contract").read_text() == "old"
    assert (fixture / "version").read_text() == "1.0\n"


def test_migration_specific_validation_failure_changes_nothing(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    (fixture / "contract").write_text("old")
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )
    migration = FixtureMigration(
        "1.1",
        lambda candidate: (candidate / "contract").read_text() == "new",
        lambda candidate: (candidate / "contract").write_text("new"),
        lambda _candidate: False,
    )

    with pytest.raises(FixtureUpgradeError, match="validation failed"):
        upgrade_fixture(project, runner_version="1.1", migrations=(migration,))

    assert (fixture / "contract").read_text() == "old"
    assert (fixture / "version").read_text() == "1.0\n"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda f: (f / "main.py").unlink(), "main.py"),
        (lambda f: (f / "gate").chmod(0o644), "executable"),
        (lambda f: (f / "setup").chmod(0o644), "executable"),
        (lambda f: (f / "sandbox").write_text("cloud\n"), "sandbox"),
        (lambda f: (f / "prompts" / "plan.md").unlink(), "prompt"),
        (lambda f: (f / "main.py").write_text("raise ValueError('broken')\n"), "load"),
    ],
)
def test_fixture_validator_checks_each_builtin_invariant(
    tmp_path: Path, mutate: Callable[[Path], object], message: str
) -> None:
    _, fixture = _project(tmp_path)
    mutate(fixture)

    with pytest.raises(FixtureUpgradeError, match=message):
        validate_fixture(fixture)


@pytest.mark.parametrize(
    "required", ["main.py", "gate", "sandbox", "Dockerfile", "version"]
)
def test_fixture_validator_rejects_each_missing_required_file(
    tmp_path: Path, required: str
) -> None:
    _, fixture = _project(tmp_path)
    (fixture / required).unlink()

    with pytest.raises(FixtureUpgradeError, match=required):
        validate_fixture(fixture)


def test_fixture_validator_rejects_broken_setup_symlink(tmp_path: Path) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "setup").unlink()
    (fixture / "setup").symlink_to("missing-setup")

    with pytest.raises(FixtureUpgradeError, match="setup.*executable"):
        validate_fixture(fixture)


@pytest.mark.parametrize("marker", ["", "host\ndocker\n"])
def test_fixture_validator_rejects_empty_or_multiline_sandbox_marker(
    tmp_path: Path, marker: str
) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "sandbox").write_text(marker)

    with pytest.raises(FixtureUpgradeError, match="sandbox"):
        validate_fixture(fixture)


def test_fixture_validator_rejects_prompt_outside_prompts_directory(
    tmp_path: Path,
) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "outside.md").write_text("outside\n")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run, execution_graph, runtime_node\n"
        "run = build_run(item=execution_graph(start='work', nodes=[runtime_node('work', '../outside.md')]))\n"
    )

    with pytest.raises(FixtureUpgradeError, match="outside prompts"):
        validate_fixture(fixture)


def test_fixture_validator_does_not_execute_gate(tmp_path: Path) -> None:
    project, fixture = _project(tmp_path)
    side_effect = project / "gate-ran"
    (fixture / "gate").write_text(f"#!/bin/sh\ntouch {side_effect}\n")

    validate_fixture(fixture)

    assert not side_effect.exists()


def test_fixture_validator_requires_an_item_execution_graph(tmp_path: Path) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "main.py").write_text(
        "from pycastle.graph import RunDefinition\n" "run = RunDefinition(item=None)\n"
    )

    with pytest.raises(FixtureUpgradeError, match="Item execution graph"):
        validate_fixture(fixture)


def test_fixture_validator_rejects_an_unknown_terminal(tmp_path: Path) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "main.py").write_text(
        "from pycastle.graph import ExecutionGraph, RunDefinition, RuntimeNode, Terminal\n"
        "run = RunDefinition(item=ExecutionGraph(start='work', nodes={\n"
        "    'work': RuntimeNode('work', 'plan.md', on_success=Terminal('UNKNOWN'))\n"
        "}))\n"
    )

    with pytest.raises(FixtureUpgradeError, match="Unknown Terminal"):
        validate_fixture(fixture)


def test_fixture_validator_rejects_a_node_key_name_mismatch(tmp_path: Path) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "main.py").write_text(
        "from pycastle.graph import ExecutionGraph, RunDefinition, RuntimeNode\n"
        "run = RunDefinition(item=ExecutionGraph(start='alias', nodes={\n"
        "    'alias': RuntimeNode('work', 'plan.md')\n"
        "}))\n"
    )

    with pytest.raises(FixtureUpgradeError, match="node key"):
        validate_fixture(fixture)


def test_fixture_validator_rejects_empty_direct_graph_fields(tmp_path: Path) -> None:
    _, fixture = _project(tmp_path)
    (fixture / "main.py").write_text(
        "from pycastle.graph import ExecutionGraph, RunDefinition, RuntimeNode\n"
        "run = RunDefinition(item=ExecutionGraph(start='', nodes={\n"
        "    '': RuntimeNode('', 'plan.md')\n"
        "}))\n"
    )

    with pytest.raises(FixtureUpgradeError, match="Item execution graph"):
        validate_fixture(fixture)


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM scratch\n# ARG PYCASTLE_HOST_UID\n# ARG PYCASTLE_HOST_GID\n",
        "FROM scratch\nRUN echo PYCASTLE_HOST_UID PYCASTLE_HOST_GID\n",
        "FROM scratch\nARG PYCASTLE_HOST_UID\n",
    ],
)
def test_012_manual_migration_rejects_nonsemantic_or_partial_args(
    tmp_path: Path, dockerfile: str
) -> None:
    project, fixture = _project(tmp_path, marker="0.1.1\n")
    (fixture / "Dockerfile").write_text(dockerfile)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )
    before = {
        p.relative_to(fixture): p.read_bytes()
        for p in fixture.rglob("*")
        if p.is_file()
    }

    with pytest.raises(FixtureUpgradeError, match="owner-authored.*PYCASTLE_HOST_UID"):
        upgrade_fixture(project, runner_version="0.1.2", migrations=MIGRATIONS)

    after = {
        p.relative_to(fixture): p.read_bytes()
        for p in fixture.rglob("*")
        if p.is_file()
    }
    assert after == before


def test_012_manual_migration_accepts_owner_authored_args_and_only_advances_marker(
    tmp_path: Path,
) -> None:
    project, fixture = _project(tmp_path, marker="0.1.1\n")
    dockerfile = "FROM scratch\narg PYCASTLE_HOST_UID=1000\nARG PYCASTLE_HOST_GID\n"
    (fixture / "Dockerfile").write_text(dockerfile)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "--amend", "--no-edit", "-q"], cwd=project, check=True
    )

    result = upgrade_fixture(project, runner_version="0.1.2", migrations=MIGRATIONS)

    assert dockerfile_declares_host_identity(fixture)
    assert (fixture / "Dockerfile").read_text() == dockerfile
    assert (fixture / "version").read_text() == "0.1.2\n"
    assert result.applied_versions == ()


def test_013_runner_patch_upgrades_actual_project_fixture_via_012_docker_identity_migration(
    tmp_path: Path,
) -> None:
    project, fixture = _project(tmp_path, marker="0.1.1\n")

    result = upgrade_fixture(project, runner_version="0.1.3", migrations=MIGRATIONS)

    definition = load_run(fixture)
    assert isinstance(definition.item.nodes["plan"], RuntimeNode)
    assert isinstance(definition.item.nodes["verify"], GateNode)
    assert (fixture / "version").read_text() == "0.1.3\n"
    assert result.applied_versions == ()
