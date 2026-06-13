"""The real Claude adapter parses stream-json and surfaces crashes."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle import orchestrator
from pycastle.models import IssueRef
from pycastle.runtime import (
    AgentCrashError,
    ClaudeRuntime,
    Runtime,
    make_runtime,
)


def _stream(events: list[dict]) -> io.StringIO:
    """Render captured events as the JSONL the claude CLI emits."""
    return io.StringIO("".join(json.dumps(event) + "\n" for event in events))


def _fake_proc(events: list[dict], *, returncode: int = 0) -> MagicMock:
    """Build a fake Popen whose stdout replays captured stream-json events."""
    proc = MagicMock()
    proc.stdout = _stream(events)
    proc.stderr = io.StringIO("")
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


# A realistic stream-json transcript: one assistant text turn plus the final
# result event carrying cost, duration, turns, and token usage.
_SUCCESS_EVENTS = [
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "planning the change"},
                {"type": "text", "text": "Implemented the feature."},
            ]
        },
    },
    {
        "type": "result",
        "result": "done",
        "total_cost_usd": 0.1234,
        "duration_ms": 4567,
        "num_turns": 3,
        "is_error": False,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 250,
            "cache_creation_input_tokens": 80,
            "cache_read_input_tokens": 640,
        },
    },
]


def test_claude_runtime_satisfies_runtime_protocol() -> None:
    assert isinstance(ClaudeRuntime(), Runtime)


def test_make_runtime_selects_claude() -> None:
    runtime = make_runtime("claude")
    assert isinstance(runtime, ClaudeRuntime)
    assert runtime.name == "claude"


def test_build_command_is_correct() -> None:
    runtime = ClaudeRuntime(model="opus", max_turns=5)

    assert runtime.build_command("do the work") == [
        "claude",
        "-p",
        "do the work",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "5",
        "--model",
        "opus",
    ]


def test_build_command_minimal_omits_optional_flags() -> None:
    assert ClaudeRuntime().build_command("hi") == [
        "claude",
        "-p",
        "hi",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def test_build_command_appends_skip_permissions() -> None:
    runtime = ClaudeRuntime(dangerously_skip_permissions=True)
    assert runtime.build_command("hi")[-1] == "--dangerously-skip-permissions"


@pytest.fixture
def mock_popen(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch subprocess.Popen in the runtime module and return the mock."""
    popen = MagicMock()
    monkeypatch.setattr("pycastle.runtime.subprocess.Popen", popen)
    return popen


def test_parses_stream_json_into_output_and_telemetry(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    result = ClaudeRuntime().run("a prompt", cwd=tmp_path, phase="implement")

    assert result.output == "Implemented the feature."

    telemetry = result.telemetry
    assert telemetry.runtime == "claude"
    assert telemetry.phase == "implement"
    assert telemetry.cost_usd == 0.1234
    assert telemetry.duration_ms == 4567
    assert telemetry.num_turns == 3
    assert telemetry.is_error is False

    assert telemetry.usage is not None
    assert telemetry.usage.input_tokens == 1000
    assert telemetry.usage.output_tokens == 250
    assert telemetry.usage.cache_creation_input_tokens == 80
    assert telemetry.usage.cache_read_input_tokens == 640


def test_run_invokes_correct_argv(mock_popen: MagicMock, tmp_path: Path) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    ClaudeRuntime().run("a prompt", cwd=tmp_path, phase="implement")

    assert mock_popen.call_args.args[0] == [
        "claude",
        "-p",
        "a prompt",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert mock_popen.call_args.kwargs["cwd"] == tmp_path


def test_falls_back_to_result_text_without_assistant_text(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    events = [
        {
            "type": "result",
            "result": "summary only",
            "total_cost_usd": 0.01,
            "duration_ms": 10,
            "num_turns": 1,
            "is_error": False,
        }
    ]
    mock_popen.return_value = _fake_proc(events)

    result = ClaudeRuntime().run("p", cwd=tmp_path, phase="plan")

    assert result.output == "summary only"
    assert result.telemetry.usage is None


def test_ignores_unparseable_lines(mock_popen: MagicMock, tmp_path: Path) -> None:
    proc = MagicMock()
    proc.stdout = io.StringIO(
        "not json\n"
        + json.dumps(_SUCCESS_EVENTS[0])
        + "\n\n"
        + json.dumps(_SUCCESS_EVENTS[1])
        + "\n"
    )
    proc.stderr = io.StringIO("")
    proc.returncode = 0
    proc.wait.return_value = 0
    mock_popen.return_value = proc

    result = ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    assert result.output == "Implemented the feature."
    assert result.telemetry.num_turns == 3


def test_result_event_is_error_raises_crash(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    events = [
        {
            "type": "result",
            "result": "",
            "is_error": True,
            "num_turns": 1,
        }
    ]
    mock_popen.return_value = _fake_proc(events, returncode=0)

    with pytest.raises(AgentCrashError) as exc_info:
        ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    # A result event flagged is_error is a crash even on a zero process exit.
    assert exc_info.value.phase == "implement"
    assert exc_info.value.exit_code == 0


def test_nonzero_exit_raises_crash(mock_popen: MagicMock, tmp_path: Path) -> None:
    proc = _fake_proc([], returncode=1)
    proc.stderr = io.StringIO("boom")
    mock_popen.return_value = proc

    with pytest.raises(AgentCrashError) as exc_info:
        ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    # The crash carries the failing phase and the process exit code so a caller
    # can branch on them without parsing the message string.
    assert exc_info.value.phase == "implement"
    assert exc_info.value.exit_code == 1


def test_nonzero_exit_reads_stderr_for_logging(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # stderr is read after the stdout loop drains; confirm it still surfaces in
    # the crash log even though stdout closed first.
    proc = _fake_proc([], returncode=2)
    proc.stderr = io.StringIO("permission denied\n")
    mock_popen.return_value = proc

    with caplog.at_level("ERROR", logger="pycastle.runtime"):
        with pytest.raises(AgentCrashError):
            ClaudeRuntime().run("p", cwd=tmp_path, phase="review")

    assert "permission denied" in caplog.text
    proc.wait.assert_called_once()


def test_empty_stream_with_clean_exit_does_not_crash(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # No events at all and a zero exit: telemetry degrades to a bare record and
    # the output is empty rather than raising.
    mock_popen.return_value = _fake_proc([], returncode=0)

    result = ClaudeRuntime().run("p", cwd=tmp_path, phase="plan")

    assert result.output == ""
    assert result.telemetry.runtime == "claude"
    assert result.telemetry.phase == "plan"
    assert result.telemetry.cost_usd is None
    assert result.telemetry.num_turns is None
    assert result.telemetry.usage is None
    assert result.telemetry.is_error is False


def test_assistant_text_without_result_event_degrades_gracefully(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Assistant text streamed but the run ended without a final result event:
    # keep the text, return bare telemetry, do not crash.
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "partial work"}]},
        }
    ]
    mock_popen.return_value = _fake_proc(events, returncode=0)

    result = ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    assert result.output == "partial work"
    assert result.telemetry.cost_usd is None
    assert result.telemetry.duration_ms is None
    assert result.telemetry.num_turns is None
    assert result.telemetry.usage is None


def test_multiple_assistant_events_concatenate_in_order(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Text blocks from several assistant events are joined in stream order,
    # including multiple text blocks within a single event.
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "first "}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "second "},
                    {"type": "text", "text": "third "},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "fourth"}]},
        },
        {"type": "result", "result": "ignored", "is_error": False, "num_turns": 2},
    ]
    mock_popen.return_value = _fake_proc(events)

    result = ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    assert result.output == "first second third fourth"


@pytest.mark.parametrize("usage_value", [None, "not-a-dict", 42, ["a", "b"]])
def test_result_event_with_non_dict_usage_does_not_throw(
    mock_popen: MagicMock, tmp_path: Path, usage_value: object
) -> None:
    # A result event whose usage is missing, null, or not a mapping must parse
    # into telemetry with usage=None rather than raising.
    events = [
        {
            "type": "result",
            "result": "done",
            "total_cost_usd": 0.02,
            "duration_ms": 20,
            "num_turns": 1,
            "is_error": False,
            "usage": usage_value,
        }
    ]
    mock_popen.return_value = _fake_proc(events)

    result = ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    assert result.telemetry.usage is None
    assert result.telemetry.cost_usd == 0.02
    assert result.telemetry.num_turns == 1


def test_docker_runtime_wraps_inner_argv_into_docker_run(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    """A docker-sandboxed Claude run invokes ``docker run``, not bare claude.

    The inner ``claude …`` argv is unchanged; the sandbox wraps it. The same
    stream-json parsing applies to the container's stdout.
    """
    from pycastle import sandbox

    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = ClaudeRuntime.in_docker(workspace=tmp_path)
    result = runtime.run("a prompt", cwd=tmp_path, phase="implement")

    argv = mock_popen.call_args.args[0]
    # The whole command is a docker invocation wrapping the claude argv.
    assert argv[:3] == ["docker", "run", "--rm"]
    assert sandbox.DEFAULT_IMAGE in argv
    image_idx = argv.index(sandbox.DEFAULT_IMAGE)
    assert argv[image_idx + 1 : image_idx + 4] == ["claude", "-p", "a prompt"]
    assert "pycastle-claude-auth:/home/node/.claude" in argv
    assert "CLAUDE_CONFIG_DIR=/home/node/.claude" in argv
    # Same stream-json parsing as the host path.
    assert result.output == "Implemented the feature."
    assert result.telemetry.num_turns == 3


def test_docker_runtime_runs_both_runtime_and_commands_in_container(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Every phase the Runtime drives is wrapped: there is no host-side claude.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = ClaudeRuntime.in_docker(workspace=tmp_path)
    runtime.run("p", cwd=tmp_path, phase="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[0] == "docker"
    # The non-docker host claude binary never appears as the process argv[0].
    assert argv[0] != "claude"


def test_host_runtime_still_invokes_bare_claude(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Without a sandbox wrapper the runtime runs claude directly on the host.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    ClaudeRuntime().run("p", cwd=tmp_path, phase="implement")

    assert mock_popen.call_args.args[0][0] == "claude"


def test_run_works_one_issue_end_to_end_via_claude(
    mock_popen: MagicMock, fixture_dir: Path, tmp_path: Path
) -> None:
    """The orchestrator completes an issue using the real Claude adapter.

    The agent is mocked at the subprocess boundary, and every git/gh call is
    mocked through the orchestrator's runner — no real agent runs.
    """
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    def _ok(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

    issue = IssueRef(number=7, title="Wire claude", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = MagicMock(side_effect=_ok)

    outcome = orchestrator.run(
        runtime=make_runtime("claude"),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        workspace=tmp_path,
        runner=runner,
    )

    assert outcome.issue is not None and outcome.issue.number == 7
    assert outcome.pr_opened is True
    # The real adapter ran through the graph: the claude CLI was invoked.
    assert mock_popen.call_args.args[0][0] == "claude"
