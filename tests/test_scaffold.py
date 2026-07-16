import stat
import subprocess
from pathlib import Path

import pytest

from pycastle.graph import DONE, GateNode, RuntimeNode, load_run
from pycastle.scaffold import FixtureExistsError, scaffold_fixture


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    fixture = root / ".pycastle"
    return {
        str(path.relative_to(fixture)): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in fixture.rglob("*")
        if path.is_file()
    }


def test_scaffold_is_language_neutral_and_sandboxes_only_change_marker(
    tmp_path: Path,
) -> None:
    host, docker = tmp_path / "host", tmp_path / "docker"
    host.mkdir()
    docker.mkdir()
    for root in (host, docker):
        for manifest in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod"):
            (root / manifest).write_text("unrelated")
    scaffold_fixture(host, sandbox="host")
    scaffold_fixture(docker, sandbox="docker")
    host_files, docker_files = _snapshot(host), _snapshot(docker)
    assert host_files.keys() == docker_files.keys()
    assert {name for name in host_files if host_files[name] != docker_files[name]} == {
        "sandbox"
    }
    assert b"pip install" not in host_files["setup"][0]
    assert b"python3 -m venv" not in host_files["setup"][0]


def test_scaffold_ignores_all_repository_contents(tmp_path: Path) -> None:
    clean, noisy = tmp_path / "clean", tmp_path / "noisy"
    clean.mkdir()
    noisy.mkdir()
    (noisy / "package.json").write_text("not json")
    (noisy / "nested").mkdir()
    (noisy / "nested/Cargo.toml").write_text("[broken")
    (noisy / "go.mod").write_text("module unrelated")
    (noisy / "executable").write_text("#!/bin/sh\n")
    (noisy / "executable").chmod(0o755)

    scaffold_fixture(clean, sandbox="host")
    scaffold_fixture(noisy, sandbox="host")

    assert _snapshot(clean) == _snapshot(noisy)


@pytest.mark.parametrize("sandbox", ["host", "docker"])
def test_hooks_are_direct_executables_with_safe_defaults(
    tmp_path: Path, sandbox: str
) -> None:
    scaffold_fixture(tmp_path, sandbox=sandbox)  # type: ignore[arg-type]
    setup = tmp_path / ".pycastle/setup"
    gate = tmp_path / ".pycastle/gate"
    assert stat.S_IMODE(setup.stat().st_mode) == 0o755
    assert stat.S_IMODE(gate.stat().st_mode) == 0o755
    for scope in ("item", "run"):
        environment = {"PYCASTLE_SCOPE": scope}
        setup_result = subprocess.run(
            [setup], stdin=subprocess.DEVNULL, env=environment, capture_output=True
        )
        gate_result = subprocess.run(
            [gate], stdin=subprocess.DEVNULL, env=environment, capture_output=True
        )
        assert setup_result.returncode == 0
        assert gate_result.returncode != 0
        assert scope.encode() in gate_result.stderr
    setup_text = setup.read_text()
    assert "repeat" in setup_text.lower()
    assert "durable" in setup_text.lower()
    gate_text = gate.read_text()
    assert "verification" in gate_text.lower()
    assert "exit 0" in gate_text.lower()


def test_initialized_graph_has_explicit_verify_and_repair_topology(
    tmp_path: Path,
) -> None:
    scaffold_fixture(tmp_path, sandbox="host")
    run = load_run(tmp_path / ".pycastle")
    assert list(run.item.nodes) == ["plan", "implement", "review", "verify", "repair"]
    assert isinstance(run.item.nodes["verify"], GateNode)
    assert isinstance(run.item.nodes["repair"], RuntimeNode)
    assert run.item.nodes["verify"].on_failure == "repair"
    assert run.item.nodes["verify"].on_success == DONE
    assert run.item.nodes["repair"].on_success == "verify"
    assert run.after is not None
    assert run.after.nodes["run-verify"].on_failure == "run-repair"
    assert run.after.nodes["run-verify"].on_success == DONE
    assert run.after.nodes["run-repair"].on_success == "run-report"
    assert list(run.after.nodes) == [
        "run-review",
        "run-report",
        "run-verify",
        "run-repair",
    ]
    for graph in (run.item, run.after):
        for node in graph.nodes.values():
            if isinstance(node, RuntimeNode):
                assert (tmp_path / ".pycastle/prompts" / node.prompt).is_file()


def test_dockerfile_is_neutral_and_has_project_extension(tmp_path: Path) -> None:
    scaffold_fixture(tmp_path, sandbox="docker")
    text = (tmp_path / ".pycastle/Dockerfile").read_text()
    assert "PROJECT TOOLCHAIN" in text
    assert "/pycastle/auth" in text
    assert "USER pycastle" in text
    assert "@anthropic-ai/claude-code" in text
    assert "@openai/codex" in text
    assert "ca-certificates" in text
    assert "git" in text
    assert "procps" in text
    assert "HOME=/home/pycastle" in text
    assert "python3" not in text and "ruff" not in text and "pytest" not in text


def test_scaffold_rejects_invalid_or_existing_fixture(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scaffold_fixture(tmp_path, sandbox="auto")  # type: ignore[arg-type]
    scaffold_fixture(tmp_path, sandbox="host")
    with pytest.raises(FixtureExistsError):
        scaffold_fixture(tmp_path, sandbox="host")
