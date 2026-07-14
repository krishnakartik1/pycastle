"""Regression tests for the user-facing README onboarding path."""

from pathlib import Path

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
