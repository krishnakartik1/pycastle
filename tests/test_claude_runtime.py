"""The real Claude adapter parses stream-json and surfaces crashes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


def test_host_build_command_does_not_skip_permissions() -> None:
    # The host run must never auto-skip permissions: the user's machine is not
    # the isolation boundary, so the in-agent permission prompts stay in force.
    assert "--dangerously-skip-permissions" not in ClaudeRuntime().build_command("hi")


def test_in_docker_build_command_skips_permissions(tmp_path: Path) -> None:
    # The Docker container is the isolation boundary (ADR-0003), so the inner
    # claude argv skips permissions there; otherwise headless claude denies
    # every Write and each node silently no-ops (issue #44). This mirrors
    # CodexRuntime.in_docker carrying CODEX_DOCKER_BYPASS.
    runtime = ClaudeRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    assert "--dangerously-skip-permissions" in runtime.build_command("hi")


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

    result = ClaudeRuntime().run("a prompt", cwd=tmp_path, node="implement")

    assert result.output == "Implemented the feature."

    telemetry = result.telemetry
    assert telemetry.runtime == "claude"
    assert telemetry.node == "implement"
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

    ClaudeRuntime().run("a prompt", cwd=tmp_path, node="implement")

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

    result = ClaudeRuntime().run("p", cwd=tmp_path, node="plan")

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

    result = ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

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
        ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

    # A result event flagged is_error is a crash even on a zero process exit.
    assert exc_info.value.node == "implement"
    assert exc_info.value.exit_code == 0


def test_nonzero_exit_raises_crash(mock_popen: MagicMock, tmp_path: Path) -> None:
    proc = _fake_proc(
        [
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "partial answer"}]},
            }
        ],
        returncode=1,
    )
    proc.stderr = io.StringIO("boom")
    mock_popen.return_value = proc

    with pytest.raises(AgentCrashError) as exc_info:
        ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

    # The crash carries the failing node and the process exit code so a caller
    # can branch on them without parsing the message string.
    assert exc_info.value.node == "implement"
    assert exc_info.value.exit_code == 1
    assert exc_info.value.transcript == "partial answer"
    assert exc_info.value.telemetry is not None
    assert exc_info.value.telemetry.runtime == "claude"


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
            ClaudeRuntime().run("p", cwd=tmp_path, node="review")

    assert "permission denied" in caplog.text
    proc.wait.assert_called_once()


def test_empty_stream_with_clean_exit_does_not_crash(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # No events at all and a zero exit: telemetry degrades to a bare record and
    # the output is empty rather than raising.
    mock_popen.return_value = _fake_proc([], returncode=0)

    result = ClaudeRuntime().run("p", cwd=tmp_path, node="plan")

    assert result.output == ""
    assert result.telemetry.runtime == "claude"
    assert result.telemetry.node == "plan"
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

    result = ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

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

    result = ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

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

    result = ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

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

    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = ClaudeRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    result = runtime.run("a prompt", cwd=tmp_path, node="implement")

    argv = mock_popen.call_args.args[0]
    # The whole command is a docker invocation wrapping the claude argv.
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "sha256:" + ("a" * 64) in argv
    image_idx = argv.index("sha256:" + ("a" * 64))
    assert argv[image_idx + 1 : image_idx + 4] == ["claude", "-p", "a prompt"]
    assert "pycastle-claude-auth:/pycastle/auth" in argv
    assert "CLAUDE_CONFIG_DIR=/pycastle/auth" in argv
    # Same stream-json parsing as the host path.
    assert result.output == "Implemented the feature."
    assert result.telemetry.num_turns == 3


def test_docker_runtime_runs_both_runtime_and_commands_in_container(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Every node the Runtime drives is wrapped: there is no host-side claude.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = ClaudeRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    runtime.run("p", cwd=tmp_path, node="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[0] == "docker"
    # The non-docker host claude binary never appears as the process argv[0].
    assert argv[0] != "claude"


def test_in_docker_resolves_relative_workspace(
    mock_popen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # in_docker forwards its workspace to the sandbox builder, which resolves a
    # relative path to absolute. A relative workspace must not produce a broken
    # relative bind-mount source in the launched docker argv.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "repo").mkdir()
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = ClaudeRuntime.in_docker(
        image="sha256:" + ("a" * 64), workspace=Path("repo")
    )
    runtime.run("p", cwd=tmp_path / "repo", node="implement")

    argv = mock_popen.call_args.args[0]
    # The relative workspace is resolved for both the bind mount and workdir.
    repo = str((tmp_path / "repo").resolve())
    assert f"{repo}:{repo}" in argv


def test_docker_workdir_is_run_cwd_not_workspace(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Regression for #50: under docker the container -w must be the per-issue
    # worktree (the run cwd), not the workspace root, so claude writes into the
    # worktree the orchestrator commits. The bind-mount source stays the root so
    # the worktree's .git file resolves to the parent repo inside the container.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)
    root = tmp_path / "root"
    worktree = root / ".pycastle" / "worktrees" / "issue-3"
    worktree.mkdir(parents=True)

    runtime = ClaudeRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=root)
    runtime.run("p", cwd=worktree, node="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[argv.index("-w") + 1] == str(worktree.resolve())
    # Mount source is the workspace root, never the worktree.
    assert f"{root.resolve()}:{root.resolve()}" in argv
    assert f"{worktree}:{worktree}" not in argv


def test_docker_resolves_relative_run_cwd_for_w(
    mock_popen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relative run cwd is resolved to absolute before it reaches -w, so the
    # container workdir is never a broken relative path (and agrees with codex's
    # resolved -C).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "worktree").mkdir()
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = ClaudeRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    runtime.run("p", cwd=Path("worktree"), node="implement")

    argv = mock_popen.call_args.args[0]
    workdir = argv[argv.index("-w") + 1]
    assert Path(workdir).is_absolute()
    assert workdir == str((tmp_path / "worktree").resolve())


def test_host_runtime_still_invokes_bare_claude(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Without a sandbox wrapper the runtime runs claude directly on the host.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    ClaudeRuntime().run("p", cwd=tmp_path, node="implement")

    assert mock_popen.call_args.args[0][0] == "claude"


def test_verbose_surfaces_thinking_live(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # With --verbose on, the thinking block in the stream is emitted as a
    # [THINKING:<node>] log line; the output still equals only the text block,
    # so thinking is never folded into the returned prose.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = ClaudeRuntime(verbose=True).run("p", cwd=tmp_path, node="implement")

    assert "[THINKING:implement] planning the change" in caplog.text
    assert result.output == "Implemented the feature."


def test_verbose_surfaces_output_live(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # With --verbose on, the text block is also surfaced as an [OUTPUT:<node>]
    # line so a reader sees what the model did; the returned output still equals
    # only the text block (surfacing is additive, never folded into handoff).
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = ClaudeRuntime(verbose=True).run("p", cwd=tmp_path, node="implement")

    assert "[OUTPUT:implement] Implemented the feature." in caplog.text
    assert result.output == "Implemented the feature."


def test_verbose_persists_thinking_to_sink(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # The transcript sink (which the orchestrator binds to the run dir) receives
    # each thinking chunk as (node, tag, text) so a finished run is auditable.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)
    captured: list[tuple[str, str, str]] = []

    ClaudeRuntime(
        verbose=True,
        transcript_sink=lambda node, tag, text: captured.append((node, tag, text)),
    ).run("p", cwd=tmp_path, node="implement")

    assert ("implement", "THINKING", "planning the change") in captured


def test_verbose_persists_output_to_sink(mock_popen: MagicMock, tmp_path: Path) -> None:
    # The text block is also persisted to the sink, tagged OUTPUT, and — because
    # the fixture orders thinking before text — it lands after the THINKING chunk,
    # proving the single sink interleaves both streams in chronological order.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)
    captured: list[tuple[str, str, str]] = []

    ClaudeRuntime(
        verbose=True,
        transcript_sink=lambda node, tag, text: captured.append((node, tag, text)),
    ).run("p", cwd=tmp_path, node="implement")

    assert ("implement", "OUTPUT", "Implemented the feature.") in captured
    thinking_idx = captured.index(("implement", "THINKING", "planning the change"))
    output_idx = captured.index(("implement", "OUTPUT", "Implemented the feature."))
    assert thinking_idx < output_idx


def test_verbose_surfaces_thinking_delta_fallback(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The SSE-style fallback: a content_block_delta with a thinking_delta is
    # surfaced too, mirroring Ralph's robustness if the stream format shifts.
    events = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "delta reasoning"},
        },
        {"type": "result", "result": "done", "is_error": False, "num_turns": 1},
    ]
    mock_popen.return_value = _fake_proc(events)

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        ClaudeRuntime(verbose=True).run("p", cwd=tmp_path, node="plan")

    assert "[THINKING:plan] delta reasoning" in caplog.text


def test_verbose_surfaces_text_delta_fallback(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The OUTPUT side of the SSE-style fallback: a content_block_delta with a
    # text_delta is surfaced AND appended to output_buf, so a delta-only stream
    # still yields handoff output (matching Ralph's run.py).
    events = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "delta out"},
        },
        {"type": "result", "result": "done", "is_error": False, "num_turns": 1},
    ]
    mock_popen.return_value = _fake_proc(events)

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = ClaudeRuntime(verbose=True).run("p", cwd=tmp_path, node="plan")

    assert "[OUTPUT:plan] delta out" in caplog.text
    assert result.output == "delta out"


def test_non_verbose_drops_thinking_and_output_surfacing(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The behaviour-unchanged guard: without --verbose, neither thinking nor
    # output is surfaced — no [THINKING:...]/[OUTPUT:...] log line, the sink is
    # never called — but the output is still computed for handoff.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)
    captured: list[tuple[str, str, str]] = []

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = ClaudeRuntime(
            transcript_sink=lambda node, tag, text: captured.append((node, tag, text))
        ).run("p", cwd=tmp_path, node="implement")

    assert "[THINKING:" not in caplog.text
    assert "[OUTPUT:" not in caplog.text
    assert captured == []
    assert result.output == "Implemented the feature."


def test_in_docker_threads_verbose_and_sink(tmp_path: Path) -> None:
    # in_docker forwards verbose and the sink onto the constructed runtime so a
    # docker run captures the transcript just like the host path.
    sink = MagicMock()
    runtime = ClaudeRuntime.in_docker(
        image="sha256:" + ("a" * 64),
        workspace=tmp_path,
        verbose=True,
        transcript_sink=sink,
    )
    assert runtime.verbose is True
    assert runtime.transcript_sink is sink
