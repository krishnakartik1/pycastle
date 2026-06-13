"""The Runtime boundary: one interface over Claude Code and Codex.

v0.1 ships a :class:`StubRuntime` that fakes an agent so the loop's plumbing
can be proven end to end. The real Claude and Codex adapters land behind this
same interface in later slices.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .models import RuntimeResult, Telemetry

STUB_MARKER = "PYCASTLE_STUB.md"


@runtime_checkable
class Runtime(Protocol):
    """Take a prompt plus options and return output text and telemetry."""

    name: str

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        """Execute the agent for one phase and return its parsed result."""
        ...


class StubRuntime:
    """A deterministic fake agent.

    Instead of calling a real CLI it writes a known marker file into the
    working tree, so a run produces a real, reviewable change with no agent
    and no network.
    """

    name = "stub"

    def run(self, prompt: str, *, cwd: Path, phase: str) -> RuntimeResult:
        """Write the marker file and return fixed output and telemetry."""
        marker = cwd / STUB_MARKER
        marker.write_text(
            f"# PyCastle stub runtime\n\nPhase: {phase}\nPrompt bytes: {len(prompt)}\n"
        )
        return RuntimeResult(
            output=f"stub wrote {STUB_MARKER} during {phase}",
            telemetry=Telemetry(runtime=self.name, phase=phase, num_turns=1),
        )


def make_runtime(name: str) -> Runtime:
    """Return the Runtime registered under ``name``.

    Only the stub exists in v0.1; the Claude and Codex adapters are wired in
    behind this same factory in later slices.
    """
    if name == "stub":
        return StubRuntime()
    if name in {"claude", "codex"}:
        raise NotImplementedError(f"The {name!r} runtime lands in a later slice.")
    raise ValueError(f"Unknown runtime: {name!r}")
