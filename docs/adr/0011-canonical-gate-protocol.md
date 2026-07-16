# ADR-0011: The canonical Gate is a frozen project-owned executable

Status: Accepted (2026-07-16)

## Context

The current Gate contract mixes project verification with PyCastle policy. A
Gate may be absent, has a PyCastle-specific `--check-tools` mode, may source
Setup, is sometimes launched through Bash instead of its shebang, is reread from
the mutable checkout, and is surfaced or persisted differently when verbose
logging is enabled. Hard-coded Gate placement and retry have already been
replaced by Execution graphs in ADR-0010.

PyCastle needs one language-agnostic boundary: the project defines what valid
verification means, while PyCastle defines only when and how that verification
is invoked and how its factual result enters graph control flow.

## Decision

Every valid Project fixture contains one mandatory executable at
`.pycastle/gate`, even when its current Execution graphs contain no Gate nodes.
`pycastle init` creates an executable Gate that explains that project
verification must be configured and exits nonzero; PyCastle never guesses a
language, package manager, toolchain, or useful passing check.

Before Run side effects, PyCastle creates one frozen fixture copy that preserves
the canonical Gate's bytes and executable mode. Every Gate node in that Run
invokes this copy, so edits in a worktree or the initiating checkout take effect
only on a later Run.

PyCastle invokes the frozen Gate directly, respecting its shebang, in the target
worktree and selected Sandbox. It receives no arguments, closed standard input,
the ordinary Sandbox process environment, and exactly one PyCastle-specific
input: `PYCASTLE_SCOPE=item` for the Item execution graph or
`PYCASTLE_SCOPE=run` for either Run-scope graph. A Gate node's name is graph
identity only and is not passed to the executable. No incoming edge context is
passed through arguments, standard input, environment, or a record path.

PyCastle invokes the mandatory Setup as a separate process immediately before
every Gate-node visit. The Gate neither invokes nor sources Setup and cannot rely
on Setup shell state surviving. The Gate has no PyCastle-specific mode such as
`--check-tools`.

Gate placement and recovery are entirely explicit Execution-graph topology. A
Gate node succeeds only when the process exits zero. A nonzero exit, signal
termination, or launch error fails the node and follows its failure edge. The
typed outcome preserves the exact exit code, signal, or OS error kind, number,
and message; PyCastle never invents an exit code. Setup failure, operator
cancellation, and host orchestration failure remain outside graph control flow
and stop the Run under their lifecycle rules.

There is no Gate-specific retry or timeout setting. Retry is an explicit graph
cycle, and projects that require an internal verification deadline own it inside
their Gate. PyCastle recommends verification-only behavior but does not classify,
reject, or roll back Gate filesystem effects; any effects remain in the shared
worktree under ADR-0010's node-persistence rule.

Standard output and standard error are opaque diagnostic bytes. PyCastle never
parses them for statuses, test counts, JSON, commands, or other control data.
Every successful or failed Gate visit produces one immutable execution record,
identified by Run, graph scope, Item when applicable, Gate-node identity, and
visit ordinal. The record must be finalized before an edge is followed; failure
to persist it is a host orchestration failure, not a red Gate.

Each local execution record retains a fixed, non-configurable maximum of 16 MiB
per stream: the first 8 MiB and final 8 MiB, with the exact omitted-byte count
when the middle is truncated. Raw records stay in ignored local Run data and are
never published or exposed to a Runtime by path.

When a Gate edge enters a Runtime node, PyCastle automatically projects the
immediate Gate outcome into that fresh invocation. The evidence contains the
typed termination facts plus a fixed, non-configurable tail of 16 KiB from each
stream. Before prompt injection PyCastle removes terminal control sequences,
decodes invalid UTF-8 with replacement markers, and redacts known credential
patterns and values from sensitive environment variables. This sanitization is
defense in depth; projects remain responsible for not printing secrets.

Execution capture is identical in every presentation mode. As established in
ADR-0010, verbose logging changes only the richer human-facing transcript
rendered from execution records after node execution. It cannot alter the Gate
or Runtime invocation, which events are retained, edge evidence, or graph
behavior. Every Gate visit retains its execution record whether or not verbose
logging was requested.

## Rationale

One mandatory zero-argument executable gives projects complete freedom to use
any language while keeping PyCastle's contract small. Fail-closed initialization
is honest where a generic passing check cannot exist. Freezing prevents work
under review from weakening active verification. Direct shebang execution avoids
making Bash part of the protocol, and separate Setup execution keeps preparation
durable rather than shell-session dependent.

Typed termination and bounded automatic evidence give repair nodes useful facts
without parsing project output or exposing an unbounded transcript. Always-on
records keep graph behavior independent of observability settings, while fixed
limits avoid another configuration surface and bound prompt and disk use.

## Consequences

- Existing optional Gates, Bash wrappers, Gate-sourced Setup, `--check-tools`,
  synthetic launch exit codes, and verbosity-dependent capture have no
  compatibility path.
- Readiness may validate the canonical executable as data but cannot execute a
  special Gate mode; its replacement toolchain boundary is decided separately.
- Runtime and Gate adapters need an always-on capture path, a deterministic
  edge-outcome projector, and a separate post-execution transcript renderer.
- Exact record serialization and cross-stream chronology are implementation
  details outside this architecture map.
