"""The Phase graph and its declarative Builder API.

A project describes its workflow in ``.pycastle/main.py`` by assembling a
:class:`PhaseGraph` from a list of :func:`phase` rows and assigning it to a
module-level ``graph``. Each phase names its own success and failure
destinations, so the workflow can branch — implement → review on success,
implement → handoff (or a human) on failure — rather than running a fixed
linear list. The executor is a transition *walker*: from ``start`` it runs each
phase, maps the phase's outcome onto its ``on_success`` / ``on_failure`` edge,
and follows that edge until it reaches a terminal (:data:`DONE` or
:data:`HUMAN`). See ADR-0004 for why the API reads as declarative rows.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .models import RuntimeResult
from .runtime import AgentCrashError, Runtime


class Terminal:
    """A sentinel marking where a walk stops rather than another phase.

    Two terminals exist: :data:`DONE` (the workflow finished cleanly) and
    :data:`HUMAN` (the workflow needs a person). A phase edge points either at
    another phase by name or at one of these.
    """

    def __init__(self, name: str) -> None:
        """Name the terminal so it reads clearly in logs and errors."""
        self.name = name

    def __repr__(self) -> str:
        """Render as the bare terminal name (``DONE`` / ``HUMAN``)."""
        return self.name


#: The walk finished cleanly; the orchestrator proceeds to commit and merge.
DONE = Terminal("DONE")
#: The walk needs a person; the orchestrator hands the issue to a human.
HUMAN = Terminal("HUMAN")

#: How many times a single phase may be (re)entered before the walk gives up.
#: Guards against runaway cycles such as ``handoff`` ↔ ``implement``; when the
#: cap is hit the walk takes the phase's failure edge (routing toward
#: :data:`HUMAN`) rather than looping forever.
DEFAULT_VISIT_CAP = 10


@dataclass(frozen=True)
class Phase:
    """One step in the workflow, with its prompt and its two transitions.

    ``on_success`` / ``on_failure`` are each another phase's name or a terminal
    sentinel (:data:`DONE` / :data:`HUMAN`). They default to the terminals, so a
    phase with no explicit edges simply finishes (success) or escalates to a
    human (failure).
    """

    name: str
    prompt: str
    on_success: str | Terminal = DONE
    on_failure: str | Terminal = HUMAN


@dataclass
class PhaseGraph:
    """A set of phases plus the name of the one the walk starts at.

    ``phases`` is keyed by phase name; declaration order is preserved (Python
    dicts are ordered) but does not affect the walk, which follows edges from
    ``start``.
    """

    start: str
    phases: dict[str, Phase] = field(default_factory=dict)


@dataclass(frozen=True)
class RunDefinition:
    """The project-owned graphs frozen for one Run."""

    item: PhaseGraph
    before: PhaseGraph | None = None
    after: PhaseGraph | None = None


def build_run(
    *,
    item: PhaseGraph,
    before: PhaseGraph | None = None,
    after: PhaseGraph | None = None,
) -> RunDefinition:
    """Build one complete Run definition from its scope-specific graphs."""
    if not isinstance(item, PhaseGraph):
        raise TypeError("item must be a PhaseGraph")
    if before is not None and not isinstance(before, PhaseGraph):
        raise TypeError("before must be a PhaseGraph or None")
    if after is not None and not isinstance(after, PhaseGraph):
        raise TypeError("after must be a PhaseGraph or None")
    return RunDefinition(item=item, before=before, after=after)


def phase(
    name: str,
    prompt: str,
    *,
    on_success: str | Terminal = DONE,
    on_failure: str | Terminal = HUMAN,
) -> Phase:
    """Declare one phase row for :func:`build`.

    ``name`` identifies the phase and ``prompt`` is its prompt file under
    ``prompts/``. ``on_success`` and ``on_failure`` name where the walk goes
    next — another phase's name or a terminal (:data:`DONE` / :data:`HUMAN`);
    they default to the terminals so a simple flow stays terse.
    """
    return Phase(name=name, prompt=prompt, on_success=on_success, on_failure=on_failure)


def build(*, start: str, phases: list[Phase]) -> PhaseGraph:
    """Assemble and validate a :class:`PhaseGraph` from declared phase rows.

    Row order is irrelevant — ``start`` names the entry explicitly. Every edge
    target must be a declared phase name or a terminal, and ``start`` must name a
    declared phase; a violation raises :class:`ValueError` with a clear message
    so a bad ``.pycastle/main.py`` fails fast rather than walking off the graph.
    """
    by_name: dict[str, Phase] = {}
    for p in phases:
        if p.name in by_name:
            raise ValueError(f"Duplicate phase name: {p.name!r}")
        by_name[p.name] = p

    if start not in by_name:
        raise ValueError(
            f"start={start!r} is not a declared phase (declared: {sorted(by_name)})"
        )

    for p in by_name.values():
        for kind, target in (
            ("on_success", p.on_success),
            ("on_failure", p.on_failure),
        ):
            if isinstance(target, Terminal):
                continue
            if target not in by_name:
                raise ValueError(
                    f"Phase {p.name!r} {kind}={target!r} is neither a declared "
                    f"phase nor a terminal (declared: {sorted(by_name)})"
                )

    return PhaseGraph(start=start, phases=by_name)


def load_run(fixture_dir: Path) -> RunDefinition:
    """Import ``<fixture_dir>/main.py`` and return its module-level ``run``."""
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


def load_graph(fixture_dir: Path) -> PhaseGraph:
    """Load the required Item phase graph from the fixture Run definition."""
    return load_run(fixture_dir).item


@dataclass
class PhaseResult:
    """The outcome of executing one phase."""

    phase: str
    result: RuntimeResult


#: A function the walker calls to run one phase. It returns ``(passed, results)``
#: where ``passed`` decides which edge the walk follows (``True`` → ``on_success``,
#: ``False`` → ``on_failure``) and ``results`` are the :class:`PhaseResult`\ s that
#: run produced (it may be more than one for a phase that retries internally).
#: ``phase_context`` is extra text to append to this phase's prompt for this run.
PhaseRunner = Callable[[Phase, str | None], "tuple[bool, list[PhaseResult]]"]


@dataclass
class WalkResult:
    """What walking a :class:`PhaseGraph` produced.

    ``results`` is the per-phase-visit results in walk order; ``terminal`` is the
    terminal the walk stopped at (:data:`DONE` or :data:`HUMAN`). The orchestrator
    reads ``terminal`` to decide between the commit/merge path and the
    hand-to-a-human path.
    """

    results: list[PhaseResult]
    terminal: Terminal


class GraphExecutor:
    """Walks a :class:`PhaseGraph`'s transitions through a Runtime.

    From the graph's ``start`` it runs each phase, maps the phase outcome onto
    its ``on_success`` / ``on_failure`` edge, and follows that edge until a
    terminal. A phase "fails" when its runtime call raises
    :class:`~pycastle.runtime.AgentCrashError`; the orchestrator can override
    that per-phase by supplying its own ``phase_runner`` (the ``implement`` node
    runs through #8's retry-with-handoff budget that way).
    """

    def __init__(
        self,
        runtime: Runtime,
        *,
        fixture_dir: Path,
        visit_cap: int = DEFAULT_VISIT_CAP,
        preamble: str = "",
    ) -> None:
        """Bind the runtime and fixture dir, and set the per-phase visit cap.

        ``preamble`` is text prepended to every phase's rendered prompt — the
        orchestrator uses it to hand the runtime its issue context (number,
        title, body) so each phase knows *which* issue it is working. It defaults
        to ``""``, which leaves the rendered prompt byte-identical to the phase
        file (plus any ``extra``).
        """
        self.runtime = runtime
        self.fixture_dir = fixture_dir
        self.visit_cap = visit_cap
        self.preamble = preamble

    def render_prompt(self, phase: Phase, extra: str | None = None) -> str:
        """Render a phase prompt: preamble, then the phase file, then ``extra``.

        The optional constructor ``preamble`` (issue context) leads, the phase's
        own prompt file follows, and any per-run ``extra`` (e.g. prior-attempt
        retry context) trails. Absent parts are dropped, so with no preamble and
        no ``extra`` the result is exactly the phase file's text.
        """
        prompt = (self.fixture_dir / "prompts" / phase.prompt).read_text()
        parts = [part for part in (self.preamble, prompt, extra) if part]
        return "\n\n".join(parts)

    def _default_runner(self, cwd: Path) -> PhaseRunner:
        """A phase runner that runs each phase once; a crash is a failure."""

        def run_once(phase: Phase, extra: str | None) -> tuple[bool, list[PhaseResult]]:
            prompt = self.render_prompt(phase, extra)
            try:
                result = self.runtime.run(prompt, cwd=cwd, phase=phase.name)
            except AgentCrashError:
                return False, []
            return True, [PhaseResult(phase=phase.name, result=result)]

        return run_once

    def execute(
        self,
        graph: PhaseGraph,
        *,
        cwd: Path,
        phase_context: dict[str, str] | None = None,
        phase_runner: PhaseRunner | None = None,
    ) -> WalkResult:
        """Walk ``graph`` from its ``start`` and return the results + terminal.

        Each phase is run via ``phase_runner`` (default: once through the
        runtime, a crash counting as failure). The phase's pass/fail outcome
        selects its ``on_success`` / ``on_failure`` edge; the walk follows edges
        until it reaches a terminal. ``phase_context`` carries extra text to
        append to a named phase's prompt — the retry path threads prior-attempt
        context into ``implement`` that way. A phase entered more than
        :attr:`visit_cap` times takes its failure edge, so a cyclic graph
        (e.g. ``handoff`` ↔ ``implement``) cannot loop forever.
        """
        phase_context = phase_context or {}
        run_phase = phase_runner or self._default_runner(cwd)

        results: list[PhaseResult] = []
        visits: dict[str, int] = {}
        node: str | Terminal = graph.start

        while not isinstance(node, Terminal):
            current = graph.phases[node]
            visits[node] = visits.get(node, 0) + 1
            if visits[node] > self.visit_cap:
                # Runaway cycle (e.g. handoff ↔ implement): stop entering this
                # phase and route to HUMAN so the walk always terminates rather
                # than chasing failure edges around the cycle forever.
                node = HUMAN
                continue

            extra = phase_context.get(current.name) or None
            passed, phase_results = run_phase(current, extra)
            results.extend(phase_results)
            node = current.on_success if passed else current.on_failure

        return WalkResult(results=results, terminal=node)
