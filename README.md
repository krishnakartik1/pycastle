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

**Headless fallback.** `sandbox setup` runs the interactive browser login,
which needs a TTY. On a headless host (CI, a server with no browser), run the
login on a machine that has a browser, then move the resulting credentials into
the same named volume the host uses.

**The Codex runtime needs IPv6-capable Docker.** Codex's HTTP client is
IPv6-first and does not fall back to IPv4, but Docker's default bridge network
is IPv4-only. So on an otherwise IPv6-capable host, `pycastle sandbox setup
--runtime codex` (and any `--runtime codex --sandbox docker` run) fails to reach
`auth.openai.com` with `error logging in ... error sending request for url`,
while Claude is unaffected (its client falls back to IPv4). Enable IPv6 for
Docker once, in `/etc/docker/daemon.json`:

```json
{ "ipv6": true, "ip6tables": true, "fixed-cidr-v6": "fd00:dead:beef::/48" }
```

then restart the daemon: `sudo systemctl restart docker` (this stops running
containers, so do it between runs). On Docker older than 27, also add
`"experimental": true`. This gives containers NAT'd IPv6 egress. Note the
trade-off: published ports (`-p`) then bind IPv6 as well as IPv4, so make sure
any host firewalling covers IPv6.

## How it fits together

PyCastle owns the reusable runner; a project owns its prompts, gates, and the
workflow graph in `.pycastle/main.py`. The runner is composed from small, deep
modules behind stable interfaces:

- **Runtime** — one interface over Claude Code and Codex.
- **Issue source** — pluggable; v0.1 ships GitHub Issues via `gh`.
- **Phase graph** — an executable Builder-style graph in `.pycastle/main.py`.
- **Orchestrator** — selects, claims, branches, runs the graph, and opens a PR.
