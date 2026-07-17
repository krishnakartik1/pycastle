# PyCastle

A reusable, installable autonomous development loop for any repository.

PyCastle owns the runner while each project owns its prompts, Setup, Gate, and
Execution graphs. It selects ready GitHub issues, gives each Item an isolated
worktree, walks the project-owned Item and Run-scope graphs, and publishes the
integrated Run in one pull request. Verification and recovery are visible Gate
and Runtime nodes in those graphs.
Claude Code and Codex are supported as runtimes, on the host or in Docker.

## Requirements

- Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/)
- `git` and the authenticated [`gh`](https://cli.github.com/) CLI
- Docker when using the Docker sandbox
- A Claude Code or Codex subscription login for the corresponding runtime

## Install

Release `v0.1.3` contains the current lifecycle skill. Once that
tag is published, install it directly from GitHub:

```bash
uv pip install git+https://github.com/krishnakartik1/pycastle@v0.1.3
```

Release `v0.1.3` supersedes `v0.1.2`: the earlier runner cannot complete
Upgrade for a standard Project fixture containing Gate nodes. The correction is
a runner patch and does not introduce a new Project fixture migration.

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
git clone --depth 1 --branch v0.1.3 https://github.com/krishnakartik1/pycastle ~/.local/share/pycastle-v0.1.3
```

Link that one `skills/pycastle/` directory into the discovery location for the
host you use:

```bash
# Codex
mkdir -p ~/.codex/skills
ln -s ~/.local/share/pycastle-v0.1.3/skills/pycastle ~/.codex/skills/pycastle

# Claude Code
mkdir -p ~/.claude/skills
ln -s ~/.local/share/pycastle-v0.1.3/skills/pycastle ~/.claude/skills/pycastle
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

`init` asks whether project execution should use the host or Docker Sandbox,
records that selection, and creates `.pycastle/` without overwriting an existing
fixture. The choice can be scripted with `pycastle init --sandbox host` or
`pycastle init --sandbox docker`, which skips the prompt. Non-interactive use
without one of those explicit choices fails rather than guessing. Repository
manifests and contents do not affect the generated fixture. The fixture contains
all of the project-owned behavior:

- `.pycastle/main.py` — the Run definition containing its Item graph and optional
  before-Run and after-Run graphs.
- `.pycastle/prompts/` — instructions for Runtime nodes at Item and Run scope.
- `.pycastle/setup` — repeatable preparation whose durable effects establish
  prerequisites for the next Runtime or Gate node; it starts as a no-op.
- `.pycastle/gate` — the fail-closed project verification policy invoked by
  every Gate node; it must be configured before verification can pass.
- `.pycastle/Dockerfile` — the language-neutral Agent image recipe with shipped
  Runtime CLIs, the reserved host UID/GID interface, and a visible extension
  point for the project toolchain.
- `.pycastle/sandbox` — the selected `host` or `docker` Sandbox.
- `.pycastle/version` — the installed PyCastle release that initialized or last
  migrated the fixture; `run` checks it before starting any Run side effects.
- `.pycastle/.gitignore` — Run artifacts and runtime scratch files that should
  stay out of version control.

Review and commit the complete directory. Configure `.pycastle/setup` when the
project needs preparation, make `.pycastle/gate` express the complete passing
policy, and extend `.pycastle/Dockerfile` with any tools Setup, Runtime nodes, or
the Gate need. The Docker Sandbox runs all three in that image. Readiness builds
it from the repository root, pins the resulting immutable identity, and reuses
that identity for the whole Run.

For a Docker run, authenticate the runtime once. The login is stored in a named
Docker volume and reused across projects:

```bash
pycastle runtime login --runtime claude --sandbox docker
```

Use `--runtime codex` instead for Codex. Host authentication uses
`pycastle runtime login --runtime claude --sandbox host`; the selected Runtime
CLI must already be installed and available on `PATH`.

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
- `pycastle runtime login --runtime claude [--sandbox host|docker]` — explicitly
  authenticate a Runtime. Without the flag, use the `.pycastle/sandbox` marker.
- Docker Doctor and Run build the canonical `.pycastle/Dockerfile`; there is no
  image override or separate Sandbox build lifecycle.

## Customize the project fixture

The fixture is regular project code, not hidden PyCastle configuration. Edit
`.pycastle/prompts/` to change Runtime-node instructions, `.pycastle/setup` to
prepare durable prerequisites, `.pycastle/gate` to define verification, and
`.pycastle/main.py` to add nodes or redirect their success and failure edges.
Setup, Runtime nodes, and Gate nodes run in the selected Sandbox; orchestration
such as `git`, `gh`, worktree management, and image building stays on the host.

## Upgrade a Project fixture

Choose a PyCastle release tag, reinstall that exact runner, and then explicitly
migrate each initialized repository. For example, for `v0.1.3`:

```bash
uv tool install --force git+https://github.com/krishnakartik1/pycastle@v0.1.3
cd /path/to/initialized/repository
pycastle upgrade
```

The equivalent `pipx` workflow is:

```bash
pipx install --force git+https://github.com/krishnakartik1/pycastle@v0.1.3
cd /path/to/initialized/repository
pycastle upgrade
```

Run `pycastle upgrade` once in every initialized repository. It applies only
bundled runner/fixture contract migrations; it does not synchronize newer Project
fixture defaults or overwrite project-owned improvements. The command refuses a
dirty worktree and leaves a successful migration as an unstaged diff for you to
inspect. PyCastle performs no self-update or update discovery, and it does not
create a commit, branch, pull request, or merge authorization.

The 0.1.2 Docker identity migration is deliberately owner-authored and uses two
Upgrade passes:

1. Doctor reports that fixture migration is required.
2. Run `pycastle upgrade` from a clean checkout. It reports the required
   `PYCASTLE_HOST_UID` and `PYCASTLE_HOST_GID` Dockerfile interface and writes
   nothing.
3. Edit, review, and commit `.pycastle/Dockerfile` so its declared non-root user
   consumes both arguments and has their numeric UID/GID.
4. Run `pycastle upgrade` again from the clean checkout. It validates the
   declarations and advances the fixture marker.
5. Review and commit the marker change, then rerun Doctor.

When Docker is selected, `.pycastle/Dockerfile` is the source of truth for the
Agent image. Doctor and Run build it with the clean repository root as context
and the host process's effective UID/GID as non-secret reserved build arguments,
then pin the resulting immutable image identity for that one readiness snapshot.
PyCastle probes only its language-neutral launch, workspace, authentication, and
Runtime boundary; project interpreters and toolchains remain behind Setup.

`pycastle doctor` reports `ready`, `no_work`, or `not_ready`. It never executes
Setup, Gate, an Execution graph, or a Runtime prompt. A later Run always takes a
fresh snapshot. Select a Sandbox explicitly with `--sandbox`, or record exactly
`host` or `docker` in `.pycastle/sandbox`; there is no implicit host default.

## Troubleshooting Codex host Black checks

The Codex Runtime with the host Sandbox runs under Codex's native
`workspace-write` sandbox. In that specific combination, a whole-tree Black
self-check such as `black --check .` can print its successful summary and then
hang because the sandbox blocks a multiprocessing worker's local socket. This
is an advisory command started by the Runtime during a node, not a stalled or
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
