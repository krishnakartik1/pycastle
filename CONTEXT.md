# PyCastle

A reusable, installable autonomous development loop: PyCastle owns the runner; a
project owns its prompts, gates, and workflow graph. This glossary fixes the
language so issues, ADRs, and code use one word per concept.

## Language

### The loop

**Run**:
One bounded pass that turns up to N ready work items into a single pull request.
Cuts a per-run branch, works each item on its own branch off it, folds the clean
ones back, and opens one PR for the batch.
_Avoid_: job, session, cycle.

**Phase**:
One step the agent performs on an item (e.g. plan, implement, review), driven by
its own prompt. Each phase names a success and a failure destination.
_Avoid_: stage, step.

**Phase graph**:
The project-owned graph of phases and their success/failure edges, authored in
`.pycastle/main.py`, that a run walks per item until it reaches a terminal.
_Avoid_: workflow, pipeline.

**Gate**:
The project-owned quality check run after the implement phase; a non-zero exit
means the attempt failed and is retried with a handoff.
_Avoid_: check, CI, test step (the gate may *run* tests, but it is not "the tests").

**Handoff**:
The document a failed attempt leaves for the next attempt, summarizing what was
tried and what to fix.
_Avoid_: note, summary.

### The pieces

**Project fixture**:
The `.pycastle/` directory a project owns and PyCastle reads: prompts, the gate,
the phase graph, the agent Dockerfile, and the sandbox marker. Scaffolded by
`pycastle init`.
_Avoid_: config, template, project config.

**Issue source**:
The boundary work items come from. v0.1 is GitHub Issues via `gh`; an item is
ready when it carries the `ready-for-agent` label.
_Avoid_: backlog, queue, tracker.

**Runtime**:
The agent CLI that performs a phase, behind one interface. The shipped runtimes
are `claude`, `codex`, and `stub`. Which runtime runs is a flag, not a code
change.
_Avoid_: agent, model, provider, engine.

### Sandbox and image

**Sandbox**:
*Where* a runtime executes: `host` (on the machine, isolated only by the per-item
git worktree) or `docker` (inside the agent container). Orthogonal to which
runtime runs.
_Avoid_: environment, isolation mode.

**Agent image**:
The container image a `docker`-sandbox run executes the runtime inside. Selected
by `--image`, else built on demand from the project's Dockerfile, else the
default tag (see ADR-0005).
_Avoid_: container, box, sandbox image.

**Image contract**:
What any agent image must satisfy to be runnable: the runtime CLI on PATH, the
project's gate toolchain on PATH (the gate runs in this image, not on the host),
a `node` user with home `/home/node`, write access to the mounted auth volume and
the bind-mounted workspace, and honoring the runtime's config-dir env var. The
scaffolded Dockerfile is one adapter that satisfies it.
_Avoid_: requirements, spec.

**Auth volume**:
The per-runtime Docker volume holding a subscription login, shared across every
project — you log in once per runtime, not once per repo.
_Avoid_: credentials mount, secret.

## Flagged ambiguities

- **"Runtime" vs "agent".** The thing that performs a phase is the **Runtime**
  (`claude`/`codex`/`stub`). Reserve "agent" for informal prose; prefer
  "Runtime" in code, issues, and ADRs.
- **"Build itself" / "dogfood".** PyCastle running its own loop against its own
  repo. Not a glossary concept — a way of using the tool; keep it out of titles
  in favor of naming the actual Run.

## Example

> **Dev:** The Docker run can't find pytest in the container.
> **Maintainer:** Right — in a `docker` sandbox the **gate** runs *inside* the
> **agent image**, same as the phases, so the image must carry the project's
> toolchain. That's part of the **image contract** now; add pytest to your
> Dockerfile. If the gate runs and finds no tools at all, it fails loudly rather
> than passing silently.
> **Dev:** So if I bring my own image with `--image`?
> **Maintainer:** Then PyCastle runs it as-is and never builds the Dockerfile —
> but it still has to meet the contract: the `node` user, the **runtime** CLI,
> and your gate toolchain.
