"""Create the language-agnostic Project fixture used by ``pycastle init``."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from . import __version__
from .compatibility import VERSION_MARKER

logger = logging.getLogger("pycastle")

FIXTURE_DIRNAME = ".pycastle"
SANDBOX_MARKER = "sandbox"
SandboxChoice = Literal["host", "docker"]


def read_sandbox(fixture_dir: Path) -> str | None:
    """Return the normalized Sandbox marker, or ``None`` when unavailable."""
    try:
        recorded = (fixture_dir / SANDBOX_MARKER).read_text().strip().lower()
    except OSError:
        return None
    return recorded or None


class FixtureExistsError(Exception):
    """Raised when scaffolding would overwrite an existing Project fixture."""


_MAIN_PY = '''"""Project-owned PyCastle Run definition.

Gate placement and recovery are ordinary graph topology. Every Gate node invokes
the same frozen `.pycastle/gate`; a Gate-node name is identity, not a hook name.
"""

from pycastle.graph import DONE, build_run, execution_graph, gate_node, runtime_node

run = build_run(
    item=execution_graph(
        start="plan",
        nodes=[
            runtime_node("plan", "plan.md", on_success="implement"),
            runtime_node("implement", "implement.md", on_success="review"),
            runtime_node("review", "review.md", on_success="verify"),
            gate_node("verify", on_success=DONE, on_failure="repair"),
            runtime_node("repair", "repair.md", on_success="verify"),
        ],
    ),
    after=execution_graph(
        start="run-review",
        nodes=[
            runtime_node("run-review", "run-review.md", on_success="run-report"),
            runtime_node("run-report", "run-report.md", on_success="run-verify"),
            gate_node("run-verify", on_success=DONE, on_failure="run-repair"),
            runtime_node("run-repair", "run-repair.md", on_success="run-report"),
        ],
    ),
)
'''

_SETUP = """#!/bin/sh
# Project-owned preparation for the current worktree.
#
# PyCastle invokes this executable directly before every Runtime node and Gate
# node. PYCASTLE_SCOPE is either "item" or "run". Only durable filesystem or
# external-system effects survive; shell activation and exported variables do
# not. Keep this safe to run repeatedly.
#
# Add commands when this project needs preparation. PyCastle does not inspect
# dependency manifests or choose a language toolchain.
set -eu
:
"""

_GATE = """#!/bin/sh
# Project-owned verification policy for the current worktree.
#
# PyCastle invokes this executable directly from every Gate node after Setup.
# PYCASTLE_SCOPE is either "item" or "run". Exit 0 only when this worktree meets
# the project's complete verification policy.
set -eu

echo "ERROR: .pycastle/gate has not been configured." >&2
echo "Replace this body with the project's verification commands." >&2
echo "Scope: ${PYCASTLE_SCOPE:-unset}" >&2
exit 1
"""

_DOCKERFILE = """# Project-owned PyCastle Agent image.
# PyCastle builds this file from the repository root and pins the resulting
# image for one Run. Once initialized, this entire file belongs to the project.
FROM node:22-bookworm-slim

ARG PYCASTLE_HOST_UID
ARG PYCASTLE_HOST_GID
ARG CLAUDE_CODE_VERSION=2.1.210
ARG CODEX_VERSION=0.144.5

RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates git procps \\
    && rm -rf /var/lib/apt/lists/* \\
    && npm install -g \\
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \\
        "@openai/codex@${CODEX_VERSION}" \\
    && npm cache clean --force

RUN set -eu; \\
    existing_group="$(getent group "${PYCASTLE_HOST_GID}" | cut -d: -f1 || true)"; \\
    if [ -z "${existing_group}" ]; then \\
        groupadd --gid "${PYCASTLE_HOST_GID}" pycastle; \\
    fi; \\
    existing_user="$(getent passwd "${PYCASTLE_HOST_UID}" | cut -d: -f1 || true)"; \\
    if [ -n "${existing_user}" ]; then \\
        usermod --login pycastle --home /home/pycastle --move-home \\
            --gid "${PYCASTLE_HOST_GID}" --shell /bin/sh "${existing_user}"; \\
    else \\
        useradd --uid "${PYCASTLE_HOST_UID}" --gid "${PYCASTLE_HOST_GID}" \\
            --create-home --shell /bin/sh pycastle; \\
    fi; \\
    install -d -o "${PYCASTLE_HOST_UID}" -g "${PYCASTLE_HOST_GID}" /pycastle/auth

# --- PROJECT TOOLCHAIN -----------------------------------------------------
# Install the interpreters, compilers, package managers, and OS libraries that
# this project's Setup, Runtime nodes, and Gate require. PyCastle never fills
# this section by inspecting repository manifests.
# ---------------------------------------------------------------------------

USER pycastle
ENV HOME=/home/pycastle
WORKDIR /home/pycastle
"""

_PROMPTS = {
    "plan.md": """# Plan

Plan the smallest change that satisfies the current Item. Read `CONTEXT.md` and
relevant ADRs. Inspect the existing code, then write the plan to
`.pycastle/plan.md`. Do not implement or commit yet.
""",
    "implement.md": """# Implement

Read `.pycastle/plan.md` and implement the current Item. Add focused tests and
commit the change. Review and verification are separate following nodes; do not
invent a substitute Gate invocation.
""",
    "review.md": """# Review

Review the current Item diff against its acceptance criteria and project domain
language. Fix defects and commit changes. A fresh Gate node performs final
verification.
""",
    "repair.md": """# Repair

The immediately preceding Gate failed. Use its typed termination and bounded
stdout/stderr evidence with the current worktree to repair the change. Add tests
and commit the repair. The graph will apply the Gate again in a fresh visit.
""",
    "run-review.md": """# Integrated Run review

Review the integrated Run diff for cross-Item defects. Fix and commit defects.
Record findings and fixes in `.pycastle/run-review.md`, or write `No findings`.
A fresh Gate node performs verification. Do not mutate GitHub.
""",
    "run-report.md": """# Run report

Summarize the candidate integrated diff in `.pycastle/run-report.md`. Do not
claim final verification; PyCastle adds the later Gate result to its factual PR
envelope. Do not mutate tracked files, call GitHub, or include secrets or raw
unbounded logs.
""",
    "run-repair.md": """# Integrated Run repair

The immediately preceding Run-scope Gate failed. Use its typed termination and
bounded evidence, `.pycastle/run-review.md`, and the worktree to repair the Run.
Commit changes. The graph will regenerate the report, then apply Gate again.
""",
}

_GITIGNORE = """# Ignored local Run state and worktrees.
/logs/
/runs/
/worktrees/

# Runtime-authored scratch artifacts.
/plan.md
/run-review.md
/run-report.md
"""


def _fixture_files(sandbox: SandboxChoice) -> dict[str, str]:
    """Return invariant fixture content; only the Sandbox marker is variable."""
    files = {
        "main.py": _MAIN_PY,
        "setup": _SETUP,
        "gate": _GATE,
        "Dockerfile": _DOCKERFILE,
        ".gitignore": _GITIGNORE,
        SANDBOX_MARKER: f"{sandbox}\n",
        VERSION_MARKER: f"{__version__}\n",
    }
    files.update({f"prompts/{name}": text for name, text in _PROMPTS.items()})
    return files


def scaffold_fixture(target_dir: Path, *, sandbox: SandboxChoice) -> list[Path]:
    """Write a deterministic Project fixture beneath ``target_dir``."""
    if sandbox not in ("host", "docker"):
        raise ValueError(f"sandbox must be 'host' or 'docker', not {sandbox!r}")
    fixture_dir = target_dir / FIXTURE_DIRNAME
    if fixture_dir.exists():
        raise FixtureExistsError(
            f"A PyCastle fixture already exists at {fixture_dir}; "
            "remove it first or scaffold into a fresh repo."
        )

    written: list[Path] = []
    for relative, content in _fixture_files(sandbox).items():
        path = fixture_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755 if relative in {"setup", "gate"} else 0o644)
        written.append(path)

    logger.info(
        "Scaffolded the PyCastle fixture into %s (%d files, sandbox=%s).",
        FIXTURE_DIRNAME,
        len(written),
        sandbox,
    )
    return written
