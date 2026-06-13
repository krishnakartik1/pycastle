"""Typed data models passed across PyCastle module boundaries."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Telemetry(BaseModel):
    """A per-phase record of what a Runtime invocation cost and did."""

    runtime: str
    phase: str
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    is_error: bool = False


class RuntimeResult(BaseModel):
    """Parsed output text plus telemetry returned by a Runtime."""

    output: str
    telemetry: Telemetry


class IssueRef(BaseModel):
    """A single work item surfaced by an Issue source."""

    number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
