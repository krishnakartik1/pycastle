"""Forward-only Project fixture migration registry.

The registry is deliberately empty until a release introduces a genuine runner/
fixture contract change. Improved scaffold defaults are not migrations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

FixtureCheck = Callable[[Path], bool]
FixtureTransform = Callable[[Path], None]


@dataclass(frozen=True)
class FixtureMigration:
    """One narrow, idempotent migration to a release contract."""

    target_release: str
    target_condition: FixtureCheck
    transform: FixtureTransform
    validate: FixtureCheck

    @property
    def version(self) -> Version:
        """Return the normalized target release used for registry ordering."""
        return Version(self.target_release)


MIGRATIONS: tuple[FixtureMigration, ...] = ()
