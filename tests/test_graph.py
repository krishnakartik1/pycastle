"""The declarative Builder API assembles a graph; the executor walks it.

The Builder is declarative rows — ``build(start=, phases=[phase(...), ...])`` —
and the executor is a transition walker: from ``start`` it runs each phase,
follows the phase's ``on_success`` / ``on_failure`` edge on the run's outcome,
and stops at a terminal (``DONE`` / ``HUMAN``). See ADR-0004.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.graph import (
    DEFAULT_VISIT_CAP,
    DONE,
    HUMAN,
    GraphExecutor,
    Phase,
    PhaseGraph,
    PhaseResult,
    build,
    build_run,
    load_graph,
    load_run,
    phase,
)
from pycastle.models import RuntimeResult, Telemetry
from pycastle.runtime import AgentCrashError, StubRuntime


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


class _ScriptedRuntime:
    """A fake Runtime that crashes on phases named in ``crash_on``.

    Lets a test drive the failure edge: a phase whose name is in ``crash_on``
    raises :class:`AgentCrashError`, so the walker takes that phase's
    ``on_failure`` edge.
    """

    name = "stub"

    def __init__(self, *, crash_on: set[str] | None = None) -> None:
        self.crash_on = crash_on or set()
        self.ran: list[str] = []

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        self.ran.append(phase)
        if phase in self.crash_on:
            raise AgentCrashError("boom", phase=phase, exit_code=1)
        return RuntimeResult(
            output="ok",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


# --------------------------------------------------------------------------- #
# Builder: declarative rows, explicit start, edge validation.                 #
# --------------------------------------------------------------------------- #


def test_builder_assembles_phases_keyed_by_name_with_explicit_start() -> None:
    built = build(
        start="plan",
        phases=[
            phase("plan", "p.md", on_success="do", on_failure=HUMAN),
            phase("do", "d.md", on_success=DONE, on_failure=HUMAN),
        ],
    )
    assert isinstance(built, PhaseGraph)
    assert built.start == "plan"
    assert list(built.phases) == ["plan", "do"]
    assert built.phases["plan"].on_success == "do"
    assert built.phases["do"].on_success is DONE


def test_run_definition_requires_item_and_allows_optional_run_graphs() -> None:
    item = build(start="item", phases=[phase("item", "item.md")])
    after = build(start="report", phases=[phase("report", "report.md")])

    definition = build_run(item=item, after=after)

    assert definition.item is item
    assert definition.before is None
    assert definition.after is after


def test_load_run_rejects_the_superseded_standalone_graph(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, phase\n"
        "graph = build(start='item', phases=[phase('item', 'item.md')])\n"
    )

    with pytest.raises(TypeError, match="module-level `run` RunDefinition"):
        load_run(fixture)


def test_phase_edges_default_to_terminals() -> None:
    """A phase with no explicit edges finishes on success, escalates on failure."""
    p = phase("implement", "implement.md")
    assert isinstance(p, Phase)
    assert p.on_success is DONE
    assert p.on_failure is HUMAN


def test_build_rejects_an_undeclared_start() -> None:
    with pytest.raises(ValueError, match="start='ghost' is not a declared phase"):
        build(start="ghost", phases=[phase("plan", "p.md")])


def test_build_rejects_an_edge_to_an_unknown_phase() -> None:
    with pytest.raises(ValueError, match="neither a declared phase nor a terminal"):
        build(start="plan", phases=[phase("plan", "p.md", on_success="nowhere")])


def test_build_rejects_duplicate_phase_names() -> None:
    with pytest.raises(ValueError, match="Duplicate phase name: 'plan'"):
        build(start="plan", phases=[phase("plan", "a.md"), phase("plan", "b.md")])


# --------------------------------------------------------------------------- #
# load_graph reads the module-level graph from a fixture's main.py.           #
# --------------------------------------------------------------------------- #


def test_load_graph_reads_module_level_graph(fixture_dir: Path) -> None:
    loaded = load_graph(fixture_dir)
    assert loaded.start == "implement"
    assert list(loaded.phases) == ["implement"]


def test_default_graph_loads_plan_implement_review(
    three_phase_fixture_dir: Path,
) -> None:
    """The default workflow graph is plan → implement → review → DONE."""
    loaded = load_graph(three_phase_fixture_dir)

    assert loaded.start == "plan"
    assert list(loaded.phases) == ["plan", "implement", "review"]
    assert loaded.phases["plan"].on_success == "implement"
    assert loaded.phases["implement"].on_success == "review"
    assert loaded.phases["review"].on_success is DONE
    # Every failure edge routes to a human in the default flow.
    assert all(p.on_failure is HUMAN for p in loaded.phases.values())


# --------------------------------------------------------------------------- #
# Executor walk: success edges, failure edges, terminals, and the visit cap.  #
# --------------------------------------------------------------------------- #


def test_executor_walks_a_single_phase_to_done(fixture_dir: Path) -> None:
    loaded = load_graph(fixture_dir)
    executor = GraphExecutor(StubRuntime(), fixture_dir=fixture_dir)

    walk = executor.execute(loaded, cwd=fixture_dir)

    assert [r.phase for r in walk.results] == ["implement"]
    assert walk.terminal is DONE
    assert (fixture_dir / "PYCASTLE_STUB.md").is_file()


def test_executor_follows_success_edges_through_to_done(
    three_phase_fixture_dir: Path,
) -> None:
    """On all-success the walk runs plan → implement → review and ends at DONE."""
    loaded = load_graph(three_phase_fixture_dir)
    runtime = _ScriptedRuntime()
    executor = GraphExecutor(runtime, fixture_dir=three_phase_fixture_dir)

    walk = executor.execute(loaded, cwd=three_phase_fixture_dir)

    assert [r.phase for r in walk.results] == ["plan", "implement", "review"]
    assert runtime.ran == ["plan", "implement", "review"]
    assert walk.terminal is DONE


def test_executor_follows_the_failure_edge_on_a_crash() -> None:
    """A phase whose run crashes takes its ``on_failure`` edge, not success.

    ``plan`` crashes, so the walk follows ``plan``'s failure edge to ``review``
    (not its success edge to ``implement``) and stops at DONE — proving the
    failure edge, not just the success edge, is applied.
    """
    graph = build(
        start="plan",
        phases=[
            phase("plan", "plan.md", on_success="implement", on_failure="review"),
            phase("implement", "implement.md", on_success=DONE, on_failure=HUMAN),
            phase("review", "review.md", on_success=DONE, on_failure=HUMAN),
        ],
    )
    runtime = _ScriptedRuntime(crash_on={"plan"})
    executor = GraphExecutor(runtime, fixture_dir=Path("/unused"))

    def run_phase(p: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        try:
            result = runtime.run("", cwd=Path("/unused"), phase=p.name)
        except AgentCrashError:
            return False, []
        return True, [PhaseResult(phase=p.name, result=result)]

    walk = executor.execute(graph, cwd=Path("/unused"), phase_runner=run_phase)

    # plan crashed -> failure edge to review (NOT success edge to implement).
    assert runtime.ran == ["plan", "review"]
    assert "implement" not in runtime.ran
    assert walk.terminal is DONE


def test_executor_routes_a_runaway_cycle_to_human() -> None:
    """A phase entered past the visit cap routes to HUMAN, never looping forever.

    ``implement`` always fails into ``handoff``, which loops back to
    ``implement`` — a cycle. The walk must terminate at HUMAN once the cap is
    hit rather than spinning.
    """
    graph = build(
        start="implement",
        phases=[
            phase(
                "implement",
                "implement.md",
                on_success=DONE,
                on_failure="handoff",
            ),
            phase("handoff", "handoff.md", on_success="implement", on_failure=HUMAN),
        ],
    )
    executor = GraphExecutor(
        _ScriptedRuntime(), fixture_dir=Path("/unused"), visit_cap=3
    )

    def always_fail(p: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        # implement always fails; handoff always succeeds (loops back).
        return (p.name != "implement"), []

    walk = executor.execute(graph, cwd=Path("/unused"), phase_runner=always_fail)

    assert walk.terminal is HUMAN


def test_visit_cap_counts_a_phase_in_exactly_its_cap_times_before_routing() -> None:
    """The cap is the number of *entries* of one phase, applied before its run.

    With ``visit_cap=3`` the looping ``implement`` node is run on visits 1, 2 and
    3; the 4th entry trips the cap and routes to HUMAN without running it again.
    Pinning the exact run count guards the off-by-one and proves the cap gates on
    entry, not after the run.
    """
    graph = build(
        start="implement",
        phases=[
            phase("implement", "implement.md", on_success=DONE, on_failure="handoff"),
            phase("handoff", "handoff.md", on_success="implement", on_failure=HUMAN),
        ],
    )
    runtime = _ScriptedRuntime()
    executor = GraphExecutor(runtime, fixture_dir=Path("/unused"), visit_cap=3)

    def always_fail(p: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        runtime.run("", cwd=Path("/unused"), phase=p.name)
        return (p.name != "implement"), []

    walk = executor.execute(graph, cwd=Path("/unused"), phase_runner=always_fail)

    assert walk.terminal is HUMAN
    # implement entered (and run) on visits 1, 2, 3; the 4th entry trips the cap
    # and routes to HUMAN without running it. handoff ran after each implement
    # fail, so 3 times; its loop-back to implement is what triggers the 4th entry.
    assert runtime.ran == [
        "implement",
        "handoff",
        "implement",
        "handoff",
        "implement",
        "handoff",
    ]
    assert runtime.ran.count("implement") == 3


def test_default_visit_cap_is_ten_in_production() -> None:
    """An executor built without an explicit cap uses ``DEFAULT_VISIT_CAP`` (10).

    Production never passes ``visit_cap``; only tests do. This pins the shipped
    default so a change to it is a deliberate, reviewed edit, not a silent drift.
    """
    executor = GraphExecutor(_ScriptedRuntime(), fixture_dir=Path("/unused"))
    assert executor.visit_cap == DEFAULT_VISIT_CAP == 10


def test_a_long_acyclic_chain_is_not_capped() -> None:
    """The cap is per-phase-visit, not a global step budget.

    A straight chain of more phases than the cap runs end to end: each phase is
    entered once, so no per-phase count ever exceeds the cap. This proves the cap
    bounds *re*-entry of a single phase (a cycle), not the total number of phases
    a healthy linear flow may have.
    """
    n = 5
    cap = 2  # smaller than the chain length: a global budget would trip here.
    phases = [
        phase(f"p{i}", f"p{i}.md", on_success=(f"p{i + 1}" if i < n - 1 else DONE))
        for i in range(n)
    ]
    graph = build(start="p0", phases=phases)
    runtime = _ScriptedRuntime()
    executor = GraphExecutor(runtime, fixture_dir=Path("/unused"), visit_cap=cap)

    def run_each(p: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        runtime.run("", cwd=Path("/unused"), phase=p.name)
        return True, []

    walk = executor.execute(graph, cwd=Path("/unused"), phase_runner=run_each)

    assert walk.terminal is DONE
    assert runtime.ran == [f"p{i}" for i in range(n)]


def test_implement_only_graph_routes_default_edges_to_done_and_human() -> None:
    """An implement-only graph (default edges) ends at DONE on pass, HUMAN on fail.

    ADR-0004's terse non-default flow — ``build(start='implement',
    phases=[phase('implement', ...)])`` — leans entirely on the default edges
    (``on_success=DONE``, ``on_failure=HUMAN``). This pins both: a passing run
    reaches DONE and a failing run reaches HUMAN, with no edges spelled out.
    """
    graph = build(start="implement", phases=[phase("implement", "implement.md")])
    executor = GraphExecutor(_ScriptedRuntime(), fixture_dir=Path("/unused"))

    def passing(_p: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        return True, []

    def failing(_p: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        return False, []

    done = executor.execute(graph, cwd=Path("/unused"), phase_runner=passing)
    human = executor.execute(graph, cwd=Path("/unused"), phase_runner=failing)

    assert done.terminal is DONE
    assert human.terminal is HUMAN


# --------------------------------------------------------------------------- #
# phase_context threads extra prompt text into the named phase alone.         #
# --------------------------------------------------------------------------- #


def test_phase_context_is_appended_only_to_the_named_phase(
    three_phase_fixture_dir: Path,
) -> None:
    """``phase_context`` reaches only the matching phase, not its siblings."""
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
    assert runtime.prompts["plan"].startswith("# Plan")
    assert runtime.prompts["review"].startswith("# Review")


def test_empty_phase_context_entry_is_not_appended(
    three_phase_fixture_dir: Path,
) -> None:
    """An empty context string for a phase leaves that phase's prompt untouched."""
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


# --------------------------------------------------------------------------- #
# render_prompt: an optional preamble leads, then the phase file, then extra.  #
# --------------------------------------------------------------------------- #


def test_render_prompt_prepends_the_preamble_before_the_phase_file(
    three_phase_fixture_dir: Path,
) -> None:
    """A constructor preamble leads the rendered prompt, then the phase file."""
    executor = GraphExecutor(
        StubRuntime(), fixture_dir=three_phase_fixture_dir, preamble="ISSUE-CONTEXT"
    )
    phase_file = (three_phase_fixture_dir / "prompts" / "plan.md").read_text()

    rendered = executor.render_prompt(phase("plan", "plan.md"))

    assert rendered == f"ISSUE-CONTEXT\n\n{phase_file}"


def test_render_prompt_orders_preamble_then_phase_then_extra(
    three_phase_fixture_dir: Path,
) -> None:
    """With both a preamble and ``extra``, order is preamble → prompt → extra."""
    executor = GraphExecutor(
        StubRuntime(), fixture_dir=three_phase_fixture_dir, preamble="ISSUE-CONTEXT"
    )
    phase_file = (three_phase_fixture_dir / "prompts" / "implement.md").read_text()

    rendered = executor.render_prompt(phase("implement", "implement.md"), "RETRY-EXTRA")

    assert rendered == f"ISSUE-CONTEXT\n\n{phase_file}\n\nRETRY-EXTRA"


def test_render_prompt_without_a_preamble_is_byte_identical_to_the_phase_file(
    three_phase_fixture_dir: Path,
) -> None:
    """No preamble (the default) leaves the rendered prompt exactly the phase file."""
    executor = GraphExecutor(StubRuntime(), fixture_dir=three_phase_fixture_dir)
    phase_file = (three_phase_fixture_dir / "prompts" / "review.md").read_text()

    assert executor.render_prompt(phase("review", "review.md")) == phase_file
