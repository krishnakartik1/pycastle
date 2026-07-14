"""Guard that the repo's committed ``.pycastle/`` stays in sync with the scaffolder.

The repo carries its own ``.pycastle/`` fixture and ``pycastle init`` scaffolds
that same tree into fresh repos via :func:`pycastle.scaffold.scaffold_fixture`.
If the two drift, a scaffolded repo no longer gets the loop that runs here. This
test pins them together: it scaffolds into a ``tmp_path`` using the repo's own
recorded sandbox choice and compares against the committed fixture.

The comparison treats two kinds of file differently:

* The non-customizable files (``sandbox``, ``.gitignore``, ``main.py``) must be
  **byte-identical** -- a drift there is exactly the staleness this guard exists
  to catch.
* The ``gate``, the ``prompts/*.md``, and the ``Dockerfile`` are **project-owned
  customizations** (a repo edits its gate for its own stack, tunes its prompts,
  and extends the Dockerfile's PROJECT EXTENSION POINT with its own toolchain --
  the docker sandbox runs the gate inside that image, see #79), so the guard only
  requires they are *present*, not byte-equal. This matches issue #26's "modulo
  the project's own gate/prompt customizations".

Both trees are compared by *shape* (the set of relative paths) ignoring any
``__pycache__``/``.pyc`` build droppings, so a stray compiled ``main.py`` does not
fail the guard.

The repo's own ``.pycastle/`` also doubles as the live working directory for the
self-hosting loop, so a run drops transient artifacts into it (a ``handoff.md``
from a retried implement attempt, a phase's ``plan.md``). Those are untracked run
output, not part of the committed fixture, so the committed side is taken from
what git *tracks* (:func:`_committed_tree`) rather than from a raw filesystem
walk -- which is what "committed fixture" means and keeps the guard immune to
whatever the running loop leaves behind.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pycastle.scaffold import (
    FIXTURE_DIRNAME,
    SANDBOX_MARKER,
    read_sandbox,
    scaffold_fixture,
)

#: Files the committed fixture may legitimately customize away from the
#: scaffolder, so the guard only checks they exist rather than matching bytes.
_EXEMPT_FROM_BYTES = {
    "gate",
    "prompts/plan.md",
    "prompts/implement.md",
    "prompts/review.md",
    "Dockerfile",
}


def _repo_root() -> Path:
    """Return the repo root by walking up from this test file to ``.pycastle/``."""
    for parent in Path(__file__).resolve().parents:
        if (parent / FIXTURE_DIRNAME).is_dir():
            return parent
    raise AssertionError(f"Could not find a {FIXTURE_DIRNAME}/ above {__file__}")


def _tree(fixture_dir: Path) -> set[str]:
    """Return fixture-relative posix paths under ``fixture_dir``, ignoring caches."""
    return {
        path.relative_to(fixture_dir).as_posix()
        for path in fixture_dir.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _committed_tree(fixture_dir: Path) -> set[str]:
    """Return the git-*tracked* fixture-relative paths under ``fixture_dir``.

    Unlike :func:`_tree`, this ignores untracked run output the self-hosting loop
    drops into its own ``.pycastle/`` (a retried attempt's ``handoff.md``, a
    phase's ``plan.md``), so the shape guard compares the *committed* fixture --
    what a scaffold reproduces -- rather than a working tree littered with a live
    run's artifacts. ``git ls-files`` run inside ``fixture_dir`` emits paths
    relative to it; caches are filtered to match :func:`_tree`.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "."],
        cwd=fixture_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        path
        for path in result.stdout.split("\0")
        if path and "__pycache__" not in Path(path).parts and not path.endswith(".pyc")
    }


def test_committed_fixture_matches_scaffolder(tmp_path: Path) -> None:
    """The committed ``.pycastle/`` equals a fresh scaffold of its own choice."""
    committed = _repo_root() / FIXTURE_DIRNAME

    # Scaffold with the repo's *own* recorded sandbox choice so the marker (the
    # one file that differs between host/docker) is compared like for like.
    recorded = read_sandbox(committed)
    assert recorded in ("host", "docker"), (
        f"{committed / SANDBOX_MARKER} must record 'host' or 'docker', "
        f"got {recorded!r}"
    )
    scaffold_fixture(tmp_path, sandbox=recorded)
    scaffolded = tmp_path / FIXTURE_DIRNAME

    # Same shape: the committed tree carries exactly the files init scaffolds.
    # The committed side is taken from git tracking, not a filesystem walk, so a
    # live self-hosting run's untracked artifacts in .pycastle/ do not count.
    committed_tree, scaffolded_tree = _committed_tree(committed), _tree(scaffolded)
    missing = scaffolded_tree - committed_tree
    extra = committed_tree - scaffolded_tree
    assert committed_tree == scaffolded_tree, (
        f"committed {FIXTURE_DIRNAME}/ shape has drifted from the scaffolder: "
        f"missing {sorted(missing)}, unexpected {sorted(extra)}"
    )

    # Byte-identical for everything except the project's own gate/prompts.
    for relative in scaffolded_tree - _EXEMPT_FROM_BYTES:
        assert (committed / relative).read_bytes() == (
            scaffolded / relative
        ).read_bytes(), f"{relative} has drifted from the scaffolder output"

    # The exempt files must still be present (a repo customizes, never drops them).
    for relative in _EXEMPT_FROM_BYTES:
        assert (committed / relative).is_file(), f"{relative} is missing"


def test_exempt_files_are_real_fixture_paths(tmp_path: Path) -> None:
    """Every exempt path is a path the scaffolder actually writes.

    The byte-equality skip list (:data:`_EXEMPT_FROM_BYTES`) is maintained by
    hand. If the scaffolder ever renamed or dropped one of those files, a stale
    entry here would silently exempt nothing -- and a real drift in the renamed
    file would slip through. Pinning the set to the scaffolded shape keeps the
    skip list honest.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    scaffolded = _tree(tmp_path / FIXTURE_DIRNAME)
    stale = _EXEMPT_FROM_BYTES - scaffolded
    assert not stale, (
        f"_EXEMPT_FROM_BYTES names paths the scaffolder no longer writes: "
        f"{sorted(stale)}"
    )


def test_guard_catches_byte_drift(tmp_path: Path) -> None:
    """The byte comparison is not vacuous: it fails when a fixture file drifts.

    The in-sync guard above passes only while the committed tree matches the
    scaffolder. A future refactor of ``_tree`` or the comparison could make that
    pass *trivially* (e.g. an empty tree compares equal to itself). This pins the
    guard's teeth: two fresh scaffolds are byte-identical, and perturbing one
    non-exempt file makes the byte comparison the guard relies on fail.
    """
    scaffold_fixture(tmp_path / "pristine", sandbox="host")
    scaffold_fixture(tmp_path / "drifted", sandbox="host")
    pristine = tmp_path / "pristine" / FIXTURE_DIRNAME
    drifted = tmp_path / "drifted" / FIXTURE_DIRNAME

    # Pick a non-exempt file and confirm a fresh scaffold matches byte for byte.
    target = "main.py"
    assert target not in _EXEMPT_FROM_BYTES
    assert (pristine / target).read_bytes() == (drifted / target).read_bytes()

    # Now drift it and confirm the comparison the guard uses would flag it.
    (drifted / target).write_text(
        (drifted / target).read_text() + "\n# unexpected drift\n"
    )
    assert (pristine / target).read_bytes() != (drifted / target).read_bytes()


def test_guard_catches_shape_drift(tmp_path: Path) -> None:
    """The shape comparison is not vacuous: dropping a file changes the tree set."""
    scaffold_fixture(tmp_path / "pristine", sandbox="host")
    scaffold_fixture(tmp_path / "drifted", sandbox="host")
    pristine = tmp_path / "pristine" / FIXTURE_DIRNAME
    drifted = tmp_path / "drifted" / FIXTURE_DIRNAME

    assert _tree(pristine) == _tree(drifted)
    (drifted / "main.py").unlink()
    assert _tree(pristine) != _tree(drifted)


def test_committed_tree_ignores_untracked_run_artifacts(tmp_path: Path) -> None:
    """The committed tree omits untracked run output the self-hosting loop drops.

    The repo's own ``.pycastle/`` is the live working directory for the loop that
    runs here, so a retried attempt leaves a ``handoff.md`` and a phase leaves a
    ``plan.md`` inside it -- untracked artifacts, not committed fixture files. The
    shape guard must ignore them, so this scaffolds a fixture into a throwaway git
    repo, commits it, then drops those exact stray files and confirms
    :func:`_committed_tree` still reports only the committed shape (while the raw
    :func:`_tree` walk does see the strays -- proving the distinction is real).
    """
    scaffold_fixture(tmp_path, sandbox="host")
    fixture = tmp_path / FIXTURE_DIRNAME
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for command in (["git", "init", "-q"], ["git", "add", "-A"]):
        subprocess.run(command, cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-qm", "scaffold"], cwd=tmp_path, check=True, env=env
    )

    committed = _committed_tree(fixture)
    (fixture / "handoff.md").write_text("stray handoff\n")
    (fixture / "plan.md").write_text("stray plan\n")

    assert (
        _committed_tree(fixture) == committed
    ), "untracked strays leaked into the tree"
    assert {"handoff.md", "plan.md"} <= _tree(fixture), "strays should be on disk"


def test_scaffolder_gate_is_executable(tmp_path: Path) -> None:
    """The scaffolder writes an executable ``gate``.

    ``pycastle run`` invokes the gate directly, so a non-executable gate would
    fail to launch. The byte/shape comparisons above ignore file mode (the gate
    is exempt from byte-equality, and ``_tree`` only collects paths), so the gate
    being runnable is pinned here rather than left to the in-sync guard.
    """
    scaffold_fixture(tmp_path, sandbox="host")
    gate = tmp_path / FIXTURE_DIRNAME / "gate"
    assert os.access(gate, os.X_OK), "scaffolded gate is not executable"


def test_committed_gate_is_executable() -> None:
    """The committed fixture's ``gate`` carries the executable bit.

    The in-sync guard treats the gate as a project-owned customization and only
    checks it exists, never its mode. But a committed gate that lost its
    executable bit would ship a fixture ``pycastle run`` cannot launch, so the
    one thing that makes the gate runnable is pinned directly on the committed
    file.
    """
    gate = _repo_root() / FIXTURE_DIRNAME / "gate"
    assert gate.is_file(), f"{gate} is missing"
    assert os.access(gate, os.X_OK), f"{gate} is not executable"
