# ADR-0010: Execution graphs unify Runtime and Gate control flow

Status: Accepted (2026-07-16)

ADR-0004 is superseded. ADR-0008 remains accepted except for the graph model,
Gate placement, retry, edge-context, and failed-node worktree semantics replaced
here.

ADR-0011 refines the canonical Gate's process, evidence, and audit-record
protocol without changing this Execution-graph model.

## Context

Phase graphs model only prompt-driven Runtime work. Gate execution and retry are
separate orchestration paths: the Item `implement` phase is recognized by name,
wrapped in an internal Gate/retry loop, and followed by a mandatory Run Gate at
another hard-coded lifecycle point. This gives one node special behavior, makes
retry unavailable to other work, and prevents a project from expressing its
complete preparation and verification flow in the project-owned graph.

Automatic retry prompt text and provider thread resumption also allow one Runtime
visit to influence another invisibly. Item and Run failures disagree about
whether partial worktree changes survive, so the same graph concept has different
communication semantics by scope.

## Decision

Replace Phase graphs with one **Execution graph** model containing two explicit
node types:

- A **Runtime node** has a unique graph name, a project prompt path, and success
  and failure destinations.
- A **Gate node** has a unique graph name and success and failure destinations.
  Every Gate node invokes the same frozen fixture copy of the canonical Gate;
  its name is graph identity only, not a command, path, mode, or hook input. The
  copy preserves the Gate bytes and executable mode resolved before Run side
  effects, so fixture or worktree edits cannot change the active Run.

The Run definition contains one required Item execution graph and optional
Before-Run and After-Run execution graphs. Graphs retain declarative,
order-independent rows and an explicit start node. Both node types default
`on_success` to `DONE` and `on_failure` to `HUMAN`; every other destination must
name a declared node. The replacement declaration vocabulary is
`execution_graph`, `runtime_node`, and `gate_node`; the old Phase graph API has no
compatibility aliases.

Gate placement is entirely project-owned. PyCastle neither inserts hidden Gate
runs nor verifies that every path to `DONE` traverses a Gate node. The initialized
fixture supplies conservative Item and After-Run graphs with Gate nodes, but a
project may declare zero, one, or multiple Gate nodes in any graph.

A node's observable execution outcome alone selects its edge. A Runtime node
succeeds when its Runtime invocation completes successfully and fails on a
nonzero exit, launch failure, timeout, or malformed Runtime result. PyCastle does
not interpret generated prose. A Gate node succeeds only on exit zero and fails
on nonzero exit, signal termination, or launch failure. Setup failure, operator
cancellation, and host-side orchestration failure are outside graph control flow
and stop the Run under their lifecycle rules; ADR-0014 defines Setup's rule.

Retry is graph topology, not a second retry subsystem or a `retries=N` property.
An edge may revisit any Runtime or Gate node. Each graph walk maintains an
independent visit count for every node identity: Item counters reset for every
Item, and Before-Run and After-Run walks have separate counters. PyCastle applies
one fixed, non-configurable safety limit of 10 visits uniformly to both node
types. Attempting an eleventh entry terminates the graph at `HUMAN` without
running Setup, invoking the node, or following another edge.

Filesystem changes survive both successful and failed node outcomes for the rest
of the graph walk. The worktree and its diff are the durable communication
channel; a failure destination may inspect or repair partial changes. Cleanup or
preservation happens at graph and Run lifecycle boundaries, not automatically
after a failed node.

Every Runtime node visit starts a fresh Runtime invocation and provider
conversation, including when a cycle revisits the same node. Thread identifiers
are audit metadata and are never resumed automatically. PyCastle gives a Runtime
node only its frozen project prompt, factual Item or Run envelope, and the typed
outcome of its immediate predecessor. Context is one-hop and never accumulates:

- Runtime success carries source node identity and success only; generated output
  and transcripts are excluded.
- Runtime failure also carries bounded process or crash diagnostics, but not
  generated prose.
- Gate success or failure carries source node identity, status, a typed
  termination (`exited`, `signaled`, or `launch_error`), and bounded, redacted
  stdout and stderr. An exited process carries its exact exit code, a signaled
  process carries the exact signal, and a launch error carries its OS error kind,
  number, and message; PyCastle never fabricates an exit code.

A Gate node receives no incoming edge context through arguments, standard input,
or environment. It receives only the canonical executable, target worktree, and
scope. Runtime transcripts remain audit records rather than prompt context. The
diagnostic size and redaction representation are specified separately.

Execution capture is invariant across presentation modes. Verbosity is applied
only when rendering saved human-facing logs after node execution; it cannot
change a Runtime or Gate invocation, which events PyCastle records, the typed
outcome projected onto an edge, or graph behavior. Runtime adapters therefore
use one stable invocation shape rather than requesting different provider output
when verbose logging is enabled.

## Rationale

One graph model makes Runtime work, verification, recovery, and retry equally
project-owned. Explicit node types preserve the different execution mechanisms
without special node names or implicit lifecycle Gates. Fresh Runtime visits and
one-hop factual context keep independent review possible, while persistent
worktree state gives intentional repair nodes the code they need. A fixed visit
limit guarantees termination without turning retry count into a second policy
surface.

## Consequences

- The `implement` name has no special meaning, the implement-only retry loop and
  Runtime thread resumption disappear, and CLI/config retry controls disappear.
- Graph loading, validation, execution, scaffolding, prompts, telemetry, and tests
  adopt Execution graph terminology with no migration layer.
- Runtime and Gate capture become always-on execution concerns, while verbose
  transcript rendering becomes a separate observer of those captured records.
- ADR-0004's declarative-row rationale is retained here, but its Phase types and
  API are no longer current.
- ADR-0008 remains authoritative for the surrounding Run lifecycle, Item/Run
  worktrees, checkpointing, publication, and non-graph failure handling except
  where those rules depend on the superseded Phase, Gate, retry, context, or
  failed-node worktree behavior.
