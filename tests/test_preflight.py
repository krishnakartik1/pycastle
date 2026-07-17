"""Host command inventory fails fast before readiness."""

import pytest

from pycastle import preflight
from pycastle.preflight import PreflightError, check_required_commands


def test_passes_when_all_commands_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "command_exists", lambda _name: True)
    check_required_commands(["git", "gh"])


def test_raises_and_names_missing_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "command_exists", lambda name: name != "gh")
    with pytest.raises(PreflightError) as excinfo:
        check_required_commands(["git", "gh"])
    assert "gh" in str(excinfo.value)
    assert "git" not in str(excinfo.value)
