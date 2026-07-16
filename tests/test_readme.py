"""Regression tests for the user-facing README onboarding path."""

import re
from pathlib import Path

from pycastle import __version__

README = Path(__file__).parents[1] / "README.md"


def test_readme_onboards_a_user_without_source_diving() -> None:
    readme = README.read_text()

    assert "coming soon" not in readme.lower()
    assert "uv pip install git+https://github.com/krishnakartik1/pycastle" in readme
    assert "uv pip install pycastle" in readme

    for command in (
        "pycastle init",
        "pycastle sandbox setup --runtime claude",
        "pycastle run --runtime claude",
        "pycastle prune",
    ):
        assert command in readme

    for fixture_entry in (
        ".pycastle/main.py",
        ".pycastle/prompts/",
        ".pycastle/gate",
        ".pycastle/Dockerfile",
        ".pycastle/sandbox",
    ):
        assert fixture_entry in readme

    assert "ready-for-agent" in readme
    assert "gh issue edit" in readme
    assert "pycastle init --sandbox host" in readme
    assert "pycastle init --sandbox docker" in readme
    assert "end-of-file" in readme.lower()


def test_readme_documents_codex_host_black_workaround() -> None:
    readme = README.read_text()
    prose = " ".join(readme.split())

    assert "Codex Runtime with the host Sandbox" in prose
    assert "black --check ." in readme
    assert "--sandbox docker" in readme
    assert "one file per Black process" in prose
    assert "xargs -0 -r -n 1 black --check --" in readme
    assert "--workers 1" in readme
    assert "does not avoid the hang" in prose
    assert "Gate remains authoritative" in prose


def test_readme_installs_runner_and_canonical_skill_from_one_release() -> None:
    readme = README.read_text()
    expected_tag = f"v{__version__}"

    tagged_urls = re.findall(
        r"https://github\.com/krishnakartik1/pycastle@(?P<tag>v[^\s\"']+)",
        readme,
    )
    skill_tags = re.findall(r"git clone[^\n]+--branch (?P<tag>v\S+)", readme)
    assert tagged_urls
    assert skill_tags
    assert set(tagged_urls) == {expected_tag}
    assert set(skill_tags) == {expected_tag}
    assert "skills/pycastle/" in readme
    assert "~/.codex/skills/pycastle" in readme
    assert "~/.claude/skills/pycastle" in readme
    assert "same Git tag" in readme
    assert "exactly match `pycastle --version`" in readme
