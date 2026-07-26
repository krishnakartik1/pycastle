"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """A minimal .pycastle/ fixture: an implement-only graph plus its prompt.

    Edges default to terminals, so this is the canonical non-default flow from
    ADR-0004 — ``execution_graph(start='implement', nodes=[runtime_node('implement', ...)])`` —
    walking straight from implement to DONE on success.
    """
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item, build_run, execution_graph, "
        "runtime_node, runtime_selection)\n"
        "run = build_run(item=build_item("
        "selection=runtime_selection('select-item.md'), "
        "graph=execution_graph(start='implement', "
        "nodes=[runtime_node('implement', 'implement.md')])))\n"
    )
    (fixture / "prompts" / "select-item.md").write_text("# Select\nChoose an Item.\n")
    (fixture / "prompts" / "implement.md").write_text("# Implement\nDo the work.\n")
    return fixture


@pytest.fixture
def three_phase_fixture_dir(tmp_path: Path) -> Path:
    """A .pycastle/ fixture wiring the default plan → implement → review graph.

    Mirrors the shape of the repo's own ``.pycastle/main.py`` so tests can
    exercise the full default flow end to end and assert node ordering. The
    walk runs plan → implement → review → DONE on success; every failure edge
    routes to HUMAN.
    """
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle.graph import (DONE, HUMAN, build_item, build_run, "
        "execution_graph, runtime_node, runtime_selection)\n"
        "run = build_run(item=build_item(\n"
        "selection=runtime_selection('select-item.md'),\n"
        "graph=execution_graph(\n"
        "    start='plan',\n"
        "    nodes=[\n"
        "        runtime_node('plan', 'plan.md', on_success='implement', on_failure=HUMAN),\n"
        "        runtime_node('implement', 'implement.md', on_success='review', "
        "on_failure=HUMAN),\n"
        "        runtime_node('review', 'review.md', on_success=DONE, on_failure=HUMAN),\n"
        "    ],\n"
        ")))\n"
    )
    (fixture / "prompts" / "select-item.md").write_text("# Select\nChoose an Item.\n")
    (fixture / "prompts" / "plan.md").write_text("# Plan\nWork out the approach.\n")
    (fixture / "prompts" / "implement.md").write_text("# Implement\nDo the work.\n")
    (fixture / "prompts" / "review.md").write_text(
        "# Review\nTest edge cases and commit improvements.\n"
    )
    return fixture
