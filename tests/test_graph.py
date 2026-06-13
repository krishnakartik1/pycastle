"""The Builder API assembles a graph and the executor runs it in order."""

from __future__ import annotations

from pathlib import Path

from pycastle import graph as g
from pycastle.graph import GraphExecutor, PhaseGraph, load_graph
from pycastle.models import RuntimeResult, Telemetry
from pycastle.runtime import StubRuntime


class _RecordingRuntime:
    """A fake Runtime that records the prompt it was handed for each phase."""

    name = "stub"

    def __init__(self) -> None:
        self.prompts: dict[str, str] = {}

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        self.prompts[phase] = prompt
        return RuntimeResult(
            output="ok",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


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


def test_default_graph_loads_plan_implement_review_in_order(
    three_phase_fixture_dir: Path,
) -> None:
    """The default workflow graph is plan → implement → review, in that order."""
    loaded = load_graph(three_phase_fixture_dir)

    assert [p.name for p in loaded.phases] == ["plan", "implement", "review"]
    assert [p.prompt for p in loaded.phases] == [
        "plan.md",
        "implement.md",
        "review.md",
    ]


def test_executor_runs_the_three_phases_in_order(
    three_phase_fixture_dir: Path,
) -> None:
    """Executing the default graph runs the three phases in plan→implement→review."""
    loaded = load_graph(three_phase_fixture_dir)
    executor = GraphExecutor(StubRuntime(), fixture_dir=three_phase_fixture_dir)

    results = executor.execute(loaded, cwd=three_phase_fixture_dir)

    assert [r.phase for r in results] == ["plan", "implement", "review"]


def test_phase_context_is_appended_only_to_the_named_phase(
    three_phase_fixture_dir: Path,
) -> None:
    """``phase_context`` reaches only the matching phase, not its siblings.

    The retry path threads prior-attempt context keyed to ``implement`` (see
    ``orchestrator._run_implement_attempts``). On the default plan → implement →
    review graph that block must land in the implement prompt alone — the plan
    and review phases must never see implement's retry context.
    """
    loaded = load_graph(three_phase_fixture_dir)
    runtime = _RecordingRuntime()
    executor = GraphExecutor(runtime, fixture_dir=three_phase_fixture_dir)

    executor.execute(
        loaded,
        cwd=three_phase_fixture_dir,
        phase_context={"implement": "PRIOR-ATTEMPT-MARKER"},
    )

    assert "PRIOR-ATTEMPT-MARKER" in runtime.prompts["implement"]
    assert "PRIOR-ATTEMPT-MARKER" not in runtime.prompts["plan"]
    assert "PRIOR-ATTEMPT-MARKER" not in runtime.prompts["review"]
    # Each phase still gets its own prompt-file body as the base.
    assert runtime.prompts["plan"].startswith("# Plan")
    assert runtime.prompts["review"].startswith("# Review")


def test_empty_phase_context_entry_is_not_appended(
    three_phase_fixture_dir: Path,
) -> None:
    """An empty context string for a phase leaves that phase's prompt untouched.

    The first implement attempt passes no context (``retry_context`` is ``""``);
    even if an empty entry reached the executor it must be a no-op, so the prompt
    is exactly the prompt file with no trailing blank block.
    """
    loaded = load_graph(three_phase_fixture_dir)
    runtime = _RecordingRuntime()
    executor = GraphExecutor(runtime, fixture_dir=three_phase_fixture_dir)

    executor.execute(
        loaded,
        cwd=three_phase_fixture_dir,
        phase_context={"implement": ""},
    )

    implement_prompt = (
        three_phase_fixture_dir / "prompts" / "implement.md"
    ).read_text()
    assert runtime.prompts["implement"] == implement_prompt
