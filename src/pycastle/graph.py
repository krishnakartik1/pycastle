"""Project-owned Execution graph declarations and bounded graph walking."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

from .models import RuntimeResult
from .runtime import AgentCrashError, Runtime


@dataclass(frozen=True)
class Terminal:
    name: str

    def __repr__(self) -> str:
        return self.name


DONE = Terminal("DONE")
HUMAN = Terminal("HUMAN")
MAX_NODE_VISITS = 10
DEFAULT_VISIT_CAP = MAX_NODE_VISITS


@dataclass(frozen=True)
class RuntimeNode:
    name: str
    prompt: str
    on_success: str | Terminal = DONE
    on_failure: str | Terminal = HUMAN


@dataclass(frozen=True)
class GateNode:
    name: str
    on_success: str | Terminal = DONE
    on_failure: str | Terminal = HUMAN


ExecutionNode: TypeAlias = RuntimeNode | GateNode


@dataclass(frozen=True)
class ExecutionGraph:
    start: str
    nodes: dict[str, ExecutionNode] = field(default_factory=dict)

    @property
    def phases(self) -> dict[str, ExecutionNode]:
        """Temporary internal bridge for the surrounding Run lifecycle."""
        return self.nodes


@dataclass(frozen=True)
class RunDefinition:
    item: ExecutionGraph
    before: ExecutionGraph | None = None
    after: ExecutionGraph | None = None


def runtime_node(
    name: str,
    prompt: str,
    *,
    on_success: str | Terminal = DONE,
    on_failure: str | Terminal = HUMAN,
) -> RuntimeNode:
    return RuntimeNode(name, prompt, on_success, on_failure)


def gate_node(
    name: str,
    *,
    on_success: str | Terminal = DONE,
    on_failure: str | Terminal = HUMAN,
) -> GateNode:
    return GateNode(name, on_success, on_failure)


def execution_graph(*, start: str, nodes: Sequence[ExecutionNode]) -> ExecutionGraph:
    if not isinstance(start, str) or not start:
        raise ValueError("start must name a declared node")
    by_name: dict[str, ExecutionNode] = {}
    for node in nodes:
        if not isinstance(node, RuntimeNode | GateNode):
            raise TypeError("execution graph nodes must be RuntimeNode or GateNode")
        if not isinstance(node.name, str) or not node.name:
            raise ValueError("Execution node names must be non-empty strings")
        if isinstance(node, RuntimeNode) and (
            not isinstance(node.prompt, str) or not node.prompt
        ):
            raise ValueError(
                f"Runtime node {node.name!r} prompt must be a non-empty string"
            )
        if node.name in by_name:
            raise ValueError(f"Duplicate node name: {node.name!r}")
        by_name[node.name] = node
    if start not in by_name:
        raise ValueError(f"start={start!r} is not a declared node")
    for node in by_name.values():
        for edge, target in (
            ("on_success", node.on_success),
            ("on_failure", node.on_failure),
        ):
            if isinstance(target, Terminal):
                if target not in (DONE, HUMAN):
                    raise ValueError(f"Unknown Terminal on {node.name!r} {edge}")
            elif not isinstance(target, str) or target not in by_name:
                raise ValueError(
                    f"Node {node.name!r} {edge}={target!r} is neither a "
                    "declared node nor a Terminal"
                )
    return ExecutionGraph(start, by_name)


def build_run(
    *,
    item: ExecutionGraph,
    before: ExecutionGraph | None = None,
    after: ExecutionGraph | None = None,
) -> RunDefinition:
    if not isinstance(item, ExecutionGraph):
        raise TypeError("item must be an ExecutionGraph")
    if before is not None and not isinstance(before, ExecutionGraph):
        raise TypeError("before must be an ExecutionGraph or None")
    if after is not None and not isinstance(after, ExecutionGraph):
        raise TypeError("after must be an ExecutionGraph or None")
    return RunDefinition(item, before, after)


def load_run(fixture_dir: Path) -> RunDefinition:
    main_py = fixture_dir / "main.py"
    spec = importlib.util.spec_from_file_location("pycastle_fixture_main", main_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Run definition from {main_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    run = getattr(module, "run", None)
    if not isinstance(run, RunDefinition):
        raise TypeError(f"{main_py} must define a module-level `run` RunDefinition")
    return run


def load_graph(fixture_dir: Path) -> ExecutionGraph:
    return load_run(fixture_dir).item


@dataclass(frozen=True)
class NodeVisit:
    node: ExecutionNode
    ordinal: int
    predecessor: object | None


@dataclass(frozen=True)
class NodeOutcome:
    success: bool
    evidence: object | None = None


@dataclass(frozen=True)
class ExecutionWalk:
    terminal: Terminal
    visits: tuple[NodeVisit, ...]


NodeVisitor: TypeAlias = Callable[[NodeVisit], NodeOutcome]


def walk_execution_graph(graph: ExecutionGraph, visit: NodeVisitor) -> ExecutionWalk:
    """Walk one graph with fixed, per-walk ten-visit node bounds."""
    counts: dict[str, int] = {}
    history: list[NodeVisit] = []
    predecessor: object | None = None
    destination: str | Terminal = graph.start
    while not isinstance(destination, Terminal):
        ordinal = counts.get(destination, 0) + 1
        if ordinal > MAX_NODE_VISITS:
            return ExecutionWalk(HUMAN, tuple(history))
        counts[destination] = ordinal
        current = graph.nodes[destination]
        current_visit = NodeVisit(current, ordinal, predecessor)
        history.append(current_visit)
        outcome = visit(current_visit)
        if not isinstance(outcome, NodeOutcome):
            raise TypeError("node visitor must return NodeOutcome")
        predecessor = outcome.evidence
        destination = current.on_success if outcome.success else current.on_failure
    return ExecutionWalk(destination, tuple(history))


# Internal compatibility while the retained Run lifecycle is moved to Node terms.
Phase = RuntimeNode
PhaseGraph = ExecutionGraph
PhaseResult = dataclass(
    type(
        "PhaseResult", (), {"__annotations__": {"phase": str, "result": RuntimeResult}}
    )
)


def phase(name: str, prompt: str, **edges: object) -> RuntimeNode:
    return runtime_node(name, prompt, **edges)  # type: ignore[arg-type]


def build(*, start: str, phases: Sequence[RuntimeNode]) -> ExecutionGraph:
    return execution_graph(start=start, nodes=phases)


@dataclass
class WalkResult:
    results: list[PhaseResult]
    terminal: Terminal


class GraphExecutor:
    """Compatibility adapter over the new fixed-cap walker."""

    def __init__(
        self, runtime: Runtime, *, fixture_dir: Path, preamble: str = "", **_: object
    ):
        self.runtime = runtime
        self.fixture_dir = fixture_dir
        self.preamble = preamble

    def render_prompt(self, node: RuntimeNode, extra: str | None = None) -> str:
        prompt = (self.fixture_dir / "prompts" / node.prompt).read_text()
        return "\n\n".join(x for x in (self.preamble, prompt, extra) if x)

    def _default_runner(self, cwd: Path):
        def run_once(node: RuntimeNode, extra: str | None):
            try:
                result = self.runtime.run(
                    self.render_prompt(node, extra), cwd=cwd, phase=node.name
                )
            except AgentCrashError:
                return False, []
            return True, [PhaseResult(node.name, result)]

        return run_once

    def execute(
        self, graph: ExecutionGraph, *, cwd: Path, phase_context=None, phase_runner=None
    ) -> WalkResult:
        results: list[PhaseResult] = []
        context = phase_context or {}

        def invoke(entry: NodeVisit) -> NodeOutcome:
            node = entry.node
            if not isinstance(node, RuntimeNode):
                raise TypeError("GraphExecutor requires an explicit Gate visitor")
            if phase_runner is not None:
                passed, produced = phase_runner(node, context.get(node.name))
                results.extend(produced)
                return NodeOutcome(passed)
            try:
                result = self.runtime.run(
                    self.render_prompt(node, context.get(node.name)),
                    cwd=cwd,
                    phase=node.name,
                )
            except AgentCrashError:
                return NodeOutcome(False)
            results.append(PhaseResult(node.name, result))
            return NodeOutcome(True)

        walked = walk_execution_graph(graph, invoke)
        return WalkResult(results, walked.terminal)
