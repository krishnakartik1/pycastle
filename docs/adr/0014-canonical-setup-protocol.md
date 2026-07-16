# ADR-0014: Setup is a frozen project-owned prerequisite

Status: Accepted (2026-07-16)

## Context

Project preparation used to leak package-manager, virtual-environment, and
toolchain assumptions into PyCastle. It also relied on shell state surviving
between preparation and execution, even though Docker may use a fresh container
for every process. A language-agnostic runner needs a smaller boundary: the
project decides how to prepare its current worktree, while PyCastle decides only
when and how that prerequisite is invoked.

## Decision

Every valid Project fixture contains one mandatory executable at
`.pycastle/setup`. `pycastle init` creates an executable, documented no-op; the
project replaces its contents when preparation is needed. PyCastle never detects
dependency manifests, chooses package managers, creates language environments,
understands toolchains, or interprets Setup commands or output.

Before Run side effects, PyCastle freezes the canonical Setup bytes and
executable mode from the initiating checkout. Every Setup invocation in that Run
uses the frozen copy, so fixture edits proposed by a Runtime take effect only on
a later Run.

PyCastle invokes Setup once as a Run-scope bootstrap after readiness creates the
Run worktree and before any Item state changes, even when there is no Before-Run
execution graph. It then invokes Setup separately immediately before every
Runtime-node and Gate-node visit at Item or Run scope. Setup is not an Execution
graph node, is never a transition target, does not consume edge context, and its
success is not reused as authority for another node.

Every invocation directly executes the frozen hook through its shebang with zero
arguments, closed standard input, and the target worktree as its working
directory. The ordinary Sandbox process environment is preserved, and
`PYCASTLE_SCOPE=item|run` is the only PyCastle-specific input. Setup receives no
node identity. Setup and the following node are separate processes and may run
in separate Docker containers.

Only durable worktree or external-system effects survive. Shell activation,
exported variables, aliases, current-directory changes, background processes,
and container-local mutations are not valid Setup outputs. The project must make
Setup safe to repeat. Exit zero establishes the prerequisite only for the
immediately following node; PyCastle provides no Setup result, environment, or
package-manager cache.

Exit zero alone succeeds. A nonzero exit, signal termination, or launch error
fails. PyCastle provides no Setup retry or timeout setting; projects that require
an internal deadline own it inside the hook.

Every invocation produces one required immutable local execution record before
PyCastle may invoke the following node. It uses the same fixed capture bound as
Gate records: at most 16 MiB from each opaque output stream, retaining the first
and final 8 MiB plus the exact omitted-byte count. Record-persistence failure is
a host orchestration failure. Setup output never becomes graph-edge context;
human-facing logs are rendered and sanitized from the record, and verbosity
changes presentation only.

Any Setup failure prevents the following node and stops the Run outside graph
control flow. PyCastle releases the active Item, if any, back to
`ready-for-agent` and never folds that Item's worktree into the Run branch. When
no Item has yet
been integrated, PyCastle creates no pull request because Before-Run checkpoints
are not independently deliverable. When at least one Item has been integrated,
PyCastle publishes only the last durable Run-branch checkpoint as a draft pull
request and never marks it ready. Local records retain exact typed termination
and bounded output; publication exposes only safe failure metadata.

## Rationale

A mandatory direct executable gives every language the same stable protocol
without teaching PyCastle any language. Freezing prevents a change under review
from weakening or replacing its own prerequisites. Repetition before every node
lets manifest changes take effect at the next boundary, while durable-effects-
only semantics work identically on a host and across disposable containers.

Stopping the whole Run on failure keeps a missing prerequisite distinct from an
ordinary graph outcome: no node ran, so there is no edge to follow. Preserving
already integrated work in a draft pull request avoids losing durable progress
without presenting the interrupted Run as complete.

## Consequences

- Optional Setup hooks, sourced hooks, shell activation, implicit environment
  reuse, node-specific preparation, and PyCastle-managed language environments
  have no compatibility path.
- Project commands must locate durable environments explicitly, such as through
  a project wrapper or a worktree-relative executable; PyCastle does not modify
  `PATH` based on Setup output.
- A Runtime change to a dependency manifest is prepared by the next Setup visit.
  If it requires immutable tooling absent from a pinned Agent image, the
  Dockerfile or fixture change must land first—or be completed manually—and a
  later Run adopts it.
- ADR-0012 owns the immutable Docker bootstrap and mount boundary. ADR-0013 owns
  readiness and the point at which a real Run first invokes this protocol.
