"""The stub Runtime satisfies the interface the real adapters will."""

from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.runtime import (
    STUB_MARKER,
    ClaudeRuntime,
    CodexRuntime,
    Runtime,
    StubRuntime,
    make_runtime,
)


def test_stub_runtime_satisfies_runtime_protocol() -> None:
    assert isinstance(StubRuntime(), Runtime)


def test_stub_runtime_writes_deterministic_change(tmp_path: Path) -> None:
    result = StubRuntime().run("a prompt", cwd=tmp_path, phase="implement")

    assert (tmp_path / STUB_MARKER).is_file()
    assert STUB_MARKER in result.output
    assert result.telemetry.runtime == "stub"
    assert result.telemetry.phase == "implement"
    assert result.telemetry.num_turns == 1
    assert result.telemetry.is_error is False


def test_make_runtime_returns_stub() -> None:
    assert isinstance(make_runtime("stub"), StubRuntime)


def test_make_runtime_returns_claude() -> None:
    assert isinstance(make_runtime("claude"), ClaudeRuntime)


def test_make_runtime_returns_codex() -> None:
    assert isinstance(make_runtime("codex"), CodexRuntime)


def test_make_runtime_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        make_runtime("nope")
