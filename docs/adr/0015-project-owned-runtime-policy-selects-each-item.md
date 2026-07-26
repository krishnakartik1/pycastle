# ADR-0015: Project-owned Runtime policy selects each Item

Status: Accepted (2026-07-26)

## Context

PyCastle currently filters ready Issues, orders them by ascending Issue number,
takes the Run limit, and freezes that ordered Item batch during Run readiness.
That makes project-specific priority and dependency judgments part of the
reusable runner. A runner-owned declarative rule vocabulary would keep those
judgments deterministic, but would require PyCastle to understand and continually
expand project scheduling semantics.

AgentRoster instead presents the current candidates and repository context to a
project-authored Runtime prompt before each Item. That permits semantic judgments
about priority and dependencies while keeping Issue-source mutation ownership in
the runner.

## Decision

Run readiness freezes one complete **Item candidate pool** snapshot, not an
ordered Item batch. Candidate membership and all policy-visible Item facts,
including title, body, comments, labels, and assignees, remain fixed for the
Run; Items that become ready or change later wait for another Run to affect
selection. The project-owned **Item selection policy** chooses one remaining
candidate before each Item, informed by the Run's completed and skipped work.
PyCastle validates and claims the choice, never allows more than the Run Item
limit, and remains the sole owner of Issue-source mutations.

The candidate pool contains every mechanically eligible Item returned through
complete Issue-source pagination, independent of the Run Item limit. Ascending
Item number is only the canonical storage and presentation order and carries no
selection meaning. The limit caps claimed Item attempts: a claimed Item consumes
one slot regardless of its graph or integration outcome, while an Item rejected
as stale before claim consumes none. Reaching the limit ends selection normally.

Mechanical eligibility remains PyCastle-owned coordination policy. A candidate
must be open, carry the ready-for-agent label, and be assigned to the resolved
Run assignee; the explicit include-unassigned option additionally admits
literally unassigned Items. Items assigned only to someone else are never
offered to selection. Project policy may interpret labels and assignees as
frozen facts but cannot override claim admissibility.

Immediately before a claim, PyCastle refreshes only the selected Item's
mechanical eligibility: open state, ready label, and assignee compatibility.
A stale Item is recorded, skipped, and removed from the remaining pool without
updating the frozen policy facts, adding a newly eligible candidate, or changing
earlier selection decisions. Because it was never claimed or worked, it does
not consume a Run Item slot; PyCastle invokes selection again with the stale
outcome in Run progress. Each frozen candidate can become stale only once, so
reselection remains bounded by the pool size. An Issue-source error during the
recheck is a Run failure rather than evidence that the Item is stale.

The **Run definition** contains one required **Item definition**, which pairs the
Item selection policy's frozen Runtime prompt with the required Item execution
graph. The lifecycle is:

1. walk the optional Before-Run execution graph once;
2. invoke Item selection;
3. walk the selected Item's execution graph;
4. repeat selection and Item execution up to the Run Item limit;
5. walk the optional After-Run execution graph when integrated work exists.

The selection prompt contains only project-authored directions for choosing an
Item. At invocation time, PyCastle prepends the complete frozen facts for every
remaining candidate and explicit Run progress. This mirrors Item execution:
Runtime-node prompt files contain project-authored processing directions while
PyCastle prepends the selected Item's complete frozen Issue body and comments.
Issue content is dynamic factual input, never embedded in the Item definition or
Project fixture.

Before-Run and After-Run Runtime nodes retain a bounded factual envelope rather
than receiving every Issue body and comment. Before-Run nodes receive the
candidate index as number, title, and candidate state. After-Run nodes receive
the same complete index with final states such as completed, skipped, stale, or
not selected, plus why Item selection ended. Complete candidate content is
specific to the selection operation; complete single-Item content is specific
to that selected Item's execution graph.

Item selection is a Run-scope Runtime operation, not an Execution graph node.
It returns an Item identity rather than choosing a success or failure edge, and
it must run before PyCastle can create an Item branch, worktree, or Item-scope
context.

The selection response names exactly one remaining candidate and gives a
bounded local audit reason, or returns no Item with a reason. A deliberate
no-Item response ends the Item portion of the Run without mutating the
remaining candidates. If integrated work exists, PyCastle continues to the
After-Run graph, Gate, and publication; otherwise it cleans up without a pull
request. This outcome is not readiness `no_work`: the candidate pool existed,
but project policy found no actionable Item. Malformed output or selection
outside the remaining pool is a selection failure, never an implicit no-Item
decision.

Each selection round invokes the Runtime exactly once. PyCastle does not retry
Runtime crashes, timeouts, malformed output, or invalid choices; no
Issue-source mutation occurs unless that one response validates. Setup retains
ADR-0014's separate no-retry contract.

PyCastle appends a fixed, non-project-owned protocol block after the factual
candidate envelope and project-authored selection directions. The block
repeats the allowed candidate numbers, forbids changes, and requires exactly
one tagged JSON response:

```text
<selection>
{"item": 42, "reason": "Bounded local audit reason."}
</selection>
```

The Item value is a positive Issue number or `null`; the reason is a non-empty
bounded string. PyCastle accepts exactly one opening and closing selection tag,
parses the enclosed value as one JSON object with only those two fields, rejects
booleans and other non-integer Item values, and verifies a non-null number
against the remaining frozen pool. Missing, repeated, malformed, oversized, or
out-of-pool output fails selection. Text outside the one block is retained as
local Runtime telemetry but has no protocol meaning. Prompt instructions help
the Runtime comply; this parser and validation are the enforcement boundary.

The exact candidate envelope, frozen prompt identity, Runtime transcript,
parsed response, and validation result are retained only in ignored local Run
records. Normal console output exposes the selected Item number and title;
verbose mode may stream the selection transcript. Model-authored reasons, raw
candidate content, and selection output are never published automatically.
Pull-request facts contain only bounded Item identities and outcomes, while a
project-authored Run report may deliberately explain ordering.

PyCastle does not claim that a Runtime policy is deterministic across separate
Runs. It freezes and records every input that PyCastle owns, invokes selection
once, validates the response, and treats an accepted decision as authoritative
for that Run. Repeating an otherwise identical Run may produce a different
order; auditability comes from the exact local record rather than replaying the
model to reproduce its judgment.

A selection failure stops the Run outside Execution graph control flow. With no
integrated Item, PyCastle removes the Run branch and worktrees and retains only
the local Run record. With at least one integrated Item, it preserves the last
Run-branch checkpoint in a draft pull request, skips the After-Run graph and
Run Gate, and publishes only safe failure metadata. A valid no-Item response is
different: it ends Item work normally and therefore permits ordinary After-Run
execution, verification, and publication.

Selection uses the ordinary writable Runtime permissions in the selected
Sandbox. PyCastle's factual prompt envelope tells the Runtime to inspect only
and forbids repository or external mutations, but this is a behavioral contract,
not a security boundary. Each invocation runs after Run-scope Setup in a fresh
disposable worktree at the latest Run-branch checkpoint. PyCastle captures the
structured response, removes the entire selection worktree regardless of its
contents, verifies that the durable Run branch and worktree did not change, and
only then may claim the selected Item. PyCastle does not inject Issue-source
credentials into selection and scrubs the standard GitHub token environment,
`gh` configuration, Git credential-helper and askpass paths, and SSH-agent
channel for the invocation. On a host Sandbox, this narrows the conventional
credential channels but cannot hide arbitrary files already readable by the
user; the selected Sandbox remains the security boundary.

If verification detects a selection-time mutation, PyCastle restores only its
own Run branch and Run worktree to the captured pre-selection commit. When
integrated work must be preserved in a draft pull request, the final push names
that captured commit explicitly rather than trusting the mutable branch ref.
PyCastle does not reset the operator's checkout or other user-owned refs or
worktrees.

Selection uses the Run's one selected Runtime, model, Sandbox, pinned Agent
image, and authentication path. The Item definition declares only the selection
prompt and Item execution graph; it cannot select separate planner
infrastructure, and the CLI exposes no selection-specific Runtime or model
override.

Selection has no operation-specific timeout, turn budget, or retry setting. It
inherits the ordinary Runtime invocation and cancellation lifecycle. A general
Runtime deadline, if introduced, applies consistently to Runtime operations
rather than creating a planner-only control that Runtime adapters cannot share.

## Consequences

- ADR-0013's requirement to freeze an exact ordered Item batch is superseded.
  Run readiness instead freezes the candidate pool used by later selections.
- ADR-0008's fixed ordered batch passed to Run-scope work is superseded. The
  Before-Run graph precedes selection, and later selection receives explicit Run
  progress.
- Doctor can prove that candidates exist and execution can start, but cannot
  predict the Runtime policy's eventual Item order without executing project
  work.
- Selection and Item execution use the same frozen Item facts. Mid-Run Issue
  edits can make an Item stale at claim time but cannot reprioritize the Run.
- Project prompts, rather than PyCastle or GitHub relationship APIs, interpret
  priority and dependency meaning.
- Writable selection requires no new cross-Runtime read-only capability.
  Disposable worktrees contain ordinary accidental file changes, while the
  selected Sandbox remains the actual isolation boundary.
- A Project fixture without an explicit Item definition and Item selection
  policy is migration-required. PyCastle provides no implicit
  lowest-Issue-number or other runner-owned compatibility policy.
- Adoption is an owner-authored, two-step fixture migration. The first
  `pycastle upgrade` makes no changes and directs the owner to add and review a
  selection prompt and wrap the existing Item execution graph in an Item
  definition. After those changes are committed, a second Upgrade validates the
  complete target-release fixture and advances its version marker. Upgrade does
  not rewrite customized executable Python or invent policy on the project's
  behalf; `pycastle init` may scaffold an explicitly project-owned starter
  policy for new fixtures.
- New fixtures scaffold a visible semantic selection prompt that asks the
  Runtime to choose the highest-priority actionable Item, infer dependencies
  from candidate and repository context, prefer work that unblocks other
  candidates, honor clearly expressed project priority labels, use lower Issue
  number only as a final tie-breaker, and return no Item when none is
  actionable. Once initialized, that file is wholly project-owned.
- The deterministic stub Runtime treats Item selection as a protocol fixture:
  it returns the lowest numbered remaining candidate with a fixed reason, makes
  no selection-worktree change, and returns no Item for an empty pool. This
  preserves end-to-end plumbing tests and is not a production fallback policy;
  real Runtimes execute the project-owned prompt.
