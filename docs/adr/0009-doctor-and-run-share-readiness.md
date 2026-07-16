# ADR-0009: Doctor and Run share one readiness boundary

Status: Superseded by ADR-0013 (2026-07-16)

ADR-0013 retains one shared Doctor/Run readiness evaluator while replacing this
ADR's binary readiness result, content-addressed image preparation, disposable
workspace/cache allowance, Gate toolchain mode, and startup ordering.

## Context

Run startup historically discovered checkout, fixture, Sandbox, Runtime, Gate,
and GitHub failures at different points. Agent-driven callers need one bounded,
machine-readable answer before Run creates branches, worktrees, claims, or Run
records. A separately implemented diagnostic would inevitably drift from direct
CLI Run safety.

## Decision

`pycastle doctor` evaluates one fully resolved Run configuration through the
same readiness evaluator used as Run's pre-side-effect safety boundary. The
report is a current snapshot, never a reservation, capability, or reusable
authorization. Run re-evaluates safety-critical state and its eligible Item
batch immediately before its first Run side effect.

The evaluator emits the stable schema-v1 inventory and the statuses `pass`,
`fail`, `blocked`, and `not_applicable`. Independent probes continue after a
failure; dependent probes are blocked. External commands are bounded, are not
retried, and expose only allow-listed facts and remediation. Runtime and Gate
output, Issue bodies/comments, and credentials are not report data.

Doctor is read-only with respect to the Project fixture, GitHub, Git refs, and
Run state. It does not execute Setup, ordinary Gate mode, a graph, a phase, or a
Runtime prompt. It may build or reuse the same content-addressed Agent image as
Run and may create disposable cache/workspace data. Those permitted cache
effects are not Run state.

Project execution remains in the selected Sandbox under ADR-0007. Doctor loads
the complete declarative Run definition from ADR-0008 with bytecode writes
disabled, validates its structure and contained prompt paths, and runs only the
Gate's non-mutating `--check-tools` mode in that Sandbox.

## Consequences

- Lifecycle skills can consume one deterministic JSON document without copying
  PyCastle policy.
- A prior Doctor success cannot be supplied to or trusted by Run; state drift is
  caught by re-evaluation.
- Readiness adapters use injected command runners, so tests need no live Docker,
  GitHub, Claude, or Codex service.
- Building a missing content-addressed Agent image is the only durable effect
  Doctor permits, and the following Run naturally reuses it.
