"""Preflight fails fast when a required command is missing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pycastle import preflight
from pycastle.preflight import (
    PreflightError,
    check_docker_gate_toolchain,
    check_required_commands,
)


def test_passes_when_all_commands_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "command_exists", lambda name: True)
    check_required_commands(["git", "gh"])  # should not raise


def test_raises_and_names_missing_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "command_exists", lambda name: name != "gh")
    with pytest.raises(PreflightError) as excinfo:
        check_required_commands(["git", "gh"])
    assert "gh" in str(excinfo.value)
    assert "git" not in str(excinfo.value)


def test_docker_gate_toolchain_check_runs_gate_check_mode_in_resolved_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    gate = fixture / "gate"
    gate.write_text("#!/usr/bin/env bash\n")
    wrapped: dict[str, object] = {}

    def fake_build_run_command(runtime_name: str, **kwargs: object) -> list[str]:
        wrapped.update(runtime_name=runtime_name, **kwargs)
        return ["docker", "run", "agent:test", "gate-check"]

    monkeypatch.setattr(preflight.sandbox, "build_run_command", fake_build_run_command)
    calls: list[tuple[list[str], bool]] = []

    def runner(argv: list[str], *, capture: bool = False) -> SimpleNamespace:
        calls.append((argv, capture))
        return SimpleNamespace(returncode=0, stdout="tools ready\n", stderr="")

    check_docker_gate_toolchain(
        fixture,
        image="agent:test",
        runtime_name="codex",
        workspace=tmp_path,
        runner=runner,
    )

    assert wrapped == {
        "runtime_name": "codex",
        "inner_argv": ["bash", str(gate.resolve()), "--check-tools"],
        "workspace": tmp_path,
        "workdir": tmp_path,
        "image": "agent:test",
    }
    assert calls == [(["docker", "run", "agent:test", "gate-check"], True)]


def test_docker_gate_toolchain_check_fails_with_output_and_remediation(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "gate").write_text("#!/usr/bin/env bash\n")

    def runner(_argv: list[str], *, capture: bool = False) -> SimpleNamespace:
        assert capture is True
        return SimpleNamespace(
            returncode=1,
            stdout="Missing gate tools: ruff black\n",
            stderr="pytest: command not found\n",
        )

    with pytest.raises(PreflightError) as exc_info:
        check_docker_gate_toolchain(
            fixture,
            image="custom/agent:bad",
            runtime_name="claude",
            workspace=tmp_path,
            runner=runner,
        )

    message = str(exc_info.value)
    assert "custom/agent:bad" in message
    assert "Missing gate tools: ruff black" in message
    assert "pytest: command not found" in message
    assert ".pycastle/Dockerfile" in message
    assert "--image" in message


def test_docker_gate_toolchain_check_is_noop_without_gate(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()

    def runner(_argv: list[str], *, capture: bool = False) -> SimpleNamespace:
        raise AssertionError("a missing gate must not launch docker")

    check_docker_gate_toolchain(
        fixture,
        image="agent:test",
        runtime_name="claude",
        workspace=tmp_path,
        runner=runner,
    )


def test_docker_gate_toolchain_check_wraps_launch_error(tmp_path: Path) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "gate").write_text("#!/usr/bin/env bash\n")

    def runner(_argv: list[str], *, capture: bool = False) -> SimpleNamespace:
        raise OSError("docker daemon unavailable")

    with pytest.raises(PreflightError, match="docker daemon unavailable"):
        check_docker_gate_toolchain(
            fixture,
            image="agent:test",
            runtime_name="claude",
            workspace=tmp_path,
            runner=runner,
        )
