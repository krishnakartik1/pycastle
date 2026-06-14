"""The Phase graph and its Builder-style API.

A project describes its workflow in ``.pycastle/main.py`` by building a
:class:`PhaseGraph` with chained calls and assigning it to a module-level
``graph``. v0.1 runs a linear sequence of phases; per-phase success and
failure transitions arrive in a later slice, so the Builder API here is kept
deliberately small.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from .models import RuntimeResult
from .runtime import Runtime


@dataclass(frozen=True)
class Phase:
    """One step in the workflow, driven by a named prompt file."""

    name: str
    prompt: str


@dataclass
class PhaseGraph:
    """An ordered sequence of phases."""

    phases: list[Phase] = field(default_factory=list)


class PhaseGraphBuilder:
    """Builds a :class:`PhaseGraph` through chained ``.phase(...)`` calls."""

    def __init__(self) -> None:
        self._phases: list[Phase] = []

    def phase(self, name: str, *, prompt: str) -> PhaseGraphBuilder:
        """Append a phase and return ``self`` for chaining."""
        self._phases.append(Phase(name=name, prompt=prompt))
        return self

    def build(self) -> PhaseGraph:
        """Return the assembled :class:`PhaseGraph`."""
        return PhaseGraph(phases=list(self._phases))


def build() -> PhaseGraphBuilder:
    """Start a graph; the entry point a fixture's ``main.py`` calls."""
    return PhaseGraphBuilder()


def load_graph(fixture_dir: Path) -> PhaseGraph:
    """Import ``<fixture_dir>/main.py`` and return its module-level ``graph``."""
    main_py = fixture_dir / "main.py"
    spec = importlib.util.spec_from_file_location("pycastle_fixture_main", main_py)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load fixture graph from {main_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    graph = getattr(module, "graph", None)
    if not isinstance(graph, PhaseGraph):
        raise TypeError(f"{main_py} must define a module-level `graph` PhaseGraph")
    return graph


@dataclass
class PhaseResult:
    """The outcome of executing one phase."""

    phase: str
    result: RuntimeResult


class GraphExecutor:
    """Runs a :class:`PhaseGraph`'s phases in order through a Runtime."""

    def __init__(self, runtime: Runtime, *, fixture_dir: Path) -> None:
        self.runtime = runtime
        self.fixture_dir = fixture_dir

    def execute(
        self,
        graph: PhaseGraph,
        *,
        cwd: Path,
        phase_context: dict[str, str] | None = None,
    ) -> list[PhaseResult]:
        """Execute each phase in order and return the per-phase results.

        ``phase_context`` carries extra text to append to a named phase's
        prompt for this run — the retry path uses it to thread prior-attempt
        context into the ``implement`` prompt without rewriting the prompt file.
        """
        phase_context = phase_context or {}
        results: list[PhaseResult] = []
        for phase in graph.phases:
            prompt = (self.fixture_dir / "prompts" / phase.prompt).read_text()
            extra = phase_context.get(phase.name)
            if extra:
                prompt = f"{prompt}\n\n{extra}"
            result = self.runtime.run(prompt, cwd=cwd, phase=phase.name)
            results.append(PhaseResult(phase=phase.name, result=result))
        return results
