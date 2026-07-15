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
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.version import Version

from pycastle.graph import DONE, PhaseGraph, load_graph
from pycastle.scaffold import (
    _DOCKERFILE,
    _EXTENSION_EMPTY,
    _EXTENSION_PYTHON,
    FixtureExistsError,
    read_sandbox,
    scaffold_fixture,
)

# The files every scaffolded fixture carries, relative to the fixture dir.
EXPECTED_TREE = {
    "main.py",
    "gate",
    "setup",
    "sandbox",
    "version",
    "Dockerfile",
    ".gitignore",
    "prompts/plan.md",
    "prompts/implement.md",
    "prompts/review.md",
}


def test_scaffold_records_normalized_installed_version(tmp_path: Path) -> None:
    scaffold_fixture(tmp_path, sandbox="host")

    assert (tmp_path / ".pycastle" / "version").read_text() == (
        f"{Version(version('pycastle'))}\n"
    )


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ("uv.lock", "uv sync --all-extras"),
        (
            "poetry.lock",
            "POETRY_VIRTUALENVS_CREATE=false poetry install",
        ),
        ("requirements.txt", "pip install -r requirements.txt"),
    ],
)
def test_scaffold_detects_project_setup_command(
    tmp_path: Path, manifest: str, expected: str
) -> None:
    (tmp_path / manifest).write_text("")
    scaffold_fixture(tmp_path, sandbox="docker")

    setup = tmp_path / ".pycastle" / "setup"
    assert expected in setup.read_text()
    assert stat.S_IMODE(setup.stat().st_mode) == 0o755


def test_scaffolded_pyproject_setup_falls_back_without_dev_extra(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    scaffold_fixture(tmp_path, sandbox="docker")

    setup = (tmp_path / ".pycastle" / "setup").read_text()
    assert 'pip install -e ".[dev]" || pip install -e .' in setup


def test_scaffold_setup_manifest_precedence_is_deterministic(tmp_path: Path) -> None:
    for manifest in ("uv.lock", "poetry.lock", "pyproject.toml", "requirements.txt"):
        (tmp_path / manifest).write_text("")

    scaffold_fixture(tmp_path, sandbox="docker")

    setup = (tmp_path / ".pycastle" / "setup").read_text()
    assert "uv sync --all-extras" in setup
    assert "poetry install" not in setup
    assert "pip install" not in setup


def test_scaffolded_setup_is_a_documented_noop_without_manifest(tmp_path: Path) -> None:
    scaffold_fixture(tmp_path, sandbox="host")

    setup = (tmp_path / ".pycastle" / "setup").read_text()
    assert "No supported dependency manifest was found" in setup
    assert "exit 0" in setup


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


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_scaffold_gitignore_excludes_runtime_scratch_files(
    tmp_path: Path, choice: str
) -> None:
    """The scaffolded .gitignore excludes the Runtime's scratch files (#68).

    A phase's plan, a retried attempt's handoff, and any issue scratch land inside
    .pycastle/ during a run. If they are not ignored, the orchestrator's
    ``git add -A`` folds them into the issue branch and the run's PR (and drifts
    the committed fixture). The patterns are anchored (leading ``/``) to .pycastle/
    so they never shadow the tracked ``prompts/plan.md``.
    """
    scaffold_fixture(tmp_path, sandbox=choice)
    gitignore = (tmp_path / ".pycastle" / ".gitignore").read_text()

    assert "/handoff.md" in gitignore
    assert "/plan.md" in gitignore
    assert "/issue.md" in gitignore
    assert "/plan-issue-*.md" in gitignore


def test_scaffolded_gitignore_keeps_scratch_out_of_git_add(tmp_path: Path) -> None:
    """``git add -A`` on a scaffolded repo never stages Runtime scratch (#68).

    The orchestrator commits an issue's work with ``git add -A``. Without the
    scratch-file ignores, the handoff/plan/issue documents a run drops into
    .pycastle/ would be staged and committed into the issue branch (and merged
    into the PR). This scaffolds into a fresh git repo, drops those exact strays,
    stages everything, and confirms none of them are tracked -- while the real
    tracked ``prompts/plan.md`` still is (proving the anchoring did not shadow a
    same-basename file one directory down).
    """
    scaffold_fixture(tmp_path, sandbox="host")
    fixture = tmp_path / ".pycastle"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)

    # The exact scratch files a run drops into .pycastle/ (a phase's plan, a
    # retried attempt's handoff, an issue scratch).
    for name in ("handoff.md", "plan.md", "issue.md", "plan-issue-19.md"):
        (fixture / name).write_text(f"stray {name}\n")

    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    # None of the scratch strays were staged...
    for name in ("handoff.md", "plan.md", "issue.md", "plan-issue-19.md"):
        assert f".pycastle/{name}" not in tracked
    # ...but the tracked prompt of the same basename still is (anchoring holds).
    assert ".pycastle/prompts/plan.md" in tracked


def test_scaffolded_gitignore_ignores_the_real_handoff_path(tmp_path: Path) -> None:
    """The scaffolded ignore stays coupled to the path the orchestrator writes (#68).

    The ignore patterns are hand-maintained string literals in the scaffolder,
    while the retry handoff path is a constant in the orchestrator
    (:data:`pycastle.orchestrator.HANDOFF_DOC`). If that constant is ever renamed
    without updating the ignore, the handoff would silently start riding into the
    issue branch again -- the exact regression #68 fixed. Asserting the *real*
    path is ignored (via ``git check-ignore``, git's own matcher) keeps the two
    from drifting apart. ``HANDOFF_DOC`` is repo-relative, so it is the path git
    resolves against the scaffolded ``.pycastle/.gitignore``.
    """
    from pycastle.orchestrator import HANDOFF_DOC

    scaffold_fixture(tmp_path, sandbox="host")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    ignored = subprocess.run(
        ["git", "check-ignore", HANDOFF_DOC],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    # git check-ignore exits 0 (and echoes the path) only when it is ignored.
    assert ignored.returncode == 0, (
        f"the orchestrator writes its handoff to {HANDOFF_DOC!r}, but the "
        f"scaffolded .pycastle/.gitignore does not ignore it: {ignored.stderr}"
    )


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

    # Both the mkdir and the chown run as root, before the image drops to
    # USER node -- a non-root user cannot create or chown a root-owned path.
    user_node = dockerfile.index("USER node")
    assert dockerfile.index("mkdir -p /home/node/.claude") < user_node
    assert dockerfile.index("chown -R node:node") < user_node


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_scaffolded_dockerfile_installs_ca_certificates(
    tmp_path: Path, choice: str
) -> None:
    """ca-certificates is installed as root before USER node (#46).

    node:22-slim ships an empty system trust store. Codex is a Rust binary
    whose TLS stack verifies certs against that store, so without
    ca-certificates it cannot reach auth.openai.com. Claude is unaffected
    because the Node CLI bundles its own roots.
    """
    scaffold_fixture(tmp_path, sandbox=choice)
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()

    # The package is installed.
    assert "ca-certificates" in dockerfile
    # Installed as root, before the image drops to USER node -- a non-root
    # user cannot apt-get install into the system trust store.
    ca_at = dockerfile.index("ca-certificates")
    assert ca_at < dockerfile.index("USER node")
    # The apt cache is dropped in the *same* RUN layer as the install, so the
    # ca-certificates install does not bloat the image with the package lists.
    # The cleanup must land between the install and the next RUN (the npm one) --
    # asserting only "before USER node" is vacuous because the commented example
    # block further down also carries an apt-cache cleanup line.
    install_block_end = dockerfile.index("RUN npm install")
    block = dockerfile[ca_at:install_block_end]
    assert "rm -rf /var/lib/apt/lists/*" in block


@pytest.mark.parametrize("python", [True, False])
def test_scaffolded_dockerfile_installs_git(tmp_path: Path, python: bool) -> None:
    """git is installed in the base image for every stack (#57).

    The implement/review prompts tell the agent to commit and to read "the diff
    produced so far", which need git regardless of language. node:22-slim ships
    no git, so without this layer codex burns the run reinventing it (Dulwich).
    git lives in the base layer, before the PROJECT EXTENSION POINT, so a
    Python project (python=True) and a non-Python one (python=False) both get
    it, and it is installed as root before the image drops to USER node.
    """
    if python:
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    scaffold_fixture(tmp_path, sandbox="docker")
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()

    # git is installed as root, before the image drops to the non-root node user.
    git_at = dockerfile.index("install -y --no-install-recommends ca-certificates git")
    assert git_at < dockerfile.index("USER node")
    # It lives in the base, before the extension point -- not in a stack block.
    assert git_at < dockerfile.index("# --- PROJECT EXTENSION POINT")


# --------------------------------------------------------------------------- #
# Python-aware Dockerfile: the agent image carries the gate toolchain (#19)     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_python_project_dockerfile_carries_the_gate_toolchain(
    tmp_path: Path, choice: str
) -> None:
    """A pyproject.toml at scaffold time pre-fills the extension point (#19).

    The Docker sandbox runs the quality gate INSIDE the agent image, and a
    toolchain-less image makes a Python project's in-container gate fail loud
    (#28). So a detected Python project gets python3 + ruff/black/pytest baked
    into the PROJECT EXTENSION POINT. The Dockerfile is identical across the
    sandbox choice, hence the parametrize.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    scaffold_fixture(tmp_path, sandbox=choice)
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()

    # python3 + pip are installed via apt, and the gate toolchain via pip.
    assert (
        "apt-get install -y --no-install-recommends python3 python3-pip" in dockerfile
    )
    assert (
        "pip install --break-system-packages --no-cache-dir ruff black pytest"
        in dockerfile
    )
    # The installs are root operations, so they land before the image drops to
    # the non-root `node` user.
    assert dockerfile.index("pip install --break-system-packages") < dockerfile.index(
        "USER node"
    )


@pytest.mark.parametrize("choice", ["host", "docker"])
def test_python_dockerfile_keeps_the_shared_base_unchanged(
    tmp_path: Path, choice: str
) -> None:
    """The Python Dockerfile changes only the extension block; the base holds."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n")
    scaffold_fixture(tmp_path, sandbox=choice)
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()

    # Everything outside the extension block is intact.
    assert "FROM node:22-slim" in dockerfile
    assert "ca-certificates" in dockerfile
    assert "RUN npm install" in dockerfile
    assert "mkdir -p /home/node/.claude /home/node/.codex" in dockerfile
    assert "chown -R node:node /home/node/.claude /home/node/.codex" in dockerfile
    assert "USER node" in dockerfile


def test_non_python_project_dockerfile_is_byte_for_byte_unchanged(
    tmp_path: Path,
) -> None:
    """No pyproject.toml -> today's Dockerfile, unchanged byte for byte (#19)."""
    scaffold_fixture(tmp_path, sandbox="host")
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()
    assert dockerfile == _DOCKERFILE


def test_non_python_dockerfile_has_an_empty_extension_point(tmp_path: Path) -> None:
    """A non-Python Dockerfile never accidentally fills the extension point."""
    scaffold_fixture(tmp_path, sandbox="host")
    dockerfile = (tmp_path / ".pycastle" / "Dockerfile").read_text()
    # The example apt-get line in the empty block is commented out, so the real
    # (uncommented) install RUN lines must be absent.
    assert "pip install --break-system-packages" not in dockerfile
    assert (
        "apt-get install -y --no-install-recommends python3 python3-pip"
        not in dockerfile
    )


def test_python_and_non_python_dockerfiles_differ_only_in_the_extension_block(
    tmp_path: Path,
) -> None:
    """The two Dockerfiles differ only in the extension block (anti-drift)."""
    py_dir = tmp_path / "py_repo"
    nonpy_dir = tmp_path / "nonpy_repo"
    py_dir.mkdir()
    nonpy_dir.mkdir()
    (py_dir / "pyproject.toml").write_text("[project]\nname = 'demo'\n")

    scaffold_fixture(py_dir, sandbox="host")
    scaffold_fixture(nonpy_dir, sandbox="host")

    py = (py_dir / ".pycastle" / "Dockerfile").read_text()
    nonpy = (nonpy_dir / ".pycastle" / "Dockerfile").read_text()

    # They genuinely differ, but only in the extension block: swapping the
    # filled block back to the empty one recovers the non-Python Dockerfile.
    assert py != nonpy
    assert py.replace(_EXTENSION_PYTHON, _EXTENSION_EMPTY) == nonpy


def test_extension_block_constants_stay_in_sync() -> None:
    """The empty extension block is a unique, verbatim substring of _DOCKERFILE.

    The Python Dockerfile is built by string-replacing this block, so the
    constant must match the base template exactly and exactly once -- otherwise
    the two could silently fall out of sync.
    """
    assert _EXTENSION_EMPTY in _DOCKERFILE
    assert _DOCKERFILE.count(_EXTENSION_EMPTY) == 1


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
def test_scaffolded_gate_fails_when_no_tools_present(tmp_path: Path) -> None:
    """The default gate FAILS LOUD when zero gate tools are available (#28).

    A toolchain-less image would let every ``command -v`` guard skip, leaving the
    gate verifying nothing and exiting 0 -- a vacuous green that ships unverified
    code. The fail-if-zero rule turns that into a non-zero exit naming the missing
    tools and pointing at the Dockerfile extension point. We run with a PATH that
    has bash + coreutils but none of ruff/black/pytest.
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

    assert proc.returncode != 0
    # The failure names the missing tools and the Dockerfile extension point.
    assert "verified nothing" in proc.stderr
    assert "ruff" in proc.stderr
    assert ".pycastle/Dockerfile" in proc.stderr


def test_scaffolded_gate_passes_when_at_least_one_tool_present(
    tmp_path: Path,
) -> None:
    """A partially-equipped image still PASSES on the checks it can run (#28).

    Fail-if-zero is NOT fail-if-any-missing: with ruff present but black/pytest
    absent, the gate runs ruff (which passes here) and exits 0. We put a fake
    ``ruff`` (a no-op script) on a scrubbed PATH alongside bash/coreutils.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    gate = tmp_path / ".pycastle" / "gate"

    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake_ruff = bindir / "ruff"
    fake_ruff.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_ruff.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:/usr/bin:/bin"  # ruff present; black/pytest absent
    proc = subprocess.run(
        [str(gate)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    # black/pytest were skipped, but ruff ran, so the gate is non-vacuous.
    assert "skipping gate step" in proc.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_scaffolded_gate_check_tools_fails_fast_without_any_tool(
    tmp_path: Path,
) -> None:
    scaffold_fixture(tmp_path, sandbox="docker")
    gate = tmp_path / ".pycastle" / "gate"
    env = dict(os.environ, PATH="/usr/bin:/bin")

    proc = subprocess.run(
        [str(gate), "--check-tools"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode != 0
    assert "ruff" in proc.stderr
    assert "black" in proc.stderr
    assert "pytest" in proc.stderr


def test_scaffolded_gate_check_tools_only_looks_up_tools(tmp_path: Path) -> None:
    scaffold_fixture(tmp_path, sandbox="docker")
    gate = tmp_path / ".pycastle" / "gate"
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fake_ruff = bindir / "ruff"
    fake_ruff.write_text("#!/usr/bin/env bash\nexit 99\n")
    fake_ruff.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin")

    proc = subprocess.run(
        [str(gate), "--check-tools"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    # The generic gate's existing policy is non-vacuousness: one available
    # configured tool is enough. Exit 99 would prove ruff was actually run.
    assert proc.returncode == 0, proc.stderr


def test_repository_gate_check_tools_requires_every_unconditional_tool(
    tmp_path: Path,
) -> None:
    repo_gate = Path(__file__).resolve().parents[1] / ".pycastle" / "gate"
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    for name in ("ruff", "pytest"):
        tool = bindir / name
        tool.write_text("#!/usr/bin/env bash\nexit 99\n")
        tool.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:/usr/bin:/bin")

    proc = subprocess.run(
        [str(repo_gate), "--check-tools"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode != 0
    assert "black" in proc.stderr
    assert "ruff" not in proc.stderr
    assert "pytest" not in proc.stderr


def test_repository_gate_check_tools_ignores_workspace_virtualenv(
    tmp_path: Path,
) -> None:
    """A host .venv must not masquerade as toolchain installed in the image."""
    repo_gate = Path(__file__).resolve().parents[1] / ".pycastle" / "gate"
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("ruff", "black", "pytest"):
        tool = venv_bin / name
        tool.write_text("#!/usr/bin/env bash\nexit 0\n")
        tool.chmod(0o755)
    env = dict(os.environ, PATH="/usr/bin:/bin")

    proc = subprocess.run(
        [str(repo_gate), "--check-tools"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode != 0
    assert "Missing gate tools: ruff black pytest" in proc.stderr


def test_scaffolded_gate_text_has_counter_and_fail_branch(tmp_path: Path) -> None:
    """The gate template carries the fail-if-zero counter and exit branch (#28).

    A lightweight guard against template regressions without spawning bash: the
    scaffolded gate must count steps that ran and fail loud when none did.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    text = (tmp_path / ".pycastle" / "gate").read_text()
    assert "ran=0" in text
    assert "ran=$((ran + 1))" in text
    assert 'if [ "$ran" -eq 0 ]; then' in text
    assert "verified nothing" in text
    assert "exit 1" in text


def test_scaffolded_gate_fail_message_names_both_sandboxes(tmp_path: Path) -> None:
    """The fail-loud remediation covers host (PATH/venv) and docker (#58).

    The gate can't tell whether it runs on the host or in the Docker sandbox, so a
    Docker-only remediation misleads host users (whose fix is to put the toolchain
    on PATH / activate the venv). The message must name both contexts while keeping
    the 'verified nothing' line and the Dockerfile extension point intact.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    text = (tmp_path / ".pycastle" / "gate").read_text()

    # Unchanged fail-loud markers (depended on by the negative-gate check).
    assert "verified nothing" in text
    # Host remediation: install on PATH / activate the project venv.
    assert "PATH" in text
    assert "venv" in text
    # Docker remediation still points at the Dockerfile extension point.
    assert ".pycastle/Dockerfile" in text
    assert "PROJECT EXTENSION POINT" in text


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
