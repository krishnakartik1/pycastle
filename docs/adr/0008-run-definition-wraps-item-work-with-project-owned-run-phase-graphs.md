# ADR-0008: A Run definition wraps item work with project-owned Run phase graphs

Status: Accepted (2026-07-15)

## Context

PyCastle currently loads one project-owned phase graph and walks it independently
for each selected issue. Successful issue branches are folded into the Run branch,
which is then pushed and presented as a pull request without any project-owned
operation reviewing or repairing the integrated diff. Gate output and Runtime
transcripts remain local Run records, while the pull request contains only its
closing issue references.

An integrated review implemented only in this repository would make the behavior
project-specific. Implementing it in CI would occur after PyCastle has finished,
would require another authenticated agent capable of committing fixes, and would
split one development loop across two orchestration systems. Allowing a Runtime
prompt to mutate GitHub directly would instead expose credentials to project
execution, make retries non-idempotent, and violate ADR-0007's boundary between
Sandboxed project execution and host-side orchestration.

## Decision

Replace the fixture's module-level Item phase graph with one module-level **Run
definition**. The declaration contains one required **Item phase graph** plus
optional before-Run and after-Run **Run phase graphs**:

```python
run = build_run(
    before=None,
    item=build(...),
    after=build(...),
)
```

Backward compatibility with the old module-level `graph` export is not required.
Each contained graph uses the existing declarative phase rows, explicit success
and failure edges, `DONE` and `HUMAN` terminals, and bounded visit behavior. One
before graph and one after graph may each contain any number of phases. The Item
phase graph is still walked sequentially once per selected item; Item phases are
not parallelized.

The project fixture and image are resolved once at Run start. Edits made by a Run
phase to the fixture are ordinary proposed changes for the pull request and take
effect on the next Run; they cannot reconfigure the active Run or weaken its
canonical Gate.

### Run lifecycle

For a non-empty selected batch, PyCastle performs this lifecycle:

1. Resolve preflight requirements, the project fixture, and the Agent image.
2. Select and freeze the ordered batch of items.
3. Create the Run branch and worktree.
4. Run Setup in the Run worktree, then walk the optional before-Run graph.
5. For each selected item in order, create its branch from the latest Run branch,
   run Setup, walk the Item phase graph, apply its Item Gate/retry behavior, and
   fold successful work back into the Run branch.
6. If at least one item completed, run Setup again in the integrated Run worktree,
   walk the optional after-Run graph, and run the mandatory integrated Run Gate.
7. Push the final branch, create a draft pull request, publish its Run report
   comment, and mark a successful pull request ready for review.

Setup remains one project-owned, idempotent executable. It runs at Item scope and
Run scope in the same Sandbox and resolved image as the phases and Gate. The Gate
also remains one project-owned executable applied at both scopes. The Run Gate
runs even when no after-Run graph is configured.

If no items are selected, the Run remains today's no-op success and does not
create a branch, worktree, run Setup, walk graphs, run a Gate, or open a pull
request. If every selected item is skipped or reaches `HUMAN`, the preparatory
before-Run work is not independently deliverable: PyCastle skips the second Run
Setup, after-Run graph, Run Gate, and pull request.

### Context and communication

PyCastle owns the factual prompt envelope. The before-Run graph receives the
fixed ordered batch and may analyze it but cannot add, remove, or reorder items.
PyCastle retains that batch and passes each item to the Item phase graph in turn.
Item branches start from the latest Run branch, so committed before-Run changes
are inherited. After each item, PyCastle records its outcome for the later
after-Run graph.

Runtime calls remain fresh: PyCastle does not automatically chain conversational
history or free-form Runtime output between phases. Phases communicate through
their shared worktree using project-owned, ignored scratch files. The default
Item Plan writes `.pycastle/plan.md`, and the default Item Implement prompt must
explicitly read it. The default after-Run phases use the same convention:

- `review` writes `.pycastle/run-review.md` with integrated findings;
- `repair` reads those findings and is a no-op when none need fixing;
- `report` inspects the repaired diff and writes `.pycastle/run-report.md`.

The report is optional to PyCastle. Projects that require it enforce its presence
in their Run Gate. PyCastle harvests a generated report into the ignored records
for that Run before worktree cleanup. A generalized artifact directory or prompt
path-injection contract is deferred.

### Commits and durability

After every successful Run phase, PyCastle stages non-artifact changes and creates
a deterministic checkpoint commit when the worktree is dirty. A phase that
already committed needs no redundant or empty commit. If a phase fails, its
uncommitted edits are discarded while checkpoints from earlier successful phases
remain.

PyCastle pushes after every successful before-Run phase checkpoint, successful
item fold, and after-Run phase checkpoint. Intermediate push failures are logged
and are non-fatal; a later checkpoint retries the complete current branch. The
final push must succeed before any normal or draft pull request is opened. This
extends the incremental durability decision made for item folds to Run phases.

### Pull-request publication

Project prompts author content but never call GitHub. PyCastle alone owns `git`,
`gh`, pull-request creation, comments, and readiness transitions on the host, as
required by ADR-0007. Runtime Sandboxes receive no GitHub credentials for this
purpose.

Publication uses one bounded Markdown **Run report**, not a generic attachment or
artifact-upload system. Full raw logs, transcripts, and reports remain in the
ignored local Run records. The pull-request comment contains:

1. a fixed PyCastle-authored factual envelope with the Run ID, completed and
   skipped items, draft/complete state, and safe final Gate metadata; then
2. the project-authored Run report verbatim, when present.

Safe Gate metadata includes the command identity, result, exit code, and duration.
Raw Gate stdout and stderr are not published automatically because they may be
large or contain secrets. A project deliberately includes curated test, lint, or
coverage evidence in its Run report.

A missing optional report is not an error. A present report that is invalid UTF-8
or exceeds PyCastle's publication limit is a publication failure: PyCastle opens
a draft pull request, publishes the factual envelope with a visible validation
error, leaves the pull request draft, and exits nonzero without silently
truncating the report.

Even on a successful Run, PyCastle initially creates the pull request as a draft,
publishes the factual envelope and optional report, and marks it ready only after
publication succeeds. Publication is idempotent inside the Run: PyCastle locates
the pull request by Run branch and upserts one hidden-marker report comment keyed
by Run ID rather than creating duplicates. A user-facing command for resuming
publication after the process exits is deferred.

### Failure and interruption

- A before-Run graph that reaches `HUMAN` stops before item claims and produces no
  pull request. Incomplete changes are discarded; records are retained.
- An after-Run graph that reaches `HUMAN` discards the failed phase's uncommitted
  edits, retains prior checkpoints, runs the Run Gate against the last committed
  branch, and opens a draft pull request with the stopped phase and Gate result.
- A red Run Gate is the Run-level equivalent of `HUMAN`: PyCastle does not replay
  phases automatically, opens a draft pull request, and exits nonzero. Projects
  express repair passes explicitly inside their after-Run graph.
- Failure of the second Run Setup skips the after-Run graph and Run Gate, then
  opens a draft pull request describing the failure and completed items.
- A handled infrastructure failure after at least one completed item releases the
  in-flight item, leaves later items untouched, skips after-Run work and the Run
  Gate, and opens a draft pull request for the durable completed work.
- Operator cancellation stops immediately, releases only the in-flight item,
  preserves pushed checkpoints and local records, performs best-effort worktree
  cleanup, opens no pull request, and reports the remote branch and cleanup path.
  `SIGKILL` and power loss can guarantee only work already pushed.

These Run-level failures do not rewrite the successful per-item outcomes or apply
new per-item labels merely because integration stopped.

## Rationale

The Run definition keeps policy project-owned while giving PyCastle one coherent
loop spanning preparation, independent item work, integrated repair, verification,
and publication. Full graphs preserve customization without adding special hook
semantics. Shared worktree scratch files reuse the phase-communication mechanism
already used by Plan and avoid a new artifact protocol in this version.

The mandatory Run Gate remains an orchestrator-observed boundary rather than an
agent assertion. Draft-first publication gives GitHub mutations a visible partial
state: a pull request cannot look ready while its report failed to publish.

## Alternatives rejected

- **Project-only script outside PyCastle.** Cannot participate coherently in Run
  state, failure handling, Sandbox selection, checkpointing, or publication.
- **CI performs the final review and fixes.** Splits the loop, requires a second
  authenticated agent, and occurs after PyCastle has declared the Run finished.
- **A prompt posts directly to the pull request.** Exposes credentials to the
  Sandbox and makes retries, duplicate suppression, and cancellation unreliable.
- **One fixed pre/post hook.** Cannot express multiple project-owned phases,
  branches, or terminals and creates a second workflow model beside phase graphs.
- **Automatic conversational context chaining.** Couples phases to Runtime thread
  behavior and risks unbounded context; explicit scratch files are inspectable and
  customizable.
- **Generic artifact uploads.** GitHub pull requests have no simple portable file
  attachment API; Actions artifacts or object storage would add another system.
- **Raw Gate-log publication.** Risks secret disclosure and unbounded comments.

## Consequences

- Graph loading, scaffolding, orchestration, logging, checkpointing, and pull-
  request publication all change around the new Run definition.
- The scaffold has `before=None`, retains its Item Plan/Implement/Review graph,
  and adds the default after-Run `review -> repair -> report` graph.
- Existing Project fixtures must adopt the new `run = build_run(...)` declaration.
- The integrated Run may end as a draft pull request even when some items completed;
  this is deliberate evidence that integration or publication needs attention.
- A future version may add publication recovery and a generalized Run artifact
  contract without changing the core ownership boundary established here.
