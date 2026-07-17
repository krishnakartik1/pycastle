"""Forward-only Project fixture migration registry."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

from .upgrade_errors import FixtureUpgradeError

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


_HOST_IDENTITY_ARGS = ("PYCASTLE_HOST_UID", "PYCASTLE_HOST_GID")


def dockerfile_declares_host_identity(fixture: Path) -> bool:
    """Recognize semantic Dockerfile ARG instructions, not comments or prose."""
    try:
        text = (fixture / "Dockerfile").read_text()
    except OSError:
        return False
    declared: set[str] = set()
    for line in text.splitlines():
        match = re.match(
            r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=.*)?$",
            line,
            re.IGNORECASE,
        )
        if match:
            declared.add(match.group(1))
    return all(name in declared for name in _HOST_IDENTITY_ARGS)


def _require_owner_host_identity_adoption(_fixture: Path) -> None:
    raise FixtureUpgradeError(
        "PyCastle 0.1.2 requires an owner-authored .pycastle/Dockerfile change. "
        "Declare both `ARG PYCASTLE_HOST_UID` and `ARG PYCASTLE_HOST_GID`, then "
        "use them to make the image-declared non-root user's numeric UID/GID "
        "compatible with host-owned worktrees. Review and commit that Dockerfile "
        "change, then rerun `pycastle upgrade` from a clean checkout. PyCastle "
        "did not modify the Project fixture."
    )


MIGRATIONS: tuple[FixtureMigration, ...] = (
    FixtureMigration(
        "0.1.2",
        dockerfile_declares_host_identity,
        _require_owner_host_identity_adoption,
        dockerfile_declares_host_identity,
    ),
)
