"""The ``pycastle init`` scaffolder writes the Project fixture into a repo.

These tests exercise :func:`pycastle.scaffold.scaffold_fixture`, the deep module
that takes the host-first/Docker-first choice and writes the file tree. The
interactive prompt itself lives in the CLI and is not unit-tested here; the
scaffolding logic (the generated tree, its contents, and the host/docker
difference) is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pycastle.graph import DONE, PhaseGraph, load_graph
from pycastle.scaffold import FixtureExistsError, scaffold_fixture

# The files every scaffolded fixture carries, relative to the fixture dir.
EXPECTED_TREE = {
    "main.py",
    "gate",
    "sandbox",
    "Dockerfile",
    ".gitignore",
    "prompts/plan.md",
    "prompts/implement.md",
    "prompts/review.md",
}


def _written_relative(written: list[Path], fixture_dir: Path) -> set[str]:
    """Return the written paths relative to ``fixture_dir`` as posix strings."""
    return {p.relative_to(fixture_dir).as_posix() for p in written}


# --------------------------------------------------------------------------- #
# The generated file tree (asserted for each init choice)                      #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_scaffold_writes_the_full_project_fixture(tmp_path: Path, choice: str) -> None:
    """Both choices write the same tree: main.py, Dockerfile, prompts, gitignore."""
    written = scaffold_fixture(tmp_path, sandbox=choice)
    fixture_dir = tmp_path / ".pycastle"

    # The returned list is exactly the fixture files, all of which exist on disk.
    assert _written_relative(written, fixture_dir) == EXPECTED_TREE
    for path in written:
        assert path.is_file()

    # The Builder-style main.py and the agent Dockerfile are both present.
    assert (fixture_dir / "main.py").is_file()
    assert (fixture_dir / "Dockerfile").is_file()
    # The plan/implement/review prompts are scaffolded.
    for name in ("plan.md", "implement.md", "review.md"):
        assert (fixture_dir / "prompts" / name).is_file()


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_scaffold_writes_a_gitignore_excluding_run_artifacts(
    tmp_path: Path, choice: str
) -> None:
    """The scaffolded .gitignore excludes run logs and generated run artifacts."""
    scaffold_fixture(tmp_path, sandbox=choice)
    gitignore = (tmp_path / ".pycastle" / ".gitignore").read_text()

    # Run logs and generated run artifacts are excluded so they never get
    # committed (mirrors the repo's own root .gitignore for these paths).
    assert "logs/" in gitignore
    assert "runs/" in gitignore
    assert "worktrees/" in gitignore


def test_gitignore_is_in_the_written_list(tmp_path: Path) -> None:
    """The .gitignore is one of the files scaffold_fixture reports writing."""
    written = scaffold_fixture(tmp_path, sandbox="host")
    names = {p.name for p in written}
    assert ".gitignore" in names


# --------------------------------------------------------------------------- #
# Host-first vs Docker-first: an observable, tested difference                 #
# --------------------------------------------------------------------------- #


def test_host_choice_records_host_as_the_default_sandbox(tmp_path: Path) -> None:
    """Host-first writes a `sandbox` marker reading ``host``."""
    scaffold_fixture(tmp_path, sandbox="host")
    marker = (tmp_path / ".pycastle" / "sandbox").read_text().strip()
    assert marker == "host"


def test_docker_choice_records_docker_as_the_default_sandbox(
    tmp_path: Path,
) -> None:
    """Docker-first writes a `sandbox` marker reading ``docker``."""
    scaffold_fixture(tmp_path, sandbox="docker")
    marker = (tmp_path / ".pycastle" / "sandbox").read_text().strip()
    assert marker == "docker"


def test_the_two_choices_differ_only_in_the_sandbox_marker(tmp_path: Path) -> None:
    """The host and docker trees differ in exactly one file: the sandbox marker."""
    host_dir = tmp_path / "host_repo"
    docker_dir = tmp_path / "docker_repo"
    host_dir.mkdir()
    docker_dir.mkdir()

    scaffold_fixture(host_dir, sandbox="host")
    scaffold_fixture(docker_dir, sandbox="docker")

    host_files = sorted(
        p.relative_to(host_dir / ".pycastle").as_posix()
        for p in (host_dir / ".pycastle").rglob("*")
        if p.is_file()
    )
    docker_files = sorted(
        p.relative_to(docker_dir / ".pycastle").as_posix()
        for p in (docker_dir / ".pycastle").rglob("*")
        if p.is_file()
    )
    # Same tree shape for both choices.
    assert host_files == docker_files

    # Every file is byte-identical except the sandbox marker.
    for rel in host_files:
        host_text = (host_dir / ".pycastle" / rel).read_text()
        docker_text = (docker_dir / ".pycastle" / rel).read_text()
        if rel == "sandbox":
            assert host_text != docker_text
        else:
            assert host_text == docker_text


def test_invalid_sandbox_choice_raises(tmp_path: Path) -> None:
    """A sandbox value outside host/docker is rejected before anything is written."""
    with pytest.raises(ValueError, match="sandbox"):
        scaffold_fixture(tmp_path, sandbox="podman")  # type: ignore[arg-type]
    # Nothing was written on the rejected path.
    assert not (tmp_path / ".pycastle").exists()


# --------------------------------------------------------------------------- #
# The scaffolded main.py uses the #10 Builder API and loads as a valid graph   #
# --------------------------------------------------------------------------- #


def test_scaffolded_main_uses_the_declarative_builder_api(tmp_path: Path) -> None:
    """The generated main.py is authored with the #10 declarative Builder API."""
    scaffold_fixture(tmp_path, sandbox="host")
    main_py = (tmp_path / ".pycastle" / "main.py").read_text()

    # Authored with build(start=, phases=[phase(...)]) and the terminals.
    assert "from pycastle.graph import" in main_py
    assert "build(" in main_py
    assert "phase(" in main_py
    assert "DONE" in main_py
    assert "HUMAN" in main_py
    assert "graph = build(" in main_py


def test_scaffolded_main_loads_as_a_valid_phase_graph(tmp_path: Path) -> None:
    """The scaffolded main.py imports and yields a walkable plan→...→DONE graph."""
    scaffold_fixture(tmp_path, sandbox="host")
    fixture_dir = tmp_path / ".pycastle"

    graph = load_graph(fixture_dir)
    assert isinstance(graph, PhaseGraph)

    # The conservative default flow: plan → implement → review → DONE.
    assert graph.start == "plan"
    assert set(graph.phases) == {"plan", "implement", "review"}
    assert graph.phases["plan"].on_success == "implement"
    assert graph.phases["implement"].on_success == "review"
    assert graph.phases["review"].on_success is DONE

    # Every named prompt file the graph references was actually scaffolded.
    for ph in graph.phases.values():
        assert (fixture_dir / "prompts" / ph.prompt).is_file()


def test_scaffolded_graph_walks_to_done_end_to_end(tmp_path: Path) -> None:
    """Walking the scaffolded graph from start reaches DONE when phases pass.

    This proves the conservative default graph runs end to end before any
    customization — the executor follows the success edges plan → implement →
    review → DONE without touching a real runtime.
    """
    from pycastle.graph import GraphExecutor, Phase, PhaseResult

    scaffold_fixture(tmp_path, sandbox="host")
    fixture_dir = tmp_path / ".pycastle"
    graph = load_graph(fixture_dir)

    visited: list[str] = []

    def always_pass(ph: Phase, _extra: str | None) -> tuple[bool, list[PhaseResult]]:
        visited.append(ph.name)
        return True, []

    executor = GraphExecutor(runtime=object(), fixture_dir=fixture_dir)
    walk = executor.execute(graph, cwd=tmp_path, phase_runner=always_pass)

    assert visited == ["plan", "implement", "review"]
    assert walk.terminal is DONE


# --------------------------------------------------------------------------- #
# The scaffolded Dockerfile and gate                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_scaffolded_dockerfile_extends_the_node22_agent_image(
    tmp_path: Path, choice: str
) -> None:
    """The Dockerfile builds the node:22 agent image with an extension point."""
    scaffold_fixture(tmp_path, sandbox=choice)
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()

    # Built on node:22 so the bundled Claude/Codex CLIs are available (#4).
    assert "FROM node:22" in dockerfile
    # The image runs as the non-root `node` user the sandbox expects.
    assert "node" in dockerfile
    # There is an obvious, documented place to add the project's own language
    # dependencies (an apt-get extension point the project edits).
    assert "apt-get" in dockerfile
    assert "RUN" in dockerfile


def test_scaffolded_gate_is_executable_and_has_a_shebang(tmp_path: Path) -> None:
    """The default gate is an executable shell script (so retries are reachable)."""
    scaffold_fixture(tmp_path, sandbox="host")
    gate = tmp_path / ".pycastle" / "gate"
    text = gate.read_text()
    assert text.startswith("#!/")
    # Marked executable so `pycastle run` can invoke it directly.
    assert gate.stat().st_mode & 0o111


# --------------------------------------------------------------------------- #
# Clobber protection                                                           #
# --------------------------------------------------------------------------- #


def test_scaffold_refuses_to_clobber_an_existing_fixture(tmp_path: Path) -> None:
    """A second init into a repo that already has .pycastle/ is refused."""
    scaffold_fixture(tmp_path, sandbox="host")
    with pytest.raises(FixtureExistsError):
        scaffold_fixture(tmp_path, sandbox="host")


def test_clobber_refusal_leaves_the_existing_fixture_untouched(
    tmp_path: Path,
) -> None:
    """The refusal does not overwrite or delete the fixture already on disk."""
    scaffold_fixture(tmp_path, sandbox="host")
    marker = tmp_path / ".pycastle" / "sandbox"
    original = marker.read_text()

    with pytest.raises(FixtureExistsError):
        scaffold_fixture(tmp_path, sandbox="docker")

    # The on-disk marker still reads the original host choice, not docker.
    assert marker.read_text() == original
