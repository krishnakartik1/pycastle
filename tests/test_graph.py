"""The Builder API assembles a graph and the executor runs it in order."""

from __future__ import annotations

from pathlib import Path

from pycastle import graph as g
from pycastle.graph import GraphExecutor, PhaseGraph, load_graph
from pycastle.runtime import StubRuntime


def test_builder_assembles_phases_in_order() -> None:
    built = g.build().phase("plan", prompt="p.md").phase("do", prompt="d.md").build()
    assert isinstance(built, PhaseGraph)
    assert [p.name for p in built.phases] == ["plan", "do"]


def test_load_graph_reads_module_level_graph(fixture_dir: Path) -> None:
    loaded = load_graph(fixture_dir)
    assert [p.name for p in loaded.phases] == ["implement"]


def test_executor_runs_each_phase_through_the_runtime(fixture_dir: Path) -> None:
    loaded = load_graph(fixture_dir)
    executor = GraphExecutor(StubRuntime(), fixture_dir=fixture_dir)

    results = executor.execute(loaded, cwd=fixture_dir)

    assert [r.phase for r in results] == ["implement"]
    assert (fixture_dir / "PYCASTLE_STUB.md").is_file()
