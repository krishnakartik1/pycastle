"""Transactional, forward-only Project fixture upgrades."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .compatibility import FixtureCompatibilityStatus, check_fixture_compatibility
from .graph import load_run
from .migrations import MIGRATIONS, FixtureMigration

FixtureWriter = Callable[[Path, Path], None]
REQUIRED_FILES = ("main.py", "gate", "sandbox", "Dockerfile", "version")


class FixtureUpgradeError(Exception):
    """Raised when an upgrade cannot be completed without risking the fixture."""


@dataclass(frozen=True)
class FixtureUpgradeResult:
    """Summary of a successful upgrade attempt."""

    fixture_version: str
    runner_version: str
    applied_versions: tuple[str, ...]
    marker_updated: bool = False

    @property
    def changed(self) -> bool:
        return self.marker_updated


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def validate_fixture(fixture_dir: Path) -> None:
    """Validate static runner/fixture invariants without executing project code."""
    for relative in REQUIRED_FILES:
        path = fixture_dir / relative
        if not _regular_file(path):
            raise FixtureUpgradeError(
                f"Required fixture file {path} is missing or unsafe."
            )

    for hook_name in ("gate", "setup"):
        hook = fixture_dir / hook_name
        # ``Path.exists`` is false for a broken symlink.  Such a path is still
        # a present, unsafe customized Setup hook rather than an absent optional
        # hook, so only skip it when no directory entry exists at all.
        if hook_name == "setup" and not hook.exists() and not hook.is_symlink():
            continue
        if not _regular_file(hook) or not os.access(hook, os.X_OK):
            raise FixtureUpgradeError(
                f"Fixture hook {hook} must be an executable file."
            )

    sandbox = (fixture_dir / "sandbox").read_text().splitlines()
    if len(sandbox) != 1 or sandbox[0].strip() not in {"host", "docker"}:
        raise FixtureUpgradeError(
            "Fixture sandbox marker must be exactly host or docker."
        )

    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        definition = load_run(fixture_dir)
    except Exception as exc:
        raise FixtureUpgradeError(
            f"Could not load the fixture Run definition: {exc}"
        ) from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode
    scoped_graphs = [("Item node", definition.item)]
    scoped_graphs.extend(
        (scope, graph)
        for scope, graph in (
            ("before-Run node", definition.before),
            ("after-Run node", definition.after),
        )
        if graph is not None
    )
    for scope, graph in scoped_graphs:
        for node in graph.nodes.values():
            prompts_dir = (fixture_dir / "prompts").resolve()
            prompt = (prompts_dir / node.prompt).resolve()
            try:
                prompt.relative_to(prompts_dir)
            except ValueError as exc:
                raise FixtureUpgradeError(
                    f"{scope} {node.name!r} references prompt outside prompts/: "
                    f"{node.prompt}"
                ) from exc
            if not _regular_file(prompt):
                raise FixtureUpgradeError(
                    f"{scope} {node.name!r} references missing prompt {node.prompt}."
                )


def _ensure_clean(project_dir: Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_dir,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise FixtureUpgradeError(
            f"Could not inspect the Git worktree: {(result.stderr or '').strip()}"
        )
    if result.stdout:
        raise FixtureUpgradeError(
            "Refusing to upgrade a dirty Git worktree; commit or stash all changes first."
        )


def _default_writer(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.upgrade-{uuid.uuid4().hex}")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _entries(root: Path) -> dict[Path, Path]:
    return {
        path.relative_to(root): path
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }


def _same_file(left: Path, right: Path) -> bool:
    if left.is_symlink() or right.is_symlink():
        return (
            left.is_symlink()
            and right.is_symlink()
            and os.readlink(left) == os.readlink(right)
        )
    return left.read_bytes() == right.read_bytes() and stat.S_IMODE(
        left.stat().st_mode
    ) == stat.S_IMODE(right.stat().st_mode)


def _write_candidate(
    candidate: Path, fixture: Path, writer: FixtureWriter, marker: str
) -> None:
    candidate_entries = _entries(candidate)
    fixture_entries = _entries(fixture)
    marker_path = Path(marker)

    # Remove paths dropped by a migration and write every changed path first.
    for relative in sorted(
        fixture_entries.keys() - candidate_entries.keys(), reverse=True
    ):
        if relative != marker_path:
            fixture_entries[relative].unlink()
    for relative, source in sorted(candidate_entries.items()):
        if relative == marker_path:
            continue
        destination = fixture / relative
        if relative not in fixture_entries or not _same_file(source, destination):
            writer(source, destination)

    # The release marker is the commit point and is always the final write.
    writer(candidate / marker, fixture / marker)


def upgrade_fixture(
    project_dir: Path,
    *,
    runner_version: str = __version__,
    migrations: Iterable[FixtureMigration] = MIGRATIONS,
    writer: FixtureWriter = _default_writer,
) -> FixtureUpgradeResult:
    """Build, validate, and install a complete candidate fixture transactionally."""
    project_dir = project_dir.resolve()
    fixture = project_dir / ".pycastle"
    ordered = tuple(sorted(migrations, key=lambda item: item.version))
    versions = tuple(str(item.version) for item in ordered)
    compatibility = check_fixture_compatibility(
        fixture, runner_version, migration_versions=versions
    )
    if (
        compatibility.status is not FixtureCompatibilityStatus.COMPATIBLE
        and compatibility.status is not FixtureCompatibilityStatus.MIGRATION_REQUIRED
    ):
        raise FixtureUpgradeError(compatibility.message)
    _ensure_clean(project_dir)
    if compatibility.status is FixtureCompatibilityStatus.COMPATIBLE:
        return FixtureUpgradeResult(
            str(compatibility.fixture_version),
            str(compatibility.runner_version),
            (),
            False,
        )

    assert compatibility.fixture_version is not None
    relevant = tuple(
        migration
        for migration in ordered
        if compatibility.fixture_version
        < migration.version
        <= compatibility.runner_version
    )
    applied: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pycastle-upgrade-") as temporary:
        candidate = Path(temporary) / ".pycastle"
        shutil.copytree(fixture, candidate, symlinks=True)
        try:
            for migration in relevant:
                if not migration.target_condition(candidate):
                    migration.transform(candidate)
                    applied.append(str(migration.version))
                if not migration.target_condition(candidate):
                    raise FixtureUpgradeError(
                        f"Migration {migration.version} did not reach its target condition."
                    )
                if not migration.validate(candidate):
                    raise FixtureUpgradeError(
                        f"Migration {migration.version} validation failed."
                    )
            (candidate / "version").write_text(f"{compatibility.runner_version}\n")
            validate_fixture(candidate)
        except FixtureUpgradeError:
            raise
        except Exception as exc:
            raise FixtureUpgradeError(
                f"Could not build the candidate fixture: {exc}"
            ) from exc

        backup = project_dir / f".pycastle-upgrade-backup-{uuid.uuid4().hex}"
        shutil.copytree(fixture, backup, symlinks=True)
        try:
            _write_candidate(candidate, fixture, writer, "version")
        except Exception as exc:
            failed = project_dir / f".pycastle-upgrade-failed-{uuid.uuid4().hex}"
            try:
                os.replace(fixture, failed)
                os.replace(backup, fixture)
                shutil.rmtree(failed)
            except Exception as rollback_exc:
                raise FixtureUpgradeError(
                    f"Upgrade write failed ({exc}) and rollback failed ({rollback_exc})."
                ) from rollback_exc
            raise FixtureUpgradeError(
                f"Upgrade write failed and was rolled back: {exc}"
            ) from exc
        finally:
            if backup.exists():
                shutil.rmtree(backup)

    return FixtureUpgradeResult(
        str(compatibility.fixture_version),
        str(compatibility.runner_version),
        tuple(applied),
        True,
    )


__all__ = [
    "FixtureMigration",
    "FixtureUpgradeError",
    "FixtureUpgradeResult",
    "upgrade_fixture",
    "validate_fixture",
]
