---
name: pycastle
description: Onboard a project, check readiness, operate a PyCastle Run, and coordinate pull request follow-up.
---

# PyCastle lifecycle

PyCastle release: `0.1.4`

Use this vendor-neutral workflow to operate PyCastle for the current repository.
Treat `v0.1.4` as the only compatible runner and skill Git tag. Earlier releases
predate the complete project-owned Item selection contract.

## Select the Runtime

Identify the invoking host before running any PyCastle lifecycle command:

- Codex host -> `codex` Runtime.
- Claude Code host -> `claude` Runtime.
- For an unknown or ambiguous host, ask the user to choose `codex` or `claude`.
  Do not guess, initialize, run Doctor, or start a Run until they choose.

Call that choice `<runtime>` in the commands below.

## Enforce release compatibility

Before `init`, Doctor, or Run, execute `pycastle --version`. Parse the command's
documented `pycastle <version>` output and require the normalized version to equal
`0.1.4` exactly. A malformed output, prerelease, local version, or any other value
is a version mismatch.

On a version mismatch, stop. Reinstall both the runner and this canonical skill
from `v0.1.4`; do not continue with a merely compatible-looking version:

```bash
uv tool install --force git+https://github.com/krishnakartik1/pycastle@v0.1.4
git clone --depth 1 --branch v0.1.4 https://github.com/krishnakartik1/pycastle /tmp/pycastle-v0.1.4
```

Then install or link `/tmp/pycastle-v0.1.4/skills/pycastle/` into the invoking
host's skill discovery directory and restart/reload that host. Re-run
`pycastle --version`; do not proceed until it exactly matches this embedded release.

## Onboard the Project

If no Project fixture exists, run:

```bash
pycastle init --sandbox docker
```

Never overwrite an existing Project fixture by initializing it again. Explain and
review all project-owned customization points directly:

- `.pycastle/main.py` declares the Run definition: its required Item Execution
  graph and optional Before-Run and After-Run Execution graphs. Graphs contain
  only Runtime nodes and Gate nodes.
- `.pycastle/prompts/` contains instructions for Runtime nodes.
- `.pycastle/setup` is the mandatory idempotent Setup executable used at Item
  and Run scope before every node visit.
- `.pycastle/gate` defines project success and runs at Item and integrated Run
  scope.
- `.pycastle/Dockerfile` is the Agent image recipe and must install the selected
  Runtime plus every tool needed by Setup, Runtime nodes, and the Gate. PyCastle
  builds this canonical recipe and pins its immutable identity for each Run;
  Docker owns layer caching.
- `.pycastle/sandbox` records the required Sandbox choice. This workflow uses Docker.
- `.pycastle/.gitignore` keeps scratch communication and local Run records out of
  version control; prompts may use those ignored files to pass bounded context.

Have the user review and commit the complete Project fixture, including executable
bits for Setup and the Gate, before continuing. Do not interpret the fixture's
release marker. Do not inspect `.pycastle/version`; fixture compatibility is
runner-owned and is reported by Doctor.

Onboard Docker authentication for the selected Runtime once with:

```bash
pycastle runtime login --sandbox docker --runtime <runtime>
```

The selected Runtime must be present in the Project's Agent image. Its named auth
volume is shared across projects, while credential contents remain private.

## Consume Doctor readiness

Run the complete readiness snapshot for at most five Items:

```bash
pycastle doctor --json --sandbox docker --runtime <runtime> --iterations 5
```

Parse stdout as one complete schema-v1 JSON document. Stop if the command exits
nonzero, JSON is malformed, the schema is unsupported, the report is not ready,
or any check is failed or blocked. Report and follow only Doctor-provided facts
and remediation. Do not reproduce readiness probes, infer fixture compatibility,
or substitute local inspection for Doctor's result. Doctor may build and pin the
canonical Agent image, but it is only a current snapshot: Run re-evaluates readiness
before side effects and freezes its own Run-readiness inputs.

When Doctor reports fixture migration required, treat remediation as
runner-owned. From a clean checkout, run `pycastle upgrade`. For the 0.1.2
manual boundary it makes no writes and instructs the owner to add the reserved
`PYCASTLE_HOST_UID` and `PYCASTLE_HOST_GID` declarations and reconcile the
image-declared non-root user. The owner edits, reviews, and commits the
project-owned Dockerfile. Run `pycastle upgrade` again from that clean checkout,
then review and commit its marker change. For the 0.1.4 manual boundary it also
makes no writes until the owner adds and reviews a project-owned selection prompt
and wraps the Item execution graph in an Item definition using `build_item` and
`runtime_selection`. Run `pycastle upgrade` again after committing those owner
changes, then review and commit its marker change and rerun Doctor. Do not inspect
or interpret the release marker, reinitialize the fixture, or rewrite project-owned
files automatically. When one upgrade spans both manual boundaries, PyCastle
keeps the operation transactional and advances the marker only after both
owner-authored contracts are present.

Use only the already-eligible `ready-for-agent` Items in Doctor's resolved batch.
Zero eligible Items is a successful no-op. If fewer than five are eligible, accept
the smaller bounded batch. Do not triage, rewrite, promote, or relabel untriaged Items
unless the user explicitly requested a separate triage workflow. Never manufacture
eligibility to fill the batch.

## Start or monitor one Run

Before starting, inspect the host's current tasks/subagents and known Run context
for a live PyCastle Run in this repository. A recovery branch or historical pull
request alone is not evidence of a live process. If an active Run exists, stop
dispatch and reattach to or monitor it. Do not duplicate an active Run.

Otherwise start exactly one command:

```bash
pycastle run --sandbox docker --runtime <runtime> --iterations 5
```

This command remains bounded when Run's fresh readiness evaluation finds fewer
Items than Doctor did. For this long-running command, use one subagent for the
command when the host supports subagents and keep the parent monitoring it. If subagents are not
supported or cannot be started, run it in the foreground. Never launch a second
Run while the first might still be alive.

## Delegate outcomes and protect merge

Report the runner's final outcome. For an unexpected failure, delegate to the
host's diagnosis capability and give it Doctor diagnostics plus runner output as
evidence. For a draft or ready pull request, delegate review and remediation to
the host's pull-request capability. Rely on PyCastle's draft/ready state, integrated
Run Gate, and published Run report; do not manually recreate review, repair,
publication, or artifact handling.

Do not copy diagnosis or pull-request review/remediation policy into this skill.
A ready and reviewed pull request is not merge permission. Identify the specific
pull request and ask for explicit user authorization at the merge boundary. Do not merge without explicit user authorization
for that pull request; otherwise stop
after presenting its state and evidence.
