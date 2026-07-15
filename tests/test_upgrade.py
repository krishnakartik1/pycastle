"""Transactional forward migration of Project fixtures."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pycastle.upgrade import (
    FixtureMigration,
    FixtureUpgradeError,
    upgrade_fixture,
    validate_fixture,
)


def _project(tmp_path: Path, marker: str = "1.0\n") -> tuple[Path, Path]:
    project = tmp_path / "project"
    fixture = project / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, phase\n"
        "graph = build(start='work', phases=[phase('work', 'work.md')])\n"
    )
    (fixture / "prompts" / "work.md").write_text("work\n")
    (fixture / "gate").write_text("#!/bin/sh\nexit 0\n")
    (fixture / "setup").write_text("#!/bin/sh\nexit 0\n")
    (fixture / "gate").chmod(0o755)
    (fixture / "setup").chmod(0o755)
    (fixture / "sandbox").write_text("host\n")
    (fixture / "Dockerfile").write_text("FROM scratch\n")
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
        (lambda f: (f / "prompts" / "work.md").unlink(), "work.md"),
        (lambda f: (f / "main.py").write_text("raise ValueError('broken')\n"), "load"),
    ],
)
def test_fixture_validator_checks_each_builtin_invariant(
    tmp_path: Path, mutate: object, message: str
) -> None:
    _, fixture = _project(tmp_path)
    mutate(fixture)  # type: ignore[operator]

    with pytest.raises(FixtureUpgradeError, match=message):
        validate_fixture(fixture)
