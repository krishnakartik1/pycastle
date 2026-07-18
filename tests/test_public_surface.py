"""Regression tests for PyCastle's public positioning and Sandbox guidance."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).parents[1]
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"

PUBLIC_DESCRIPTION = (
    "PyCastle runs autonomous development graphs that turn ready GitHub issues into "
    "tested pull requests. Projects own their Execution graphs, Setup, and Gate; "
    "PyCastle owns the runner."
)


def test_public_surface_leads_with_autonomous_development_graphs() -> None:
    readme = README.read_text()
    intro = " ".join(readme.split("\n\n", 2)[1].split())
    metadata = tomllib.loads(PYPROJECT.read_text())

    assert intro == PUBLIC_DESCRIPTION
    assert metadata["project"]["description"] == PUBLIC_DESCRIPTION


def test_readme_recommends_docker_and_discloses_host_authority() -> None:
    readme = README.read_text()
    prose = " ".join(readme.split())

    assert readme.index("## Choose a Sandbox") < readme.index("## Requirements")
    assert (
        "The Docker Sandbox is the recommended isolation boundary for project "
        "execution" in prose
    )
    assert "running directly on the operator's machine" in prose
    assert (
        "Claude host execution currently inherits the operator's ambient Claude Code "
        "permission mode" in prose
    )
    assert "https://github.com/krishnakartik1/pycastle/issues/62" in readme
