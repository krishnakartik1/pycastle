# ADR-0013: Run readiness proves safe startup, not project preparation

Status: Accepted (2026-07-16)

## Context

Setup and Gate are now mandatory project-owned executables with no
readiness-specific mode. PyCastle cannot claim to understand whether a project's
dependencies, generated prerequisites, tests, or other verification policy are
ready without executing project code and rebuilding the language-specific
knowledge that those hooks deliberately encapsulate.

Doctor must still give a meaningful answer for one intended Run, and Run must
reject unsafe orchestration and bootstrap configurations before it creates Git
or Issue state. Docker adds a deliberate complication: the project-owned
Dockerfile is the only way to know whether the neutral Agent-image boundary is
valid, but building it has ordinary Docker-layer effects.

## Decision

Doctor and Run use one **Run readiness** evaluator. Its overall outcome is
`ready`, `no_work`, or `not_ready`. Its ordered checks use `pass`, `fail`,
`blocked`, and `not_applicable`; independent checks continue after failures and
dependent checks are reported as blocked. Human and machine-readable renderers
project the same deterministic snapshot.

Run readiness resolves one explicit configuration. An explicit Sandbox choice
wins; otherwise PyCastle reads `.pycastle/sandbox`; an absent or invalid choice
is `not_ready`, with no implicit default or environment detection.

The evaluator first performs only read-only coordination checks:

- the initiating checkout is an attached, clean Git worktree and its selected
  remote base branch resolves to an exact commit;
- the Project fixture is compatible and structurally valid, including its Run
  definition, graph topology, prompt paths, and Sandbox selection;
- the canonical Setup and Gate are regular, non-symlinked executable files with
  syntactically valid shebangs; they are validated as data and never executed;
- the Issue source is authenticated and identifies the expected repository,
  required permissions and workflow labels exist, and an ordered eligible Item
  snapshot can be read without claiming or relabelling anything.

An empty eligible snapshot produces `no_work`. Doctor and Run both exit
successfully without building an Agent image, freezing execution inputs,
allocating Run state, creating Git state, or mutating the Issue source.

For a non-empty snapshot, the evaluator freezes the exact remote base commit,
the active Project fixture, and the full ordered Item batch. Under the host
Sandbox it verifies PyCastle's host orchestration commands plus selected Runtime
launch and native authentication. It does not inspect the Setup or Gate shebang
interpreter or any project tool. Missing project bootstrap capacity is learned
from the first real Setup launch.

Under the Docker Sandbox, readiness performs the canonical project Dockerfile
build against the clean repository context, pins the resulting immutable image
identity, and probes only ADR-0012's language-neutral Image contract and the
selected Runtime's launch and native authentication. The build may populate
Docker's ordinary layer cache; this is an allowed local preparation effect, not
a PyCastle-managed cache. Readiness never detects a language, reads a dependency
manifest, invokes a package manager, probes a compiler or interpreter, runs a
project command, or executes Setup, Gate, an Execution graph, or a Runtime
prompt.

A `ready` outcome means only that PyCastle can safely allocate a Run and attempt
project preparation for the frozen snapshot. It does not mean Setup or Gate will
pass. Doctor discards its frozen snapshot and never produces reusable authority;
a later Run evaluates and freezes its own current snapshot. Run retains its
frozen fixture, Item batch, base commit, and Docker image identity and uses those
exact inputs rather than resolving mutable names again.

After Run readiness returns `ready`, PyCastle allocates the Run identifier and
ignored local Run record, creates the Run branch from the pinned base commit and
its Run worktree, then invokes the frozen Setup with
`PYCASTLE_SCOPE=run`—even when no Before-Run graph exists. This is the first real
project preparation and occurs before PyCastle claims, relabels, or creates a
branch for any Item. Setup still runs separately immediately before every
Runtime-node and Gate-node visit; the bootstrap success is neither cached nor
treated as authority for a later node.

A bootstrap Setup failure records the exact typed termination and bounded stdout
and stderr, removes the Run branch and worktree, retains the ignored local Run
record, and stops without touching Item state. Setup has no special bootstrap
mode: the same frozen executable receives the same `PYCASTLE_SCOPE=run` input.

The frozen Item batch is not a reservation. Immediately before each claim,
PyCastle's host-side Issue-source adapter rechecks only that Item's frozen
eligibility assumptions. A stale Item is recorded and skipped; PyCastle never
substitutes a newly eligible Item or changes the frozen order. If no Item is
ultimately claimed or completed, PyCastle creates no pull request and cleans the
Run branch and worktree while retaining the local record.

Each claimed Item has a separate worktree and invokes the same frozen Setup with
`PYCASTLE_SCOPE=item`. Run-worktree preparation is never copied into an Item
worktree, Setup success is never reused across worktrees, and PyCastle provides
no cross-worktree environment or package-manager cache. Projects may use their
own caching behavior behind the language-agnostic Setup boundary.

Readiness reports expose only bounded, allow-listed facts and remediation:
resolved configuration, pinned base identity, fixture identity, Item numbers and
titles, and the immutable Docker image identity when applicable. Command output,
credentials, Issue bodies or comments, project-hook diagnostics, and reusable
authorization tokens are excluded. Exact execution-record serialization remains
an implementation detail.

## Rationale

This boundary makes Doctor useful without pretending that PyCastle understands
project preparation. Building and probing the project image is necessary for a
truthful Docker startup answer; running Setup or Gate would instead turn Doctor
into a dry Run with project effects. A real Run-scope Setup before Item ownership
then catches missing project bootstrap capacity at the earliest safe point and
preserves its exact failure diagnostics.

Three explicit outcomes remove the old contradiction where an empty batch made
readiness fail but Run translated that particular failure into success. Frozen
identities close time-of-check/time-of-use races while keeping Item selection
optimistic rather than turning Doctor or readiness into a reservation system.

## Consequences

- ADR-0009 is superseded. Its shared-evaluator and snapshot principles survive,
  but its Gate `--check-tools` probe, content-addressed image behavior, and
  binary ready/unready result do not.
- Run readiness can succeed even though the first Setup fails. This is the
  intentional ownership boundary, not a false-positive readiness defect.
- No-work Runs avoid Docker builds and all Run allocation.
- Host readiness intentionally cannot promise that a project-specific shebang
  interpreter or toolchain exists; the pre-claim bootstrap Setup reports that
  failure exactly.
- Docker builds, Runtime authentication checks, and Image-contract probes remain
  Runtime-aware but language- and project-toolchain-agnostic.
- The current readiness check inventory, image-resolution paths, Gate-toolchain
  probe, empty-batch special case, and pre-claim orchestration must be replaced
  by the resulting implementation specification; no compatibility behavior is
  required.
