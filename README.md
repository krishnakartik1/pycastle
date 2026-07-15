# PyCastle

A reusable, installable autonomous development loop for any repository.

PyCastle owns the runner while each project owns its prompts, gate, and phase
graph. It selects ready GitHub issues, gives each issue an isolated worktree,
runs a plan → implement → review phase graph, retries failed implementation
attempts with a handoff, and opens one pull request for the successful batch.
Claude Code and Codex are supported as runtimes, on the host or in Docker.

## Requirements

- Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/)
- `git` and the authenticated [`gh`](https://cli.github.com/) CLI
- Docker when using the Docker sandbox
- A Claude Code or Codex subscription login for the corresponding runtime

## Install

Until PyCastle is published, install the current repository directly from GitHub:

```bash
uv pip install git+https://github.com/krishnakartik1/pycastle
```

Run that inside an active virtual environment, or pass `--system` to `uv pip`
when intentionally installing into the system environment. Once the package is
published (#22/#23/#24), the equivalent command will be:

```bash
uv pip install pycastle
```

For development on PyCastle itself, clone the repository and run
`uv pip install -e ".[dev]"`.

## Quick start

From the repository you want PyCastle to work on, scaffold its project fixture:

```bash
pycastle init
```

`init` asks whether phases should run on the host or in Docker, records that
default, and creates `.pycastle/` without overwriting an existing fixture. The
fixture contains all of the project-owned behavior:

- `.pycastle/main.py` — the executable phase graph and its transitions.
- `.pycastle/prompts/` — instructions for the plan, implement, and review phases.
- `.pycastle/gate` — the quality gate run after an implementation attempt.
- `.pycastle/Dockerfile` — the agent image recipe, including the project gate
  toolchain for detected Python projects.
- `.pycastle/sandbox` — the default `host` or `docker` sandbox choice.
- `.pycastle/version` — the installed PyCastle release that initialized or last
  migrated the fixture; `run` checks it before starting any Run side effects.
- `.pycastle/.gitignore` — Run artifacts and runtime scratch files that should
  stay out of version control.

Review and commit this directory. In particular, make `.pycastle/gate` express
what “passing” means for the project and extend `.pycastle/Dockerfile` with any
toolchain the phases and gate need. The Docker sandbox runs both the runtime and
the gate in that image; PyCastle builds it on demand and reuses it while the
Dockerfile is unchanged.

For a Docker run, authenticate the runtime once. The login is stored in a named
Docker volume and reused across projects:

```bash
pycastle sandbox setup --runtime claude
```

Use `--runtime codex` instead for Codex. Host runs do not need `sandbox setup`,
but the selected runtime CLI must already be installed, authenticated, and
available on `PATH`.

PyCastle reads GitHub Issues carrying the `ready-for-agent` label. By default it
only selects issues assigned to your authenticated `gh` user, so prepare an
issue with:

```bash
gh issue edit 123 --add-assignee @me --add-label ready-for-agent
```

Then run it from the repository root:

```bash
pycastle run --runtime claude
```

The sandbox recorded by `init` is used automatically. Override it for one run
with `--sandbox host` or `--sandbox docker`; choose Codex with `--runtime codex`.
Successful issues are folded into a per-run branch and PyCastle opens a pull
request back to the branch from which the run started. After run PRs are merged
or closed, run `pycastle prune` to remove their remote `pycastle/run-*` branches.
The command discovers all open PR heads before deleting anything and always
keeps their branches intact.

## Commands

- `pycastle --version` — print the normalized installed PyCastle release.
- `pycastle init` — scaffold the `.pycastle/` project fixture described above.
- `pycastle run -i N --runtime claude` — work up to `N` ready issues; the
  default is one. Use `--include-unassigned` to include unassigned issues.
- `pycastle run --sandbox docker --runtime codex` — override the recorded
  sandbox and runtime for this run.
- `pycastle run --verbose` — stream reasoning/output and persist per-issue
  transcripts and telemetry under `.pycastle/runs/`.
- `pycastle prune` — delete remote `pycastle/run-*` branches whose PRs are
  merged or closed, while preserving every branch attached to an open PR.
- `pycastle sandbox setup --runtime claude` — authenticate a runtime in its
  shared Docker auth volume.
- `pycastle sandbox build` — explicitly build the content-addressed image from
  `.pycastle/Dockerfile`; normal Docker runs build it on demand.

## Customize the project fixture

The fixture is regular project code, not hidden PyCastle configuration. Edit
`.pycastle/prompts/` to change phase instructions, `.pycastle/gate` to run the
project's real checks, and `.pycastle/main.py` to add phases or redirect their
success and failure transitions. Every phase and the gate runs in the selected
sandbox; orchestration such as `git`, `gh`, worktree management, and image
building stays on the host.

When Docker is selected, `.pycastle/Dockerfile` is the source of truth for the
agent image. An unchanged recipe reuses its content-addressed image; editing the
recipe causes a new image to be built. `--image IMAGE` is the bring-your-own-image
escape hatch and bypasses the Dockerfile, but that image must provide the
runtime CLI, project gate toolchain, `git`, and the expected non-root `node` user
with home `/home/node`.

## Docker authentication notes

Claude credentials live in `pycastle-claude-auth`; Codex credentials live in
`pycastle-codex-auth`. Credential contents are never read, printed, or copied by
PyCastle. Claude's browser login needs a TTY. On a headless host, authenticate
on a machine with a browser and transfer the credentials into the same named
volume on the target host.

Codex's HTTP client requires IPv6-capable Docker. If authentication fails while
reaching `auth.openai.com`, enable IPv6 in `/etc/docker/daemon.json`:

```json
{ "ipv6": true, "ip6tables": true, "fixed-cidr-v6": "fd00:dead:beef::/48" }
```

Restart Docker afterward (`sudo systemctl restart docker`; this stops running
containers). Docker older than 27 also requires `"experimental": true`. Enabling
IPv6 can make published ports bind on IPv6 too, so ensure the host firewall
covers IPv6.
