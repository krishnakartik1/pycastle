# ADR-0004: Declarative Builder API for phase-graph success/failure transitions

Status: Superseded by ADR-0010 (2026-07-16)

ADR-0010 retains declarative, local, order-independent transitions while
replacing Phase types, the Phase graph API, and special retry behavior with the
Execution graph model.

## Context

v0.1 shipped a flat, linear phase graph — `g.build().phase("plan", ...).phase("implement", ...).phase("review", ...).build()` — executed strictly in list order. Issue #10 needs per-phase **success/failure transitions** so a workflow can branch (implement → review on success; → handoff/human on failure) and express non-default flows such as implement-only (PRD user stories 39, 43). ADR-0001 already fixed the executable-Python Builder approach (not TOML/YAML). Crucially, `.pycastle/main.py` is edited by **both humans and AI agents** (PRD user stories 4, 39).

A throwaway prototype (`prototypes/issue10_phase_graph_api.py`) compared four authoring shapes — all compiling to the *same* executable graph and run path — so the decision was purely about how `.pycastle/main.py` should read.

## Decision

Adopt a **declarative-rows** Builder API: each phase is a self-contained call in a plain list; an explicit `start` names the entry; edges default to terminals.

```python
graph = build(
    start="plan",
    phases=[
        phase("plan",      "plan.md",      on_success="implement", on_failure=HUMAN),
        phase("implement", "implement.md", on_success="review",    on_failure="handoff"),
        phase("review",    "review.md",    on_success=DONE,        on_failure="handoff"),
        phase("handoff",   "handoff.md",   on_success="implement", on_failure=HUMAN),
    ],
)

# A non-default flow is just a shorter list — edges default to terminals:
graph = build(start="implement", phases=[phase("implement", "implement.md")])
```

- `phase(name, prompt, *, on_success=DONE, on_failure=HUMAN)` — terminals are the defaults.
- `build(*, start, phases)` — row order is irrelevant; `start` is explicit.
- `DONE` / `HUMAN` are terminal sentinels.
- The executor becomes a transition **walker**: from `start`, run each phase, map its outcome (agent crash or a failed gate = failure, else success) onto the `on_success` / `on_failure` edge, until a terminal. A per-phase visit cap guards against runaway cycles.

## Rationale

This file is rewritten by AI agents, so **explicit + local + order-independent** beats terse. Every phase names its own destinations (no implicit "next"), each row is an independent one-line diff you can comment, and reordering rows cannot change the flow.

## Alternatives rejected

- **Fluent `.phase().on_success().on_failure()` chain** — the chain *implies* an ordering that explicit edges do not have, and one giant expression is awkward to reorder, comment, and diff.
- **Explicit `.edge(name, success=, failure=)` block** — clean to validate, but separates phases from their wiring (two read-passes to trace the flow).
- **Linear `.phase()` + `.off_ramp()`, success defaulting to the next declared phase** — terse, but "success = next phase" is implicit magic that misfires when a branch target sits in declaration order (observed in the prototype: `review` silently defaulted into `handoff`).

## Consequences

- The linear `PhaseGraphBuilder` and the list-order executor are replaced; `.pycastle/main.py`, the test fixtures, and the executor change.
- Composes with #8 (retry/handoff) and #9 (conflict/interrupt): the walker drives phases; the `implement` phase still runs through #8's retry-with-handoff budget; conflict/interrupt handling (after the graph reaches a terminal) is unchanged. The default shipped graph preserves #8's behaviour (retries remain internal to the implement phase).
