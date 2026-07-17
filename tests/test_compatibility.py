"""Project fixture release-marker compatibility."""

from pathlib import Path

import pytest
from packaging.version import Version

from pycastle.compatibility import (
    FixtureCompatibilityError,
    FixtureCompatibilityStatus,
    check_fixture_compatibility,
    require_fixture_compatibility,
)


def _fixture(tmp_path: Path, marker: str | None) -> Path:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir(exist_ok=True)
    if marker is not None:
        (fixture / "version").write_text(marker)
    return fixture


@pytest.mark.parametrize(
    "marker",
    [None, "", "\n", "not-a-version\n", "1.0\nextra\n", "1.0\0\n"],
)
def test_missing_or_malformed_marker_is_an_invalid_fixture(
    tmp_path: Path, marker: str | None
) -> None:
    result = check_fixture_compatibility(_fixture(tmp_path, marker), "1.2.0")

    assert result.status is FixtureCompatibilityStatus.INVALID_FIXTURE
    assert "pycastle init" in result.message


def test_newer_fixture_is_an_unsupported_downgrade(tmp_path: Path) -> None:
    result = check_fixture_compatibility(_fixture(tmp_path, "2.0\n"), "1.2.0")

    assert result.status is FixtureCompatibilityStatus.UNSUPPORTED_DOWNGRADE
    assert result.fixture_version == Version("2.0")
    assert "newer" in result.message


def test_same_or_older_fixture_is_compatible_without_migrations(tmp_path: Path) -> None:
    for marker in ("1.2.0\n", "1.0\n", "0\n"):
        result = check_fixture_compatibility(
            _fixture(tmp_path, marker), "1.2.0", migration_versions=()
        )
        assert result.status is FixtureCompatibilityStatus.COMPATIBLE


def test_applicable_registered_migration_requires_future_upgrade_hook(
    tmp_path: Path,
) -> None:
    result = check_fixture_compatibility(
        _fixture(tmp_path, "1.0\n"), "1.2.0", migration_versions=("1.1",)
    )

    assert result.status is FixtureCompatibilityStatus.MIGRATION_REQUIRED
    assert "pycastle upgrade" in result.message


def test_migrations_outside_fixture_to_runner_interval_do_not_apply(
    tmp_path: Path,
) -> None:
    result = check_fixture_compatibility(
        _fixture(tmp_path, "1.1\n"),
        "1.2.0",
        migration_versions=("1.0", "1.1", "1.3"),
    )

    assert result.status is FixtureCompatibilityStatus.COMPATIBLE


def test_version_marker_that_is_not_a_regular_file_is_invalid(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, None)
    (fixture / "version").mkdir()

    result = check_fixture_compatibility(fixture, "1.2.0")

    assert result.status is FixtureCompatibilityStatus.INVALID_FIXTURE
    assert "cannot be read" in result.message


def test_requirement_raises_with_the_actionable_result(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, None)

    with pytest.raises(FixtureCompatibilityError, match="Invalid Project fixture"):
        require_fixture_compatibility(fixture, "1.2.0")
