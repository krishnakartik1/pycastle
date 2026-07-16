# PyCastle

A reusable, installable autonomous development loop: PyCastle owns the runner; a
project owns its prompts, gates, and workflow graph. This glossary fixes the
language so issues, ADRs, and code use one word per concept.

## Language

### The loop

**Run readiness**:
A current PyCastle-owned snapshot whose outcome is `ready`, `no_work`, or
`not_ready` for one resolved Run configuration. `ready` means PyCastle may
allocate a Run and attempt **Setup**; it neither reserves Items nor asserts that
project preparation or verification will pass.
_Avoid_: preflight, dry Run, project readiness, verification.

**Doctor**:
A non-destructive **Run readiness** operation. It may build the canonical
**Agent image** and probe PyCastle's language-neutral **Image contract**, but
never executes **Setup**, the **Gate**, an **Execution graph**, or a Runtime
prompt; a later Run always evaluates its own snapshot.
_Avoid_: dry Run, canary Run, health score, reservation.

**Run**:
One bounded pass that turns up to N ready work items into a single pull request.
Cuts a per-run branch, works each item on its own branch off it, folds the clean
ones back, and opens one PR for the batch.
_Avoid_: job, session, cycle.

**Runtime node**:
An **Execution graph** node that asks a **Runtime** to perform prompt-driven work
at Item or Run scope. It names a success and a failure destination.
_Avoid_: phase, agent node, prompt step.

**Gate node**:
An **Execution graph** node that applies the project-owned **Gate** to the current
worktree. It names a success and a failure destination like a Runtime node.
_Avoid_: gate phase, check step, implicit gate.

**Execution graph**:
A project-owned directed graph of **Runtime nodes**, **Gate nodes**, and their
success/failure edges, walked from one explicit start node to a **Terminal**.
_Avoid_: phase graph, workflow, pipeline.

**Item execution graph**:
The required **Execution graph** a Run walks once for each Item.
_Avoid_: Item phase graph, workflow, pipeline.

**Before-Run execution graph**:
An optional **Execution graph** walked at Run scope before any Items are worked.
_Avoid_: before-Run phase graph, pre-hook, pipeline.

**After-Run execution graph**:
An optional **Execution graph** walked at Run scope after Item work is integrated.
_Avoid_: after-Run phase graph, post-hook, pipeline.

**Run definition**:
The project-owned declaration that combines one required **Item execution
graph** with optional **Before-Run execution graph** and **After-Run execution
graph** declarations.
_Avoid_: workflow, pipeline, full graph.

**Terminal**:
A destination that ends an **Execution graph**. `DONE` completes the current
scope; `HUMAN` stops autonomous progression and hands that scope to a person.
_Avoid_: exit, return state.

**Gate**:
The mandatory project-owned verification policy embodied by one canonical
executable at both Item and Run scope. A **Gate node** applies it, and the
result selects the graph's success or failure edge.
_Avoid_: check, CI, test step (the gate may *run* tests, but it is not "the tests").

**Setup**:
The mandatory project-owned `.pycastle/setup` executable that establishes the
durable prerequisites derived from the current worktree. It is idempotent,
stores its results in mounted project-owned state, applies at both Item and Run
scope, and cannot rely on shell or Sandbox process state surviving. A ready Run
first invokes it at Run scope before Item state changes, then again immediately
before every Runtime or Gate node.

**Handoff**:
The document a failed attempt leaves for the next attempt, summarizing what was
tried and what to fix.
_Avoid_: note, summary.

**Run report**:
A project-authored artifact that summarizes the integrated Run for its pull
request. A Run-scope Runtime node writes its content; PyCastle alone publishes it.
_Avoid_: PR comment, final review, test report.

### The pieces

**Project fixture**:
The `.pycastle/` directory a project owns and PyCastle reads: prompts, **Setup**,
the Gate, the **Run definition**, the agent Dockerfile, the sandbox marker, and
the release marker used to verify fixture compatibility before a Run.
Scaffolded by `pycastle init`.
_Avoid_: config, template, project config.

**Issue source**:
The boundary work items come from. v0.1 is GitHub Issues via `gh`; an item is
ready when it carries the `ready-for-agent` label.
_Avoid_: backlog, queue, tracker.

**Runtime**:
The agent CLI that performs a **Runtime node**, behind one interface. The shipped
runtimes are `claude`, `codex`, and `stub`. Which Runtime runs is a flag, not a
code change.
_Avoid_: agent, model, provider, engine.

**Runtime login**:
The explicit operation that authenticates one **Runtime** in the selected
**Sandbox**. Host login uses the Runtime's normal host configuration; Docker
login uses its shared **Auth volume** and the project's **Agent image**.
_Avoid_: sandbox setup, Setup, authentication phase.

### Sandbox and image

**Sandbox**:
The bootstrap environment in which PyCastle invokes **Setup**, **Runtime nodes**,
and **Gate nodes**: `host` reuses the externally provisioned machine, while
`docker` uses the Run's pinned **Agent image**. The same project-owned execution
protocol applies in both, and PyCastle provisions neither language environment.
A Docker Sandbox persists only its mounted repository workspace and selected
**Auth volume**; each container's private filesystem is disposable, and ambient
host environment variables are not forwarded into it.
_Avoid_: environment, isolation mode.

**Agent image**:
The project-owned immutable bootstrap environment for a `docker` **Sandbox**,
built from the canonical `.pycastle/Dockerfile`. It supplies the selected
**Runtime** and tools needed to launch project execution; **Setup** materializes
worktree-specific prerequisites into mounted state. A **Run** pins one built
image identity before side effects and uses it for every container it starts.
_Avoid_: container, box, sandbox image.

**Image contract**:
The language-agnostic boundary any **Agent image** must satisfy so PyCastle can
execute the selected **Runtime**, **Setup**, and **Gate** in a writable Docker
**Sandbox**. The image declares its own non-root user and writable home and must
allow that user to write the worktree and PyCastle's neutral auth mount; PyCastle
does not prescribe an identity or supply the project's language toolchain.
_Avoid_: requirements, spec.

**Auth volume**:
The per-**Runtime** Docker volume holding a subscription login, shared across
projects and mounted alone at `/pycastle/auth` for the selected Runtime.
_Avoid_: credentials mount, secret.

## Flagged ambiguities

- **"Runtime" vs "agent".** The thing that performs a Runtime node is the **Runtime**
  (`claude`/`codex`/`stub`). Reserve "agent" for informal prose; prefer
  "Runtime" in code, issues, and ADRs.
- **"Phase" vs "node".** Runtime work and Gate verification are both nodes in
  one **Execution graph** model. Use **Runtime node** or **Gate node** and avoid
  the superseded phase terminology.
- **"Build itself" / "dogfood".** PyCastle running its own loop against its own
  repo. Not a glossary concept — a way of using the tool; keep it out of titles
  in favor of naming the actual Run.

## Example

> **Dev:** The Docker Run cannot launch Setup because the package manager is
> missing.
> **Maintainer:** Add that bootstrap tool to the project-owned
> `.pycastle/Dockerfile`. The **Agent image** supplies immutable tools; **Setup**
> derives the current worktree's dependencies into mounted project state before
> each executable node. PyCastle pins one built image for the whole Run.
> **Dev:** Does a host Sandbox use a different preparation hook?
> **Maintainer:** No. It invokes the same **Setup** contract, but the host must
> already provide the Runtime and tools needed to launch it. PyCastle does not
> install or activate a language environment in either Sandbox.
> **Dev:** Where does an integrated review fit when one **Run** contains several
> items?
> **Maintainer:** The **Run definition** wraps the required **Item execution
> graph** with optional **Before-Run execution graph** and **After-Run execution
> graph** declarations. The after graph can review and repair the integrated
> branch, apply the **Gate** through a Gate node, and author a **Run report**.
