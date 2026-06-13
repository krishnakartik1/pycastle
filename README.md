# PyCastle

A reusable, installable autonomous development loop for any repository.

PyCastle is the Python successor to the project-bound "Ralph" loop. It gives any
repo the same plan → implement → review → merge cycle, lets each repo own the
shape of its own workflow, supports both Claude Code and Codex as the runtime,
and can run the agent inside Docker without forcing API-key billing.

> **Status:** v0.1 in progress. This is the walking skeleton — `pycastle run`
> works a single ready issue end to end through a stub runtime. Real Claude and
> Codex adapters, the Docker sandbox, batch runs, retries, and `pycastle init`
> land in subsequent slices. See the GitHub issues for the roadmap.

## Install (development)

```bash
pip install -e ".[dev]"
```

## Commands

- `pycastle run -i N --runtime stub` — work up to N ready issues into one PR.
- `pycastle init` — scaffold a `.pycastle/` fixture into a repo *(coming soon)*.
- `pycastle sandbox setup --runtime claude|codex` — log a runtime into its
  Docker auth volume *(coming soon)*.

## How it fits together

PyCastle owns the reusable runner; a project owns its prompts, gates, and the
workflow graph in `.pycastle/main.py`. The runner is composed from small, deep
modules behind stable interfaces:

- **Runtime** — one interface over Claude Code and Codex.
- **Issue source** — pluggable; v0.1 ships GitHub Issues via `gh`.
- **Phase graph** — an executable Builder-style graph in `.pycastle/main.py`.
- **Orchestrator** — selects, claims, branches, runs the graph, and opens a PR.
