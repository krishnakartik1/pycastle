# PyCastle

A reusable, installable autonomous development loop for any repository.

PyCastle owns the runner while each project owns its prompts, gate, and phase
graph. It selects ready GitHub issues, gives each issue an isolated worktree,
runs a plan → implement → review Item phase graph, retries failed implementation
attempts with a handoff, reviews the integrated Run, applies its final Gate, and
publishes verified Run evidence to one pull request for the successful batch.
Claude Code and Codex are supported as runtimes, on the host or in Docker.

## Requirements

- Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/)
- `git` and the authenticated [`gh`](https://cli.github.com/) CLI
- Docker when using the Docker sandbox
- A Claude Code or Codex subscription login for the corresponding runtime

## Install

Until PyCastle is published, install release `v0.1.0` directly from GitHub:

```bash
uv pip install git+https://github.com/krishnakartik1/pycastle@v0.1.0
```

Run that inside an active virtual environment, or pass `--system` to `uv pip`
when intentionally installing into the system environment. Once the package is
published (#22/#23/#24), the equivalent command will be:

```bash
uv pip install pycastle
```

For development on PyCastle itself, clone the repository and run
`uv pip install -e ".[dev]"`.

### Install the lifecycle skill

The repository ships one vendor-neutral lifecycle skill for both Codex and
Claude Code. Obtain its canonical source from the same Git tag as the installed
runner:

```bash
git clone --depth 1 --branch v0.1.0 https://github.com/krishnakartik1/pycastle ~/.local/share/pycastle-v0.1.0
```

Link that one `skills/pycastle/` directory into the discovery location for the
host you use:

```bash
# Codex
mkdir -p ~/.codex/skills
ln -s ~/.local/share/pycastle-v0.1.0/skills/pycastle ~/.codex/skills/pycastle

# Claude Code
mkdir -p ~/.claude/skills
ln -s ~/.local/share/pycastle-v0.1.0/skills/pycastle ~/.claude/skills/pycastle
```

The wheel/tool and skill must always come from the same Git tag. The skill embeds
that release and requires it to exactly match `pycastle --version` before it
initializes a Project, checks readiness, or starts a Run. Restart or reload the
host after installing the skill.

## Quick start

From the repository you want PyCastle to work on, scaffold its project fixture:

```bash
pycastle init
```

`init` asks whether phases should run on the host or in Docker, records that
default, and creates `.pycastle/` without overwriting an existing fixture. The
choice can be scripted with `pycastle init --sandbox host` or
`pycastle init --sandbox docker`, which skips the prompt. If standard input is
unavailable and the prompt reaches end-of-file, `init` uses the host default.
The fixture contains all of the project-owned behavior:

- `.pycastle/main.py` — the Run definition containing its Item graph and optional
  before-Run and after-Run graphs.
- `.pycastle/prompts/` — instructions for Item and integrated Run phases.
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
request back to the branch from which the run started. After each successful
item fold, PyCastle pushes the current `pycastle/run-<run-id>` branch to
`origin`. A failed durability push is logged without stopping the Run; the next
completed item and finalization retry the latest Run state. The final push must
succeed before PyCastle creates a pull request.

If a Run is interrupted, list its durable recovery branches with:

```bash
git ls-remote --heads origin 'refs/heads/pycastle/run-*'
```

Recover a branch locally by fetching and checking it out (replace the example
Run ID with the remote branch you found):

```bash
git fetch origin pycastle/run-20260715-120000
git switch --create recover-run-20260715-120000 FETCH_HEAD
```

This recovers every item whose merge was successfully pushed; incomplete item
work is intentionally not pushed. An interrupted Run branch with no pull
request remains on `origin` as the recovery artifact. `pycastle prune` preserves
these no-PR branches and every branch attached to an open PR, deleting only Run
branches whose PR is closed or merged. Once no-PR recovery branches are no
longer needed, remove them explicitly with `pycastle prune --include-no-pr`.
Discovery failures are fail-safe: prune deletes nothing unless it can classify
the remote branches from pull-request history.

## Commands

- `pycastle --version` — print the normalized installed PyCastle release.
- `pycastle init [--sandbox {host,docker}]` — scaffold the `.pycastle/` Project
  fixture described above, optionally without prompting.
- `pycastle upgrade` — transactionally apply bundled forward migrations to an
  initialized Project fixture, leaving the result as an unstaged diff to review.
- `pycastle run -i N --runtime claude` — work up to `N` ready issues; the
  default is one. Use `--include-unassigned` to include unassigned issues.
- `pycastle run --sandbox docker --runtime codex` — override the recorded
  sandbox and runtime for this run.
- `pycastle run --verbose` — stream reasoning/output and persist per-issue
  transcripts and telemetry under `.pycastle/runs/`.
- `pycastle prune [--include-no-pr]` — delete remote `pycastle/run-*` branches
  whose PRs are merged or closed. By default no-PR recovery branches are kept;
  opt in to deleting them with `--include-no-pr`. Open-PR branches are always
  preserved.
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

## Upgrade a Project fixture

Choose a PyCastle release tag, reinstall that exact runner, and then explicitly
migrate each initialized repository. For example, for `v0.1.0`:

```bash
uv tool install --force git+https://github.com/krishnakartik1/pycastle@v0.1.0
cd /path/to/initialized/repository
pycastle upgrade
```

The equivalent `pipx` workflow is:

```bash
pipx install --force git+https://github.com/krishnakartik1/pycastle@v0.1.0
cd /path/to/initialized/repository
pycastle upgrade
```

Run `pycastle upgrade` once in every initialized repository. It applies only
bundled runner/fixture contract migrations; it does not synchronize newer Project
fixture defaults or overwrite project-owned improvements. The command refuses a
dirty worktree and leaves a successful migration as an unstaged diff for you to
inspect. PyCastle performs no self-update or update discovery, and it does not
create a commit, branch, pull request, or merge authorization.

When Docker is selected, `.pycastle/Dockerfile` is the source of truth for the
agent image. An unchanged recipe reuses its content-addressed image; editing the
recipe causes a new image to be built. `--image IMAGE` is the bring-your-own-image
escape hatch and bypasses the Dockerfile, but that image must provide the
runtime CLI, project gate toolchain, `git`, and the expected non-root `node` user
with home `/home/node`.

## Troubleshooting Codex host Black checks

The Codex Runtime with the host Sandbox runs under Codex's native
`workspace-write` sandbox. In that specific combination, a whole-tree Black
self-check such as `black --check .` can print its successful summary and then
hang because the sandbox blocks a multiprocessing worker's local socket. This
is an advisory command started by the Runtime during a phase, not a stalled or
failed PyCastle Gate.

Prefer the Docker Sandbox for Codex (`pycastle run --runtime codex --sandbox
docker`), where the agent image is the isolation boundary and the check exits
normally. If a host Run needs an advisory Black self-check, invoke one file per
Black process, for example:

```bash
git ls-files -z '*.py' | xargs -0 -r -n 1 black --check --
```

Passing `--workers 1` does not avoid the hang. PyCastle launches the
project-owned Gate outside the Codex CLI's nested native sandbox, so the Gate is
unaffected by this limitation. The Gate remains authoritative even if a
Runtime's advisory self-check times out.

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
