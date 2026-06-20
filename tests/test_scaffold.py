"""The ``pycastle init`` scaffolder writes the Project fixture into a repo.

These tests exercise :func:`pycastle.scaffold.scaffold_fixture`, the deep module
that takes the host-first/Docker-first choice and writes the file tree. The
interactive prompt itself lives in the CLI and is not unit-tested here; the
scaffolding logic (the generated tree, its contents, and the host/docker
difference) is.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from pycastle.graph import DONE, PhaseGraph, load_graph
from pycastle.scaffold import FixtureExistsError, read_sandbox, scaffold_fixture

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

    # Built on node:22-slim so the bundled Claude/Codex CLIs are available (#4).
    assert "FROM node:22-slim" in dockerfile
    # The image runs as the non-root `node` user the sandbox expects.
    assert "node" in dockerfile
    # There is an obvious, documented place to add the project's own language
    # dependencies (an apt-get extension point the project edits).
    assert "apt-get" in dockerfile
    assert "RUN" in dockerfile


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_scaffolded_dockerfile_precreates_node_owned_auth_dirs(
    tmp_path: Path, choice: str
) -> None:
    """The auth-volume mount dirs are created node-owned before USER node.

    A fresh Docker named volume mounted at a path absent from the image
    initializes root-owned, which blocks the non-root `node` user from writing
    its login. The Dockerfile must mkdir + chown both auth dirs as root, before
    dropping to `node`, so a brand-new volume inherits node ownership.
    """
    scaffold_fixture(tmp_path, sandbox=choice)
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()

    # Both auth-volume mount points are created and chowned to node.
    assert "mkdir -p /home/node/.claude /home/node/.codex" in dockerfile
    assert "chown -R node:node /home/node/.claude /home/node/.codex" in dockerfile

    # The mkdir/chown runs as root, before the image drops to USER node.
    assert dockerfile.index("chown -R node:node") < dockerfile.index("USER node")


def test_scaffolded_gate_is_executable_and_has_a_shebang(tmp_path: Path) -> None:
    """The default gate is an executable shell script (so retries are reachable)."""
    scaffold_fixture(tmp_path, sandbox="host")
    gate = tmp_path / ".pycastle" / "gate"
    text = gate.read_text()
    assert text.startswith("#!/")
    # Marked executable so `pycastle run` can invoke it directly.
    assert gate.stat().st_mode & 0o111


def test_scaffolded_gate_mode_is_exactly_0755(tmp_path: Path) -> None:
    """The gate is mode 0755 regardless of the caller's umask.

    The scaffolder sets the mode outright (not OR-ed onto the write-time mode),
    so a permissive umask cannot leave the gate group/other-writable. We force a
    permissive umask for this test to prove the mode does not depend on it.
    """
    old_umask = os.umask(0o000)
    try:
        scaffold_fixture(tmp_path, sandbox="host")
    finally:
        os.umask(old_umask)
    gate = tmp_path / ".pycastle" / "gate"
    assert stat.S_IMODE(gate.stat().st_mode) == 0o755


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_scaffolded_gate_passes_in_a_fresh_repo_with_no_tooling(tmp_path: Path) -> None:
    """The default gate exits 0 in a brand-new repo before any tools are added.

    Each step is guarded by ``command -v``, so a missing ruff/black/pytest is
    skipped rather than failing. This keeps the #14 retry-with-handoff path
    reachable (the gate is real and runnable) without an empty project's first
    run spuriously failing its own gate. We run it with a minimal PATH that has
    bash and coreutils but none of the Python tools.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    gate = tmp_path / ".pycastle" / "gate"

    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"  # bash + coreutils, but no ruff/black/pytest
    proc = subprocess.run(
        [str(gate)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    # The absent tools are skipped, not run, so the empty repo passes its gate.
    assert "skipping gate step" in proc.stdout


# --------------------------------------------------------------------------- #
# Every scaffolded file is non-empty                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_every_scaffolded_file_is_non_empty(tmp_path: Path, choice: str) -> None:
    """No scaffolded file is empty: each carries real, usable content."""
    written = scaffold_fixture(tmp_path, sandbox=choice)
    for path in written:
        assert path.read_text().strip(), f"{path} is empty"


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

    assert marker.read_text() == original


# --------------------------------------------------------------------------- #
# read_sandbox: the marker reader pycastle run uses for its default            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_read_sandbox_round_trips_the_written_marker(
    tmp_path: Path, choice: str
) -> None:
    """``read_sandbox`` reads back exactly the choice the scaffolder recorded."""
    scaffold_fixture(tmp_path, sandbox=choice)
    assert read_sandbox(tmp_path / ".pycastle") == choice


def test_read_sandbox_returns_none_when_marker_is_absent(tmp_path: Path) -> None:
    """A fixture dir with no ``sandbox`` marker reads as ``None`` (not a crash)."""
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    assert read_sandbox(fixture) is None


def test_read_sandbox_returns_none_when_fixture_dir_is_missing(
    tmp_path: Path,
) -> None:
    """A missing fixture dir reads as ``None`` rather than raising."""
    assert read_sandbox(tmp_path / "nope") is None


@pytest.mark.parametrize("blank", ["", "   ", "\n", "  \n\t "])
def test_read_sandbox_treats_empty_marker_as_none(tmp_path: Path, blank: str) -> None:
    """An empty or whitespace-only marker reads as ``None``."""
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "sandbox").write_text(blank)
    assert read_sandbox(fixture) is None


def test_read_sandbox_strips_and_lowercases(tmp_path: Path) -> None:
    """The marker value is stripped and lower-cased for a forgiving read."""
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "sandbox").write_text("  DOCKER \n")
    assert read_sandbox(fixture) == "docker"


def test_refused_init_writes_nothing_new(tmp_path: Path) -> None:
    """A refused second init leaves the fixture byte-for-byte as it was.

    Clobber protection must be an all-or-nothing refusal: the second init must
    not add, replace, or partially write any file before raising. We snapshot
    the whole fixture, attempt a clobbering init, and assert the tree is
    unchanged down to file contents.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    fixture_dir = tmp_path / ".pycastle"

    def snapshot() -> dict[str, str]:
        return {
            p.relative_to(fixture_dir).as_posix(): p.read_text()
            for p in fixture_dir.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    with pytest.raises(FixtureExistsError):
        scaffold_fixture(tmp_path, sandbox="docker")

    # Same set of files, each with identical content -- nothing new was written.
    assert snapshot() == before


# --------------------------------------------------------------------------- #
# Scaffolding leaves the rest of the target dir untouched                      #
# --------------------------------------------------------------------------- #


def test_scaffold_does_not_disturb_existing_files_in_the_target_dir(
    tmp_path: Path,
) -> None:
    """Scaffolding into a non-empty repo touches only ``.pycastle/``.

    A real repo already has its own files when ``pycastle init`` runs. The
    scaffolder must add ``.pycastle/`` without reading, moving, or overwriting
    anything already in the target directory.
    """
    # Pre-existing project files alongside (and below) where .pycastle/ lands.
    (tmp_path / "README.md").write_text("# My project\n")
    (tmp_path / "main.py").write_text("print('mine')\n")  # same basename, root level
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n")

    existing_before = {
        p.relative_to(tmp_path).as_posix(): p.read_text()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }

    scaffold_fixture(tmp_path, sandbox="host")

    # Every pre-existing file is still present and byte-identical.
    for rel, content in existing_before.items():
        assert (tmp_path / rel).read_text() == content

    # The scaffold added only files under .pycastle/.
    new_files = {
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    } - set(existing_before)
    assert new_files
    assert all(rel.startswith(".pycastle/") for rel in new_files)
