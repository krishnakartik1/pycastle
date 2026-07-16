from pathlib import Path

import pytest

from pycastle.graph import (
    DONE,
    HUMAN,
    ExecutionGraph,
    GateNode,
    NodeOutcome,
    RuntimeNode,
    build_run,
    execution_graph,
    gate_node,
    load_run,
    runtime_node,
    walk_execution_graph,
)


def test_mixed_graph_has_explicit_local_edges() -> None:
    graph = execution_graph(
        start="work",
        nodes=[
            gate_node("verify", on_failure="work"),
            runtime_node("work", "work.md", on_success="verify"),
        ],
    )
    assert isinstance(graph, ExecutionGraph)
    assert isinstance(graph.nodes["work"], RuntimeNode)
    assert isinstance(graph.nodes["verify"], GateNode)
    assert graph.nodes["verify"].on_success is DONE
    assert graph.nodes["work"].on_failure is HUMAN


@pytest.mark.parametrize("bad", ["missing", "", None])
def test_graph_rejects_invalid_start(bad: object) -> None:
    with pytest.raises(ValueError, match="declared node"):
        execution_graph(start=bad, nodes=[gate_node("gate")])  # type: ignore[arg-type]


def test_graph_rejects_duplicate_and_unknown_destinations() -> None:
    with pytest.raises(ValueError, match="Duplicate node"):
        execution_graph(start="x", nodes=[gate_node("x"), gate_node("x")])
    with pytest.raises(ValueError, match="declared node"):
        execution_graph(
            start="x", nodes=[runtime_node("x", "x.md", on_success="missing")]
        )


def test_run_requires_item_and_allows_optional_scope_graphs() -> None:
    item = execution_graph(start="item", nodes=[gate_node("item")])
    after = execution_graph(start="after", nodes=[gate_node("after")])
    run = build_run(item=item, after=after)
    assert run.item is item and run.before is None and run.after is after


def test_cycle_attempts_ten_visits_and_stops_before_eleventh() -> None:
    graph = execution_graph(start="gate", nodes=[gate_node("gate", on_success="gate")])
    invoked: list[int] = []

    def visit(entry):
        invoked.append(entry.ordinal)
        return NodeOutcome(True, {"ordinal": entry.ordinal})

    result = walk_execution_graph(graph, visit)
    assert result.terminal is HUMAN
    assert invoked == list(range(1, 11))


def test_walk_passes_only_immediate_predecessor_evidence() -> None:
    graph = execution_graph(
        start="one",
        nodes=[
            gate_node("one", on_success="two"),
            runtime_node("two", "two.md"),
        ],
    )
    seen = []

    def visit(entry):
        seen.append(entry.predecessor)
        return NodeOutcome(True, entry.node.name)

    walk_execution_graph(graph, visit)
    assert seen == [None, "one"]


def test_load_run_reads_new_fixture_vocabulary(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,gate_node\n"
        "run=build_run(item=execution_graph(start='g',nodes=[gate_node('g')]))\n"
    )
    assert isinstance(load_run(fixture).item.nodes["g"], GateNode)
