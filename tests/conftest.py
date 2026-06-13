"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    """A minimal .pycastle/ fixture: a single-phase graph plus its prompt."""
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle import graph as g\n"
        "graph = g.build().phase('implement', prompt='implement.md').build()\n"
    )
    (fixture / "prompts" / "implement.md").write_text("# Implement\nDo the work.\n")
    return fixture


@pytest.fixture
def three_phase_fixture_dir(tmp_path: Path) -> Path:
    """A .pycastle/ fixture wiring the default plan → implement → review graph.

    Mirrors the shape of the repo's own ``.pycastle/main.py`` so tests can
    exercise the full default flow end to end and assert phase ordering.
    """
    fixture = tmp_path / ".pycastle"
    (fixture / "prompts").mkdir(parents=True)
    (fixture / "main.py").write_text(
        "from pycastle import graph as g\n"
        "graph = (\n"
        "    g.build()\n"
        "    .phase('plan', prompt='plan.md')\n"
        "    .phase('implement', prompt='implement.md')\n"
        "    .phase('review', prompt='review.md')\n"
        "    .build()\n"
        ")\n"
    )
    (fixture / "prompts" / "plan.md").write_text("# Plan\nWork out the approach.\n")
    (fixture / "prompts" / "implement.md").write_text("# Implement\nDo the work.\n")
    (fixture / "prompts" / "review.md").write_text(
        "# Review\nTest edge cases and commit improvements.\n"
    )
    return fixture
