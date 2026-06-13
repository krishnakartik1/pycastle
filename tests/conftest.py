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
