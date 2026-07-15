"""Contract tests for the vendor-neutral PyCastle lifecycle skill."""

import re
from pathlib import Path

from packaging.version import Version

from pycastle import __version__

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "skills" / "pycastle" / "SKILL.md"


def _skill_text() -> str:
    return SKILL.read_text()


def _frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(?P<body>.*?)\n---\n", text, re.DOTALL)
    assert match is not None
    entries = {}
    for line in match.group("body").splitlines():
        key, separator, value = line.partition(":")
        assert separator and key and value.strip()
        entries[key] = value.strip()
    return entries


def test_one_portable_pycastle_skill_has_valid_frontmatter() -> None:
    skill_files = list((ROOT / "skills").glob("**/SKILL.md"))

    assert skill_files == [SKILL]
    frontmatter = _frontmatter(_skill_text())
    assert set(frontmatter) == {"name", "description"}
    assert frontmatter["name"] == "pycastle"
    for trigger in ("onboard", "readiness", "run", "pull request"):
        assert trigger in frontmatter["description"].lower()


def test_skill_release_matches_the_cli_release() -> None:
    text = _skill_text()
    match = re.search(r"^PyCastle release: `([^`]+)`$", text, re.MULTILINE)

    assert match is not None
    assert str(Version(match.group(1))) == match.group(1)
    assert match.group(1) == __version__
    assert f"v{__version__}" in text


def test_skill_selects_runtime_and_pins_lifecycle_commands() -> None:
    text = _skill_text()

    for instruction in (
        "Codex host -> `codex` Runtime",
        "Claude Code host -> `claude` Runtime",
        "unknown or ambiguous host",
        "ask the user to choose",
        "pycastle --version",
        "pycastle init --sandbox docker",
        "pycastle doctor --json --sandbox docker --runtime <runtime> --iterations 5",
        "pycastle run --sandbox docker --runtime <runtime> --iterations 5",
    ):
        assert instruction in text


def test_skill_preserves_runner_and_user_owned_boundaries() -> None:
    text = _skill_text()
    lowered = text.lower()

    for stop_condition in (
        "version mismatch",
        "failed or blocked",
        "active run",
        "explicit user authorization",
    ):
        assert stop_condition in lowered

    for boundary in (
        "Do not inspect `.pycastle/version`",
        "Do not triage, rewrite, promote, or relabel untriaged Items",
        "Do not duplicate an active Run",
        "Do not merge without explicit user authorization",
        "Do not copy diagnosis or pull-request review/remediation policy",
    ):
        assert boundary in text

    assert "schema-v1" in text
    assert "Run re-evaluates readiness" in text
    assert "ready-for-agent" in text
    assert "fewer than five" in lowered


def test_skill_delegates_long_runs_diagnosis_and_pr_follow_up() -> None:
    text = _skill_text()

    assert "one subagent" in text
    assert "when the host supports subagents" in text
    assert "foreground" in text
    assert "diagnosis capability" in text
    assert "pull-request capability" in text
    assert "Run report" in text
