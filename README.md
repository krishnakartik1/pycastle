# PyCastle

A reusable, installable autonomous development loop for any repository.

PyCastle is the Python successor to the project-bound "Ralph" loop. It gives any
repo the same plan → implement → review → merge cycle, lets each repo own the
shape of its own workflow, supports both Claude Code and Codex as the runtime,
and can run the agent inside Docker without forcing API-key billing.

> **Status:** v0.1 in progress. This is the walking skeleton — `pycastle run`
> works a single ready issue end to end through a stub or the real Claude or
> Codex runtime, on the host or inside the Docker agent sandbox. Batch runs,
> retries, and `pycastle init` land in subsequent slices. See the GitHub issues
> for the roadmap.

## Install (development)

```bash
pip install -e ".[dev]"
```

## Commands

- `pycastle run -i N --runtime stub` — work up to N ready issues into one PR.
- `pycastle run --sandbox docker --runtime claude` — run the loop with the
  Claude runtime *inside* the Docker agent sandbox (see below).
- `pycastle run --sandbox docker --runtime codex` — same loop with the Codex
  runtime; switching runtime is just the flag.
- `pycastle init` — scaffold a `.pycastle/` fixture into a repo *(coming soon)*.
- `pycastle sandbox setup --runtime claude` — log the Claude runtime into its
  Docker auth volume (browser login), then confirm auth from a fresh container.
- `pycastle sandbox setup --runtime codex` — log the Codex runtime into its
  Docker auth volume via the device-authorization flow (a printed code and URL,
  no localhost callback or TTY).

## The Docker agent sandbox

`--sandbox docker` runs the agent inside a container instead of on the host, so
both the runtime and every command it invokes are isolated. The container runs
as the non-root user `node` (home `/home/node`), based on a `node:22` image, and
bind-mounts the project workspace so the agent reads and writes the real tree.

Auth is a subscription login stored in a Docker volume — one volume **per
runtime**, shared across every project. Claude's volume is `pycastle-claude-auth`,
mounted at `/home/node/.claude` with `CLAUDE_CONFIG_DIR` pinned to it; Codex's is
`pycastle-codex-auth`, mounted at `/home/node/.codex` with `CODEX_HOME` pinned to
it. You log in once per agent, not once per repo. Credential file contents are
never read, printed, or copied; for Claude, `sandbox setup` confirms auth only by
having the agent answer a one-word prompt from a fresh container.

Onboard auth with:

```bash
pycastle sandbox setup --runtime claude
```

**Headless token fallback.** `sandbox setup` runs the interactive browser login,
which needs a TTY. On a headless host (CI, a server with no browser), either run
the login on a machine that has a browser and move the resulting credentials
into the same named volume, or skip the volume and pass a long-lived token into
the container via the `CLAUDE_CODE_OAUTH_TOKEN` environment variable. The token
is read from the host environment at run time and never written to the command
line.

## How it fits together

PyCastle owns the reusable runner; a project owns its prompts, gates, and the
workflow graph in `.pycastle/main.py`. The runner is composed from small, deep
modules behind stable interfaces:

- **Runtime** — one interface over Claude Code and Codex.
- **Issue source** — pluggable; v0.1 ships GitHub Issues via `gh`.
- **Phase graph** — an executable Builder-style graph in `.pycastle/main.py`.
- **Orchestrator** — selects, claims, branches, runs the graph, and opens a PR.
