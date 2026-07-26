"""Forward-only Project fixture migration registry."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

from .fixture_validation import safe_prompt_path
from .graph import ExecutionGraph, ItemDefinition, RuntimeSelection, load_run
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


def fixture_declares_project_owned_item_selection(fixture: Path) -> bool:
    """Return whether the owner supplied the complete Item selection contract."""
    try:
        definition = load_run(fixture)
        item = definition.item
    except Exception:
        # Fixture Python is owner-authored; any load failure means the target
        # contract is not yet complete, while interrupts still propagate.
        return False
    return (
        isinstance(item, ItemDefinition)
        and isinstance(item.selection, RuntimeSelection)
        and isinstance(item.graph, ExecutionGraph)
        and safe_prompt_path(fixture / "prompts", item.selection.prompt) is not None
    )


def _require_owner_item_selection_adoption(_fixture: Path) -> None:
    raise FixtureUpgradeError(
        "PyCastle 0.1.3 requires an owner-authored Project fixture migration. "
        "Add and review a project-owned selection prompt under "
        "`.pycastle/prompts/`, then wrap the existing Item execution graph in "
        "an Item definition with `build_item`, pairing that graph with "
        "`runtime_selection` for the new prompt. Review and commit both "
        "changes, then rerun `pycastle upgrade` from a clean checkout. "
        "PyCastle did not modify the Project fixture."
    )


MIGRATIONS: tuple[FixtureMigration, ...] = (
    FixtureMigration(
        "0.1.2",
        dockerfile_declares_host_identity,
        _require_owner_host_identity_adoption,
        dockerfile_declares_host_identity,
    ),
    FixtureMigration(
        "0.1.3",
        fixture_declares_project_owned_item_selection,
        _require_owner_item_selection_adoption,
        fixture_declares_project_owned_item_selection,
    ),
)
