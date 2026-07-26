"""Shared structural validation for a target-release Project fixture."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from .graph import (
    ExecutionGraph,
    GateNode,
    ItemDefinition,
    RunDefinition,
    RuntimeNode,
    RuntimeSelection,
    execution_graph,
    load_run,
)


class ProjectFixtureValidationError(ValueError):
    """A Project fixture does not satisfy the structural contract."""


def _regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def validate_project_fixture_structure(fixture_dir: Path) -> RunDefinition:
    """Validate and return one complete target-release Run definition."""
    main_py = fixture_dir / "main.py"
    if not _regular_file(main_py):
        raise ProjectFixtureValidationError(
            f"Invalid Project fixture main file: {main_py}."
        )

    previous_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        try:
            definition = load_run(fixture_dir)
        except Exception as exc:
            raise ProjectFixtureValidationError(
                f"Could not load the fixture Run definition: {exc}"
            ) from exc
    finally:
        sys.dont_write_bytecode = previous_bytecode

    if not isinstance(definition.item, ItemDefinition):
        raise ProjectFixtureValidationError("Invalid Item definition.")
    if not isinstance(definition.item.selection, RuntimeSelection):
        raise ProjectFixtureValidationError("Invalid Item selection policy.")
    if not isinstance(definition.item.graph, ExecutionGraph):
        raise ProjectFixtureValidationError("Invalid Item execution graph.")

    prompt_root = fixture_dir / "prompts"
    if prompt_root.is_symlink() or not prompt_root.is_dir():
        raise ProjectFixtureValidationError("Invalid prompts directory.")
    prompts = prompt_root.resolve()
    selection_candidate = prompts / definition.item.selection.prompt
    try:
        selection_parts = selection_candidate.relative_to(prompts).parts
        selection_path = selection_candidate.resolve()
    except (OSError, ValueError) as exc:
        raise ProjectFixtureValidationError(
            "Item selection policy references a prompt outside prompts/."
        ) from exc
    selection_has_symlink = any(
        (prompts.joinpath(*selection_parts[:index])).is_symlink()
        for index in range(1, len(selection_parts) + 1)
    )
    if (
        prompts not in selection_path.parents
        or not selection_path.is_file()
        or selection_has_symlink
    ):
        raise ProjectFixtureValidationError(
            "Item selection policy references a missing or unsafe prompt "
            f"{definition.item.selection.prompt}."
        )
    for scope, graph in (
        ("Before-Run execution graph", definition.before),
        ("Item execution graph", definition.item.graph),
        ("After-Run execution graph", definition.after),
    ):
        if graph is None:
            continue
        if not isinstance(graph, ExecutionGraph) or not isinstance(graph.nodes, dict):
            raise ProjectFixtureValidationError(f"Invalid {scope}.")
        for key, node in graph.nodes.items():
            if not isinstance(node, RuntimeNode | GateNode):
                raise ProjectFixtureValidationError(f"Invalid node in {scope}.")
            if key != node.name:
                raise ProjectFixtureValidationError(
                    f"{scope} node key {key!r} does not match node name {node.name!r}."
                )
        try:
            execution_graph(start=graph.start, nodes=list(graph.nodes.values()))
        except (TypeError, ValueError) as exc:
            raise ProjectFixtureValidationError(f"Invalid {scope}: {exc}.") from exc
        for node in graph.nodes.values():
            if isinstance(node, GateNode):
                continue
            candidate = prompts / node.prompt
            try:
                relative_parts = candidate.relative_to(prompts).parts
            except ValueError as exc:
                raise ProjectFixtureValidationError(
                    f"{scope} Runtime node {node.name!r} references prompt outside "
                    f"prompts/: {node.prompt}."
                ) from exc
            try:
                path = candidate.resolve()
            except OSError as exc:
                raise ProjectFixtureValidationError(
                    f"{scope} Runtime node {node.name!r} references an unsafe prompt: "
                    f"{node.prompt}."
                ) from exc
            has_symlink = any(
                (prompts.joinpath(*relative_parts[:index])).is_symlink()
                for index in range(1, len(relative_parts) + 1)
            )
            if prompts not in path.parents:
                raise ProjectFixtureValidationError(
                    f"{scope} Runtime node {node.name!r} references prompt outside "
                    f"prompts/: {node.prompt}."
                )
            if not path.is_file() or has_symlink:
                raise ProjectFixtureValidationError(
                    f"{scope} Runtime node {node.name!r} references missing or unsafe "
                    f"prompt {node.prompt}."
                )

    for name in ("setup", "gate"):
        path = fixture_dir / name
        if not _regular_file(path) or not path.stat().st_mode & 0o111:
            raise ProjectFixtureValidationError(f"Invalid {name} executable.")
        try:
            first_line = path.read_bytes().splitlines()[0].decode("utf-8")
            words = shlex.split(first_line[2:]) if first_line.startswith("#!") else []
        except (IndexError, OSError, UnicodeDecodeError, ValueError):
            words = []
        if not words or not words[0].startswith("/") or len(words) > 2:
            raise ProjectFixtureValidationError(f"Invalid {name} shebang.")

    return definition


__all__ = [
    "ProjectFixtureValidationError",
    "validate_project_fixture_structure",
]
