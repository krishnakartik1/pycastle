"""Scaffold the Project fixture into a repo for ``pycastle init``.

This is the deep module behind ``pycastle init`` (#11): it takes the host-first
vs Docker-first choice and writes the Project fixture — a Builder-style
``main.py``, a ``Dockerfile`` for the agent image, the default Item and after-Run
prompts, default ``setup`` and ``gate`` executables, a ``sandbox`` marker, and a
release ``version`` marker, plus a ``.gitignore`` that excludes Run records and
scratch files. The output is a file tree, which is what the tests assert.

There is no interactive I/O here: the CLI does the prompting and passes the
choice in, so this stays a pure-ish function that is trivial to unit-test
against a ``tmp_path``. The scaffolded fixture is the same proven content the
repo runs itself, so a fresh repo gets exactly the loop that works here and runs
end to end before any customization.

The host-first vs Docker-first choice produces one observable difference in the
tree: the ``sandbox`` marker file reads ``host`` or ``docker``. Every other file
is byte-identical between the two choices. ``pycastle run`` could read that
marker to pick a default sandbox; today it is the tested, recorded distinction
between the two init choices.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from . import __version__
from .compatibility import VERSION_MARKER

logger = logging.getLogger("pycastle")

#: The directory name the fixture is written into, relative to the target repo.
FIXTURE_DIRNAME = ".pycastle"

#: The fixture file recording the host-first/Docker-first choice, relative to
#: the fixture dir. ``pycastle init`` writes it; ``pycastle run`` reads it.
SANDBOX_MARKER = "sandbox"

#: The two execution choices ``pycastle init`` offers.
SandboxChoice = Literal["host", "docker"]


def read_sandbox(fixture_dir: Path) -> str | None:
    """Return the sandbox choice recorded in ``fixture_dir``'s marker, or ``None``.

    Reads the ``sandbox`` marker ``pycastle init`` writes (see
    :data:`SANDBOX_MARKER`) and returns its stripped, lower-cased contents.
    Returns ``None`` when the marker is absent, empty, or unreadable, so a caller
    can fall back to a default without crashing on a missing or garbled file. The
    value is returned verbatim (after stripping) and is *not* validated against
    ``host``/``docker`` here -- the caller decides how to treat an unknown value.
    """
    marker = fixture_dir / SANDBOX_MARKER
    try:
        recorded = marker.read_text().strip().lower()
    except OSError:
        return None
    return recorded or None


class FixtureExistsError(Exception):
    """Raised when scaffolding would clobber an existing ``.pycastle/`` fixture.

    ``pycastle init`` refuses to overwrite a fixture a repo already has rather
    than silently replacing a project's prompts, gate, and graph shape.
    """


# --------------------------------------------------------------------------- #
# Template content. This mirrors the repo's own proven `.pycastle/` fixture so  #
# a scaffolded repo gets exactly what runs here. The scaffolded `main.py` uses  #
# the finalized declarative Builder API from #10 (see ADR-0004).               #
# --------------------------------------------------------------------------- #

_MAIN_PY = '''\
"""PyCastle workflow for this repository.

Hand-written for the conservative default flow: ``plan`` -> ``implement`` ->
``review`` -> done. The plan phase works out an approach, implement does the work
test-first (retrying with a handoff while the quality gates stay red), and
review tests edge cases and commits any improvements before the issue branch is
merged. Each phase names its own success and failure destinations as explicit
rows; the executor walks those transitions rather than running a fixed list (see
ADR-0004).

The failure edges all route to ``HUMAN``: implement's bounded retry is kept
internal to the implement phase, so a phase that genuinely cannot pass hands the
issue to a person rather than looping. Edit this file with normal Python to
change the workflow -- add phases, repoint edges, or model handoff as its own
node.
"""

from pycastle.graph import DONE, HUMAN, build, build_run, phase

run = build_run(
    before=None,
    item=build(
        start="plan",
        phases=[
            phase("plan", "plan.md", on_success="implement", on_failure=HUMAN),
            phase("implement", "implement.md", on_success="review", on_failure=HUMAN),
            phase("review", "review.md", on_success=DONE, on_failure=HUMAN),
        ],
    ),
    after=build(
        start="run-review",
        phases=[
            phase("run-review", "run-review.md", on_success="run-repair"),
            phase("run-repair", "run-repair.md", on_success="run-report"),
            phase("run-report", "run-report.md"),
        ],
    ),
)
'''

_PLAN_MD = """\
# Plan

You are planning how to work a single GitHub issue. Do not write the
implementation yet -- work out the approach so the implement phase can move fast.

1. Read the issue's "What to build" and "Acceptance criteria". If it references
   a parent PRD, read that too.
2. Read the existing code the change touches so you understand the current
   state before proposing anything new. Note the files and public APIs in play.
3. Read `CONTEXT.md` for the project's domain language and use those exact
   terms -- never invent synonyms. Read any relevant `docs/adr/` decisions.
4. Sketch the smallest change that satisfies every acceptance criterion: which
   files to add or edit, the test-first order to build them in, and the edge
   cases the review phase should later probe.
5. Call out anything that looks out of scope for this one issue, and stop there.

Write the plan to `.pycastle/plan.md` -- an ignored scratch path the implement
phase can pick up but that is never committed. Stay within the scope of this one
issue. Do not modify unrelated code, and do not commit in this phase.
"""

_IMPLEMENT_MD = """\
# Implement

You are working a single GitHub issue to completion.

1. Read `.pycastle/plan.md`, then re-read the issue's "What to build" and
   "Acceptance criteria".
2. Implement the change test-first: write failing tests for the criteria, then
   the code to make them pass.
3. Run the project's quality gates and fix anything they flag.
4. Commit your work with a conventional commit message that references the
   issue number.

Stay within the scope of this one issue. Do not modify unrelated code.
"""

_REVIEW_MD = """\
# Review

You are reviewing and hardening the implementation of a single GitHub issue
before its branch is merged. This is a hardening pass, not an approve/reject
gate: fix what you find, do not hand work back.

1. Re-read the issue's "Acceptance criteria" and the diff produced so far.
2. Run the project's quality gates and fix anything they flag before going on.
3. Stress-test the edge cases the implement phase may have missed: empty inputs,
   zero, `None`, missing optional fields, boundary and off-by-one conditions,
   and invalid inputs that should raise specific errors. Write tests that probe
   these paths. If you can break the implementation, fix it.
4. Tidy code quality: unclear names, needless nesting, missing type hints or
   docstrings, redundant code, and any domain terms that drift from
   `CONTEXT.md`.
5. Run the quality gates once more and confirm they pass.
6. Commit your improvements with a conventional commit message that references
   the issue number. If you made no changes, there is nothing to commit.

Commit any review improvements in this phase so they are part of the issue
branch before it is merged. Stay within the scope of this one issue.
"""

_RUN_REVIEW_MD = """\
# Integrated Run Review

Review the integrated Run branch and its complete diff. Run focused checks and
write actionable findings to `.pycastle/run-review.md`. Do not mutate GitHub.
Write an explicit "No findings" when no repair is needed.
"""

_RUN_REPAIR_MD = """\
# Integrated Run Repair

Read `.pycastle/run-review.md`. Repair every actionable finding and add tests
for integration defects. When there are no findings, make no changes. Do not
mutate GitHub.
"""

_RUN_REPORT_MD = """\
# Run Report

Inspect the repaired integrated diff and the checks you can verify. Write a
concise Markdown report to `.pycastle/run-report.md`, curating relevant test,
lint, and coverage evidence. Do not mutate GitHub and do not include secrets or
raw unbounded logs.
"""

# A default, runnable gate so the retry-with-handoff path is reachable from the
# first run. It is project-owned: the project edits it to match its own stack.
# The commands below are a sensible documented default a Python project keeps,
# guarded so an absent tool does not hard-fail a brand-new repo.
_GATE = """\
#!/usr/bin/env bash
# PyCastle project quality gate.
#
# PyCastle runs this script inside each issue's worktree after the implement
# phase. Exit 0 means the gates passed; any non-zero exit means they failed and
# the attempt is retried with a handoff document. This script is project-owned:
# edit it to define what "passing" means for your repo (linters, formatters,
# type checkers, your test suite -- whatever a green build requires here).
#
# The default below runs ruff/black/pytest. Each step runs only when its tool is
# installed; a partially-equipped image (say ruff present, pytest absent) still
# PASSES on the checks it CAN run. But if ZERO tools are present the gate has
# verified nothing, so it FAILS LOUD rather than reporting a vacuous green --
# add the gate toolchain to the project Dockerfile (.pycastle/Dockerfile) so the
# image the gate runs in actually carries ruff/black/pytest. Replace these with
# your real gate commands.
set -euo pipefail

ran=0
missing=()

run_if_available() {
  if command -v "$1" >/dev/null 2>&1; then
    ran=$((ran + 1))
    "$@"
  else
    missing+=("$1")
    echo "skipping gate step (not installed): $1"
  fi
}

if [ "${1:-}" = "--check-tools" ]; then
  for tool in ruff black pytest; do
    if command -v "$tool" >/dev/null 2>&1; then
      ran=$((ran + 1))
    else
      missing+=("$tool")
    fi
  done
  if [ "$ran" -eq 0 ]; then
    echo "Missing gate tools: ${missing[*]}" >&2
    exit 1
  fi
  exit 0
fi

fixture_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$fixture_dir/setup"

run_if_available ruff check . --exit-non-zero-on-fix
run_if_available black --check .
run_if_available pytest -q

if [ "$ran" -eq 0 ]; then
  echo "ERROR: no gate tools were available, so this gate verified nothing." >&2
  echo "Missing: ${missing[*]}" >&2
  echo "No gate tools found on PATH. Install ruff/black/pytest where the gate runs:" >&2
  echo "  - host sandbox: install them into your environment / activate your project venv" >&2
  echo "  - docker sandbox: add them to .pycastle/Dockerfile (the PROJECT EXTENSION POINT)" >&2
  exit 1
fi
"""

_SETUP_NOOP = """\
#!/usr/bin/env bash
# PyCastle project dependency setup.
# No supported dependency manifest was found when `pycastle init` ran.
# Replace this no-op with the command that prepares your project for its phases
# and gate. PyCastle executes this project-owned hook at the start of each issue.
:
"""


def _setup_script(target_dir: Path) -> str:
    """Choose the initial project-owned setup hook from present manifests."""
    if (target_dir / "uv.lock").exists():
        command = "export UV_PROJECT_ENVIRONMENT=.pycastle/venv\nuv sync --all-extras"
    elif (target_dir / "poetry.lock").exists():
        # Poetry otherwise creates its environment under the short-lived
        # container's cache. Reuse the activated, bind-mounted environment so
        # phases and the gate can import the installed dependencies later.
        command = "POETRY_VIRTUALENVS_CREATE=false poetry install"
    elif (target_dir / "pyproject.toml").exists():
        command = 'pip install -e ".[dev]" || pip install -e .'
    elif (target_dir / "requirements.txt").exists():
        command = "pip install -r requirements.txt"
    else:
        return _SETUP_NOOP
    return f"""#!/usr/bin/env bash
# PyCastle project dependency setup. This file is project-owned; edit it freely.
set -euo pipefail
python3 -m venv --system-site-packages .pycastle/venv
source .pycastle/venv/bin/activate
{command}
"""


# The agent image the project extends with its own language dependencies (#4).
# Based on node:22-slim so the bundled Claude/Codex CLIs are present; the project
# adds its toolchains at the marked extension point.
_DOCKERFILE = """\
# PyCastle agent image.
#
# This builds the `pycastle/agent:node22` image the Docker sandbox runs the
# agent inside. It is based on `node:22-slim` so the bundled Claude/Codex CLIs
# available, and it runs as the non-root `node` user the sandbox expects.
#
# Extend it with YOUR project's language dependencies at the marked point below,
# then build it once:
#
#     docker build -t pycastle/agent:node22 .pycastle
#
FROM node:22-slim

# Codex's Rust TLS stack verifies certificates against the system trust store,
# which node:22-slim ships empty. Install ca-certificates as root so codex can
# reach auth.openai.com; the Node-based Claude CLI bundles its own roots and is
# unaffected. git is installed here too: the implement/review prompts tell the
# agent to commit and to read "the diff produced so far", which need git
# regardless of language, so it belongs in the base image, not a stack-specific
# extension point.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*

# Install the agent CLIs your runtime needs. Pin versions to taste.
RUN npm install -g @anthropic-ai/claude-code @openai/codex && npm cache clean --force

# --- PROJECT EXTENSION POINT ------------------------------------------------
# Add your project's own language toolchains and system packages here, e.g.:
#
#   RUN apt-get update && apt-get install -y --no-install-recommends \\
#         python3 python3-pip \\
#     && rm -rf /var/lib/apt/lists/*
#
# Leave this block empty if a plain Node toolchain is all your repo needs.
# ---------------------------------------------------------------------------

# Pre-create the auth-volume mount dirs owned by `node`. A fresh Docker named
# volume mounted at a path absent from the image initializes root-owned, which
# would block the non-root `node` user from writing its login. Creating them
# node-owned here means a brand-new auth volume inherits node ownership.
RUN mkdir -p /home/node/.claude /home/node/.codex \\
    && chown -R node:node /home/node/.claude /home/node/.codex

# The agent runs as the non-root `node` user (the Claude CLI refuses root, and
# files it writes stay owned by a real user rather than root).
USER node
WORKDIR /home/node
"""

# The empty PROJECT EXTENSION POINT block exactly as it appears in `_DOCKERFILE`
# (non-Python: nothing to install). This is the verbatim replacement target the
# Python Dockerfile swaps out, so it must stay a byte-exact, unique substring of
# `_DOCKERFILE` -- a test pins that invariant so the two cannot drift.
_EXTENSION_EMPTY = """\
# --- PROJECT EXTENSION POINT ------------------------------------------------
# Add your project's own language toolchains and system packages here, e.g.:
#
#   RUN apt-get update && apt-get install -y --no-install-recommends \\
#         python3 python3-pip \\
#     && rm -rf /var/lib/apt/lists/*
#
# Leave this block empty if a plain Node toolchain is all your repo needs.
# ---------------------------------------------------------------------------"""

# The filled PROJECT EXTENSION POINT block for a Python project: install python3
# plus the gate toolchain (ruff/black/pytest) so the in-container gate (#28) can
# run. The Docker sandbox runs the quality gate INSIDE this image, and a
# toolchain-less image makes that gate fail loud, so these must be baked in.
# node:22-slim (Debian 12) marks the system interpreter externally-managed, so
# `--break-system-packages` is required to pip-install into it.
_EXTENSION_PYTHON = """\
# --- PROJECT EXTENSION POINT ------------------------------------------------
# This is a Python project (a pyproject.toml was present at scaffold time), so
# the image carries python3 plus the gate toolchain (ruff/black/pytest). The
# Docker sandbox runs the quality gate INSIDE this image, and a toolchain-less
# image makes that gate fail loud, so these must be baked in.
RUN apt-get update \\
    && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \\
    && rm -rf /var/lib/apt/lists/*

# node:22-slim (Debian 12) marks the system interpreter externally-managed, so
# --break-system-packages is required to pip-install into it.
RUN pip install --break-system-packages --no-cache-dir ruff black pytest uv poetry

# Dependency setup runs in a short-lived container before the phases. Keep its
# project environment in the bind-mounted worktree so later phase/gate
# containers see the installed dependencies too.
ENV PATH=".pycastle/venv/bin:${PATH}"

# Project runtime and test dependencies are installed by `.pycastle/setup` from
# the bind-mounted worktree at container start, keeping them aligned with the
# checked-out manifest without changing this image's content-addressed tag.
# NOTE: only the Python gate toolchain is handled automatically today; carrying
# the toolchain for non-Python stacks (Go/JS/Rust) is a follow-up.
# ---------------------------------------------------------------------------"""


def _dockerfile(*, python: bool) -> str:
    """Return the agent Dockerfile, gate-toolchain-filled for a Python project.

    For a non-Python project this is ``_DOCKERFILE`` unchanged (empty extension
    point). For a Python project the PROJECT EXTENSION POINT block is replaced
    with a real python3 + ruff/black/pytest install so the in-container gate
    (#28) can run. Both share the same base template, so they cannot drift.

    Args:
        python: Whether the scaffolded project is a Python project.

    Returns:
        The Dockerfile text for the agent image.
    """
    if not python:
        return _DOCKERFILE
    return _DOCKERFILE.replace(_EXTENSION_EMPTY, _EXTENSION_PYTHON)


# Excludes the transient run logs and generated run artifacts so they are never
# committed. Mirrors the paths the repo's own root .gitignore excludes. The
# Runtime scratch files (the plan a phase leaves, a retried attempt's handoff, any
# issue scratch) land directly in .pycastle/ during a run, so they are excluded
# too -- otherwise the orchestrator's `git add -A` folds them into the issue
# branch and the run's PR. They are anchored to this dir (leading `/`) so they
# never shadow the tracked prompts/plan.md.
_GITIGNORE = """\
# PyCastle run output (transient): run logs and generated run artifacts.
logs/
runs/
worktrees/

# PyCastle Runtime scratch files (transient): the plan a phase leaves for the next,
# a retried attempt's handoff, and any issue scratch. They land in .pycastle/
# during a run; excluding them keeps `git add -A` from committing them into an
# issue branch. Anchored here so they never shadow the tracked prompts/plan.md.
/handoff.md
/plan.md
/issue.md
/plan-issue-*.md
/run-review.md
/run-report.md
/venv/
"""


def _fixture_files(
    sandbox: SandboxChoice, *, python: bool, setup: str
) -> dict[str, str]:
    """Return the fixture's relative paths mapped to their text content.

    The mapping is identical for both choices except the ``sandbox`` marker,
    which records ``host`` or ``docker`` -- the one observable difference
    between a host-first and a Docker-first scaffold -- and the Dockerfile, whose
    PROJECT EXTENSION POINT carries the gate toolchain when ``python`` is set.

    Args:
        sandbox: The host-first/Docker-first choice recorded in the marker file.
        python: Whether to emit the Python Dockerfile (gate toolchain pre-filled).
    """
    return {
        "main.py": _MAIN_PY,
        "gate": _GATE,
        "setup": setup,
        SANDBOX_MARKER: f"{sandbox}\n",
        VERSION_MARKER: f"{__version__}\n",
        "Dockerfile": _dockerfile(python=python),
        ".gitignore": _GITIGNORE,
        "prompts/plan.md": _PLAN_MD,
        "prompts/implement.md": _IMPLEMENT_MD,
        "prompts/review.md": _REVIEW_MD,
        "prompts/run-review.md": _RUN_REVIEW_MD,
        "prompts/run-repair.md": _RUN_REPAIR_MD,
        "prompts/run-report.md": _RUN_REPORT_MD,
    }


def scaffold_fixture(target_dir: Path, *, sandbox: SandboxChoice) -> list[Path]:
    """Write the Project fixture into ``target_dir/.pycastle`` and list the files.

    ``sandbox`` is the host-first/Docker-first choice the CLI collected; it is
    recorded in the fixture's ``sandbox`` marker file and is the only difference
    between the two scaffolds. The scaffolded ``main.py`` uses the finalized
    declarative Builder API (``build(start=, phases=[phase(...)])`` with the
    ``DONE``/``HUMAN`` terminals) and encodes the conservative default flow
    ``plan`` -> ``implement`` -> ``review`` -> ``DONE``, which loads and walks
    end to end before any customization.

    Returns the written files (``setup`` and ``gate`` are left executable). Raises
    :class:`FixtureExistsError` if ``target_dir`` already has a ``.pycastle/``
    fixture (init never clobbers one), and :class:`ValueError` for a ``sandbox``
    value outside ``host``/``docker`` -- validated before anything is written.
    """
    if sandbox not in ("host", "docker"):
        raise ValueError(f"sandbox must be 'host' or 'docker', not {sandbox!r}")

    fixture_dir = target_dir / FIXTURE_DIRNAME
    if fixture_dir.exists():
        raise FixtureExistsError(
            f"A PyCastle fixture already exists at {fixture_dir}; "
            "remove it first or scaffold into a fresh repo."
        )

    # Detect a Python project so the agent image can carry the gate toolchain
    # (#19): the Docker sandbox runs the gate inside the image, and a
    # toolchain-less image makes a Python project's in-container gate fail loud
    # (#28). Presence-check only -- an empty/malformed pyproject.toml still counts.
    python = any(
        (target_dir / manifest).exists()
        for manifest in ("pyproject.toml", "requirements.txt")
    )

    written: list[Path] = []
    for relative, content in _fixture_files(
        sandbox, python=python, setup=_setup_script(target_dir)
    ).items():
        path = fixture_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)

    # The gate is an executable script `pycastle run` invokes directly, so give
    # it mode 0755. Set the mode outright rather than OR-ing onto the
    # write-time mode so the result does not depend on the caller's umask
    # (an OR could leave it group/other-writable under a permissive umask).
    gate = fixture_dir / "gate"
    gate.chmod(0o755)
    setup = fixture_dir / "setup"
    setup.chmod(0o755)

    logger.info(
        "Scaffolded the PyCastle fixture into %s (%d files, sandbox=%s).",
        fixture_dir,
        len(written),
        sandbox,
    )
    return written
