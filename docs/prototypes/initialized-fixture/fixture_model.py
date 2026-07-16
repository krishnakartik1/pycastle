"""Pure state and fixture model for the throwaway init-fixture prototype."""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class FixtureFile:
    path: str
    mode: str
    purpose: str
    content: str


@dataclass(frozen=True)
class PrototypeState:
    sandbox: str = "host"
    selected: int = 0


SETUP = """#!/bin/sh
# Project-owned preparation for the current worktree.
#
# PyCastle invokes this executable directly before every Runtime node and Gate
# node. PYCASTLE_SCOPE is either "item" or "run". Only durable filesystem or
# external-system effects survive; shell activation and exported variables do
# not. Keep this safe to run repeatedly.
#
# Add the commands that prepare this project. PyCastle deliberately does not
# inspect dependency manifests or choose a language toolchain.
set -eu
:
"""


GATE = """#!/bin/sh
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


MAIN = '''"""Project-owned PyCastle Run definition.

Gate placement and recovery are ordinary graph topology. Every Gate node invokes
the same frozen `.pycastle/gate`; a Gate-node name is identity, not a hook name.
"""

from pycastle.graph import (
    DONE,
    HUMAN,
    build_run,
    execution_graph,
    gate_node,
    runtime_node,
)


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
            gate_node(
                "run-verify",
                on_success=DONE,
                on_failure="run-repair",
            ),
            runtime_node("run-repair", "run-repair.md", on_success="run-report"),
        ],
    ),
)
'''


DOCKERFILE = """# Project-owned PyCastle Agent image.
# PyCastle builds this file from the repository root and pins the resulting
# image for one Run. Once initialized, this entire file belongs to the project.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.210
ARG CODEX_VERSION=0.144.5

RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates git procps \\
    && rm -rf /var/lib/apt/lists/* \\
    && npm install -g \\
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \\
        "@openai/codex@${CODEX_VERSION}" \\
    && npm cache clean --force

RUN useradd --create-home --shell /bin/sh pycastle \\
    && install -d -o pycastle -g pycastle /pycastle/auth

# --- PROJECT TOOLCHAIN -----------------------------------------------------
# Install the interpreters, compilers, package managers, and OS libraries that
# this project's Setup, Runtime nodes, and Gate require. PyCastle never fills
# this section by inspecting repository manifests.
# ---------------------------------------------------------------------------

USER pycastle
ENV HOME=/home/pycastle
WORKDIR /home/pycastle
"""


PROMPTS = {
    "prompts/plan.md": """# Plan

Plan the smallest change that satisfies the current Item. Read `CONTEXT.md` and
relevant ADRs. Inspect the existing code, then write the plan to
`.pycastle/plan.md`. Do not implement or commit yet.
""",
    "prompts/implement.md": """# Implement

Read `.pycastle/plan.md` and implement the current Item. Add focused tests and
commit the change. Review and verification are separate following nodes; do not
invent a substitute Gate invocation.
""",
    "prompts/repair.md": """# Repair

The immediately preceding Gate failed. Use its typed termination and bounded
stdout/stderr evidence together with the current worktree to repair the change.
Add or adjust tests when appropriate and commit the repair. The graph will apply
the Gate again in a fresh visit.
""",
    "prompts/review.md": """# Review

Review the current Item diff against its acceptance criteria and the project
domain language. Fix defects and commit any changes. A fresh Gate node performs
the final verification.
""",
    "prompts/run-review.md": """# Integrated Run review

Review the integrated Run diff for cross-Item defects. Fix defects and commit any
changes. Record the findings and fixes in `.pycastle/run-review.md`, or write
`No findings`. A fresh Gate node performs verification. Do not mutate GitHub.
""",
    "prompts/run-repair.md": """# Integrated Run repair

The immediately preceding Run-scope Gate failed. Use its typed termination and
bounded stdout/stderr evidence, `.pycastle/run-review.md`, and the current
worktree to repair the integrated Run. Commit any changes. The graph will
regenerate the Run report, then apply the Gate again in a fresh visit.
""",
    "prompts/run-report.md": """# Run report

Summarize the candidate integrated diff in `.pycastle/run-report.md`. Do not
claim final verification; PyCastle adds the later Gate result to its factual PR
envelope. Do not mutate tracked project files, call GitHub, or include secrets or
raw unbounded logs.
""",
}


GITIGNORE = """# Ignored local Run state and worktrees.
/logs/
/runs/
/worktrees/

# Runtime-authored scratch artifacts.
/plan.md
/run-review.md
/run-report.md
"""


def fixture_files(sandbox: str) -> tuple[FixtureFile, ...]:
    """Return the proposal; only the selected Sandbox marker is variable."""
    fixed = [
        FixtureFile(
            "version", "0644", "Fixture compatibility marker", "<PYCASTLE_RELEASE>\n"
        ),
        FixtureFile("sandbox", "0644", "Default Sandbox selection", f"{sandbox}\n"),
        FixtureFile("setup", "0755", "Mandatory durable preparation hook", SETUP),
        FixtureFile("gate", "0755", "Mandatory fail-closed verification hook", GATE),
        FixtureFile(
            "main.py", "0644", "Run definition and explicit graph topology", MAIN
        ),
        FixtureFile(
            "Dockerfile", "0644", "Project-owned Docker Sandbox image", DOCKERFILE
        ),
        FixtureFile(
            ".gitignore", "0644", "Local Run and Runtime scratch exclusions", GITIGNORE
        ),
    ]
    fixed.extend(
        FixtureFile(path, "0644", "Frozen Runtime-node prompt", content)
        for path, content in PROMPTS.items()
    )
    return tuple(fixed)


def reduce_state(state: PrototypeState, action: str) -> PrototypeState:
    """Apply one terminal action without performing I/O."""
    files = fixture_files(state.sandbox)
    if action == "next":
        return replace(state, selected=(state.selected + 1) % len(files))
    if action == "previous":
        return replace(state, selected=(state.selected - 1) % len(files))
    if action == "sandbox":
        return replace(state, sandbox="docker" if state.sandbox == "host" else "host")
    if action.startswith("select:"):
        index = int(action.removeprefix("select:"))
        if 0 <= index < len(files):
            return replace(state, selected=index)
    return state


def init_message(sandbox: str) -> str:
    """Return the proposed user-facing completion message."""
    return f"""Created .pycastle/ (sandbox: {sandbox}).

Before the first Run:
  1. Replace the documented no-op in .pycastle/setup if preparation is needed.
  2. Replace the fail-closed .pycastle/gate with the project's verification policy.
  3. For Docker, add project toolchains to .pycastle/Dockerfile.
  4. Review and commit the complete .pycastle/ directory.

The Gate will fail until the project defines what passing means."""
