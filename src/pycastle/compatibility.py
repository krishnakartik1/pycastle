"""Validate a Project fixture against the installed PyCastle release."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import __version__

VERSION_MARKER = "version"


class FixtureCompatibilityStatus(Enum):
    """Stable outcomes consumable by Run and a future ``pycastle doctor``."""

    COMPATIBLE = "compatible"
    INVALID_FIXTURE = "invalid_fixture"
    UNSUPPORTED_DOWNGRADE = "unsupported_downgrade"
    MIGRATION_REQUIRED = "migration_required"


@dataclass(frozen=True)
class FixtureCompatibility:
    """The compatibility outcome without prescribing a CLI presentation."""

    status: FixtureCompatibilityStatus
    runner_version: Version
    fixture_version: Version | None
    message: str

    @property
    def compatible(self) -> bool:
        return self.status is FixtureCompatibilityStatus.COMPATIBLE


class FixtureCompatibilityError(Exception):
    """Raised when a Run cannot safely use its Project fixture."""

    def __init__(self, result: FixtureCompatibility) -> None:
        super().__init__(result.message)
        self.result = result


def _invalid(runner: Version, detail: str) -> FixtureCompatibility:
    return FixtureCompatibility(
        FixtureCompatibilityStatus.INVALID_FIXTURE,
        runner,
        None,
        f"Invalid Project fixture: {detail} Run `pycastle init` in a fresh "
        "project; markerless fixture adoption is not supported.",
    )


def check_fixture_compatibility(
    fixture_dir: Path,
    runner_version: str = __version__,
    *,
    migration_versions: Iterable[str] = (),
) -> FixtureCompatibility:
    """Inspect ``fixture_dir/version`` and return its compatibility outcome.

    A migration version denotes the release whose migration upgrades a fixture
    to that release. The registry is intentionally empty in this ticket, but the
    parameter fixes the boundary the upgrade implementation will populate.
    """
    runner = Version(runner_version)
    marker = fixture_dir / VERSION_MARKER
    try:
        raw = marker.read_text()
    except FileNotFoundError:
        return _invalid(runner, f"{marker} is missing.")
    except OSError as exc:
        return _invalid(runner, f"{marker} cannot be read: {exc}.")

    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        return _invalid(runner, f"{marker} must contain one valid release version.")
    try:
        fixture = Version(lines[0].strip())
    except InvalidVersion:
        return _invalid(runner, f"{marker} contains malformed version {lines[0]!r}.")

    if fixture > runner:
        return FixtureCompatibility(
            FixtureCompatibilityStatus.UNSUPPORTED_DOWNGRADE,
            runner,
            fixture,
            f"Unsupported downgrade: Project fixture {fixture} is newer than "
            f"installed PyCastle {runner}. Install PyCastle {fixture} or newer.",
        )

    migrations = sorted(Version(item) for item in migration_versions)
    applicable = [target for target in migrations if fixture < target <= runner]
    if applicable:
        return FixtureCompatibility(
            FixtureCompatibilityStatus.MIGRATION_REQUIRED,
            runner,
            fixture,
            f"Project fixture {fixture} requires migration to run with PyCastle "
            f"{runner}. Run `pycastle upgrade` (available in a future release).",
        )

    return FixtureCompatibility(
        FixtureCompatibilityStatus.COMPATIBLE,
        runner,
        fixture,
        f"Project fixture {fixture} is compatible with PyCastle {runner}.",
    )


def require_fixture_compatibility(
    fixture_dir: Path,
    runner_version: str = __version__,
    *,
    migration_versions: Iterable[str] = (),
) -> FixtureCompatibility:
    """Return a compatible result or raise with its actionable diagnostic."""
    result = check_fixture_compatibility(
        fixture_dir, runner_version, migration_versions=migration_versions
    )
    if not result.compatible:
        raise FixtureCompatibilityError(result)
    return result
