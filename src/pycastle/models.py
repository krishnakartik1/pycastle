"""Typed data models passed across PyCastle module boundaries."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TokenUsage(BaseModel):
    """Per-invocation token counts reported by a Runtime, when available.

    The fields cover both runtimes' vocabularies. Claude reports
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens``; Codex reports
    ``cached_input_tokens`` (cache reads) plus ``reasoning_output_tokens`` for
    the tokens spent on hidden reasoning. Every field is optional so a record
    carries only what the runtime actually reported.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None


class Telemetry(BaseModel):
    """A per-phase record of what a Runtime invocation cost and did.

    ``duration_ms`` is the runtime-reported wall time when it supplies one
    (Claude does); ``elapsed_ms`` is PyCastle's own measured wall time, used by
    runtimes that do not report duration (Codex). ``thread_id`` is the resumable
    conversation handle a runtime exposes for handoffs (Codex's thread id);
    ``None`` when the runtime has no resume concept.
    """

    runtime: str
    phase: str
    cost_usd: float | None = None
    duration_ms: int | None = None
    elapsed_ms: int | None = None
    num_turns: int | None = None
    usage: TokenUsage | None = None
    thread_id: str | None = None
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
