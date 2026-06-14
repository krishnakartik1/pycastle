"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """A minimal .pycastle/ fixture: an implement-only graph plus its prompt.

    Edges default to terminals, so this is the canonical non-default flow from
    ADR-0004 — ``build(start='implement', phases=[phase('implement', ...)])`` —
    walking straight from implement to DONE on success.
    """
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, phase\n"
        "graph = build(start='implement', "
        "phases=[phase('implement', 'implement.md')])\n"
    )
    (fixture / "prompts" / "implement.md").write_text("# Implement\nDo the work.\n")
    return fixture


@pytest.fixture
def three_phase_fixture_dir(tmp_path: Path) -> Path:
    """A .pycastle/ fixture wiring the default plan → implement → review graph.

    Mirrors the shape of the repo's own ``.pycastle/main.py`` so tests can
    exercise the full default flow end to end and assert phase ordering. The
    walk runs plan → implement → review → DONE on success; every failure edge
    routes to HUMAN.
    """
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle.graph import DONE, HUMAN, build, phase\n"
        "graph = build(\n"
        "    start='plan',\n"
        "    phases=[\n"
        "        phase('plan', 'plan.md', on_success='implement', on_failure=HUMAN),\n"
        "        phase('implement', 'implement.md', on_success='review', "
        "on_failure=HUMAN),\n"
        "        phase('review', 'review.md', on_success=DONE, on_failure=HUMAN),\n"
        "    ],\n"
        ")\n"
    )
    (fixture / "prompts" / "plan.md").write_text("# Plan\nWork out the approach.\n")
    (fixture / "prompts" / "implement.md").write_text("# Implement\nDo the work.\n")
    (fixture / "prompts" / "review.md").write_text(
        "# Review\nTest edge cases and commit improvements.\n"
    )
    return fixture
