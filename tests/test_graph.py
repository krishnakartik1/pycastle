from pathlib import Path

import pytest

import pycastle.graph as graph_module
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


@pytest.mark.parametrize("name", ["", None, 0])
def test_graph_rejects_invalid_node_names(name: object) -> None:
    with pytest.raises(ValueError, match="non-empty strings"):
        execution_graph(start="gate", nodes=[GateNode(name)])  # type: ignore[arg-type]


@pytest.mark.parametrize("prompt", ["", None, 0])
def test_graph_rejects_invalid_runtime_prompts(prompt: object) -> None:
    with pytest.raises(ValueError, match="prompt must be a non-empty string"):
        execution_graph(
            start="work",
            nodes=[RuntimeNode("work", prompt)],  # type: ignore[arg-type]
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


def test_failure_follows_only_declared_failure_edge() -> None:
    graph = execution_graph(
        start="gate",
        nodes=[
            gate_node("gate", on_success="wrong", on_failure="repair"),
            runtime_node("wrong", "wrong.md"),
            runtime_node("repair", "repair.md"),
        ],
    )
    visited = []

    def visit(entry):
        visited.append(entry.node.name)
        return NodeOutcome(entry.node.name != "gate")

    assert walk_execution_graph(graph, visit).terminal is DONE
    assert visited == ["gate", "repair"]


def test_cycle_limit_is_per_node_and_replaces_predecessor_each_edge() -> None:
    graph = execution_graph(
        start="gate",
        nodes=[
            gate_node("gate", on_failure="repair"),
            runtime_node("repair", "repair.md", on_success="gate"),
        ],
    )
    seen = []

    def visit(entry):
        seen.append((entry.node.name, entry.ordinal, entry.predecessor))
        return NodeOutcome(
            entry.node.name == "repair", f"{entry.node.name}-{entry.ordinal}"
        )

    result = walk_execution_graph(graph, visit)
    assert result.terminal is HUMAN
    assert len(seen) == 20
    assert seen[-2:] == [("gate", 10, "repair-9"), ("repair", 10, "gate-10")]


def test_load_run_reads_new_fixture_vocabulary(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,gate_node\n"
        "run=build_run(item=execution_graph(start='g',nodes=[gate_node('g')]))\n"
    )
    assert isinstance(load_run(fixture).item.nodes["g"], GateNode)


def test_legacy_graph_surface_is_absent() -> None:
    for name in (
        "Phase",
        "PhaseGraph",
        "PhaseResult",
        "phase",
        "build",
        "load_graph",
        "GraphExecutor",
        "WalkResult",
        "DEFAULT_VISIT_CAP",
    ):
        assert not hasattr(graph_module, name)
