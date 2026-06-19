"""Guard that the repo's committed ``.pycastle/`` stays in sync with the scaffolder.

The repo carries its own ``.pycastle/`` fixture and ``pycastle init`` scaffolds
that same tree into fresh repos via :func:`pycastle.scaffold.scaffold_fixture`.
If the two drift, a scaffolded repo no longer gets the loop that runs here. This
test pins them together: it scaffolds into a ``tmp_path`` using the repo's own
recorded sandbox choice and compares against the committed fixture.

The comparison treats two kinds of file differently:

* The non-customizable files (``sandbox``, ``Dockerfile``, ``.gitignore``,
  ``main.py``) must be **byte-identical** -- a drift there is exactly the staleness
  this guard exists to catch.
* The ``gate`` and the ``prompts/*.md`` are **project-owned customizations** (a
  repo edits its gate for its own stack and tunes its prompts), so the guard only
  requires they are *present*, not byte-equal. This matches issue #26's "modulo
  the project's own gate/prompt customizations".

Both trees are compared by *shape* (the set of relative paths) ignoring any
``__pycache__``/``.pyc`` build droppings, so a stray compiled ``main.py`` does not
fail the guard.
"""

from __future__ import annotations

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
    assert _tree(committed) == _tree(scaffolded)

    # Byte-identical for everything except the project's own gate/prompts.
    for relative in _tree(scaffolded) - _EXEMPT_FROM_BYTES:
        assert (committed / relative).read_bytes() == (
            scaffolded / relative
        ).read_bytes(), f"{relative} has drifted from the scaffolder output"

    # The exempt files must still be present (a repo customizes, never drops them).
    for relative in _EXEMPT_FROM_BYTES:
        assert (committed / relative).is_file(), f"{relative} is missing"
