"""The real Codex adapter parses JSONL and starts each node in a fresh thread."""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle.runtime import (
    CODEX_DOCKER_BYPASS,
    AgentCrashError,
    CodexRuntime,
    Runtime,
    make_runtime,
)


def _stream(events: list[dict]) -> io.StringIO:
    """Render captured events as the JSONL the codex CLI emits."""
    return io.StringIO("".join(json.dumps(event) + "\n" for event in events))


def _fake_proc(events: list[dict], *, returncode: int = 0) -> MagicMock:
    """Build a fake Popen whose stdout replays captured Codex events."""
    proc = MagicMock()
    proc.stdout = _stream(events)
    proc.stderr = io.StringIO("")
    proc.returncode = returncode
    proc.wait.return_value = returncode
    return proc


# A realistic Codex transcript: the thread id, one agent_message of prose, a
# verbose command run and a file change (both dropped from output), and the
# final turn.completed carrying token usage.
_SUCCESS_EVENTS = [
    {"type": "thread.started", "thread_id": "thread-123"},
    {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "implemented"},
    },
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "pytest",
            "aggregated_output": "failed\n",
            "exit_code": 1,
            "status": "failed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [{"path": "src/example.py", "kind": "update"}],
            "status": "completed",
        },
    },
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 11,
            "cached_input_tokens": 7,
            "output_tokens": 5,
            "reasoning_output_tokens": 3,
        },
    },
]


@pytest.fixture
def mock_popen(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch subprocess.Popen in the runtime module and return the mock."""
    popen = MagicMock()
    monkeypatch.setattr("pycastle.runtime.subprocess.Popen", popen)
    return popen


def test_codex_runtime_satisfies_runtime_protocol() -> None:
    assert isinstance(CodexRuntime(), Runtime)


def test_make_runtime_selects_codex() -> None:
    runtime = make_runtime("codex")
    assert isinstance(runtime, CodexRuntime)
    assert runtime.name == "codex"


def test_build_command_host_is_codex_exec_json(tmp_path: Path) -> None:
    # The host path scopes writes to the worktree with -s workspace-write (Codex's
    # default sandbox is read-only) and carries no docker-only bypass flag.
    runtime = CodexRuntime(model="gpt-test")
    # build_command always emits the resolved (absolute) -C; assert against
    # tmp_path.resolve() so the test holds where the temp root is symlinked
    # (e.g. macOS /var -> /private/var) rather than only where it is not.
    assert runtime.build_command("do the work", cwd=tmp_path) == [
        "codex",
        "-C",
        str(tmp_path.resolve()),
        "--model",
        "gpt-test",
        "-s",
        "workspace-write",
        "exec",
        "--json",
        "do the work",
    ]


def test_build_command_minimal_omits_model_and_bypass(tmp_path: Path) -> None:
    assert CodexRuntime().build_command("hi", cwd=tmp_path) == [
        "codex",
        "-C",
        str(tmp_path.resolve()),
        "-s",
        "workspace-write",
        "exec",
        "--json",
        "hi",
    ]


def test_verbosity_does_not_change_provider_invocation(tmp_path: Path) -> None:
    verbose = CodexRuntime(verbose=True).build_command("hi", cwd=tmp_path)
    quiet = CodexRuntime(verbose=False).build_command("hi", cwd=tmp_path)
    assert verbose == quiet


def test_build_command_omits_reasoning_summary_override(tmp_path: Path) -> None:
    assert "-c" not in CodexRuntime(verbose=False).build_command("hi", cwd=tmp_path)


def test_build_command_host_carries_workspace_write(tmp_path: Path) -> None:
    # On host the scoped sandbox is -s workspace-write, placed before exec (where
    # the docker bypass otherwise sits), and the docker-only bypass is absent.
    cmd = CodexRuntime().build_command("hi", cwd=tmp_path)
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert cmd.index("-s") < cmd.index("exec")
    assert CODEX_DOCKER_BYPASS not in cmd


def test_build_command_bypass_flag_when_sandboxed(tmp_path: Path) -> None:
    # The bypass flag lives on the runtime's inner argv (before exec), not the
    # docker wrapper, so the wrapper stays runtime-agnostic.
    runtime = CodexRuntime(bypass_sandbox=True)
    cmd = runtime.build_command("hi", cwd=tmp_path)
    assert cmd == [
        "codex",
        "-C",
        str(tmp_path.resolve()),
        CODEX_DOCKER_BYPASS,
        "exec",
        "--json",
        "hi",
    ]
    # The bypass flag precedes the exec subcommand (it is a global codex flag).
    assert cmd.index(CODEX_DOCKER_BYPASS) < cmd.index("exec")
    # The bypass and the host -s sandbox are mutually exclusive: a docker run
    # carries the bypass and never the scoped host sandbox (no double flag).
    assert "-s" not in cmd
    assert "workspace-write" not in cmd


def test_build_command_resolves_relative_cwd_to_absolute_dash_c() -> None:
    # The orchestrator hands the runtime a relative worktree path derived from a
    # relative FIXTURE_DIR. Codex resolves a relative -C against its own process
    # cwd (which run() also sets to cwd), so the -C value must be absolute or it
    # doubles. build_command resolves it regardless of how it is called.
    relative = Path(".pycastle/worktrees/issue-3")
    cmd = CodexRuntime().build_command("p", cwd=relative)

    dash_c_value = cmd[cmd.index("-C") + 1]
    assert Path(dash_c_value).is_absolute()
    assert dash_c_value == str(relative.resolve())


def test_run_relative_cwd_does_not_double_dash_c_path(
    mock_popen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact failure from the issue: a relative cwd used for both -C and the
    # Popen cwd makes Codex re-resolve -C against the process cwd and enter
    # …/issue-3/.pycastle/worktrees/issue-3 (os error 2). Resolving once means
    # -C is absolute, carries no doubled segment, and matches the Popen cwd.
    monkeypatch.chdir(tmp_path)
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    relative = Path(".pycastle/worktrees/issue-3")
    CodexRuntime().run("p", cwd=relative, node="implement")

    argv = mock_popen.call_args.args[0]
    dash_c_value = argv[argv.index("-C") + 1]
    assert Path(dash_c_value).is_absolute()
    # The doubled segment from the bug must not appear.
    assert "worktrees/issue-3/.pycastle" not in dash_c_value
    # -C and the process cwd agree, so no second relative resolution is possible.
    assert dash_c_value == str(relative.resolve())
    assert mock_popen.call_args.kwargs["cwd"] == relative.resolve()


def test_run_absolute_cwd_is_idempotent(mock_popen: MagicMock, tmp_path: Path) -> None:
    # The common case: the orchestrator hands an already-absolute, already-real
    # worktree path. Resolving it (once in run, again in build_command) must be a
    # no-op — the -C value and the Popen cwd both equal the input unchanged, so
    # the fix never mangles a path that was already correct.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)
    absolute = tmp_path.resolve()

    CodexRuntime().run("p", cwd=absolute, node="implement")

    argv = mock_popen.call_args.args[0]
    dash_c_value = argv[argv.index("-C") + 1]
    assert dash_c_value == str(absolute)
    assert mock_popen.call_args.kwargs["cwd"] == absolute


def test_docker_relative_cwd_yields_absolute_inner_dash_c(
    mock_popen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under Docker the inner -C must also be absolute so it matches the
    # bind-mount path that build_run_command already resolves; a relative inner
    # -C would double under -w.

    monkeypatch.chdir(tmp_path)
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    relative = Path(".pycastle/worktrees/issue-3")
    runtime = CodexRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    runtime.run("p", cwd=relative, node="implement")

    argv = mock_popen.call_args.args[0]
    image_idx = argv.index("sha256:" + ("a" * 64))
    inner = argv[image_idx + 1 :]
    inner_dash_c_value = inner[inner.index("-C") + 1]
    assert Path(inner_dash_c_value).is_absolute()
    assert inner_dash_c_value == str(relative.resolve())


def test_docker_workdir_matches_inner_dash_c(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # The symmetric half of the #50 fix: under docker the container -w and the
    # inner codex -C must agree (both the resolved worktree), and the bind-mount
    # source must stay the workspace root, not the worktree.

    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)
    root = tmp_path / "root"
    worktree = root / ".pycastle" / "worktrees" / "issue-3"
    worktree.mkdir(parents=True)

    runtime = CodexRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=root)
    runtime.run("p", cwd=worktree, node="implement")

    argv = mock_popen.call_args.args[0]
    workdir = argv[argv.index("-w") + 1]
    image_idx = argv.index("sha256:" + ("a" * 64))
    inner = argv[image_idx + 1 :]
    inner_dash_c = inner[inner.index("-C") + 1]
    assert workdir == inner_dash_c == str(worktree.resolve())
    assert Path(workdir).is_absolute()
    # Mount source stays the workspace root, never the worktree.
    assert f"{root.resolve()}:{root.resolve()}" in argv
    assert f"{worktree}:{worktree}" not in argv


def test_parses_jsonl_into_output_and_telemetry(
    mock_popen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the clock so elapsed_ms is deterministic (125 ms), mirroring Ralph.
    monkeypatch.setattr(
        "pycastle.runtime.time.perf_counter", MagicMock(side_effect=[10.0, 10.125])
    )
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    result = CodexRuntime(model="gpt-test").run(
        "do work", cwd=tmp_path, node="implement"
    )

    # Only the agent_message prose becomes output; command/file-change items drop.
    assert result.output == "implemented"

    telemetry = result.telemetry
    assert telemetry.runtime == "codex"
    assert telemetry.node == "implement"
    assert telemetry.thread_id == "thread-123"
    # Codex reports no cost or duration; PyCastle measures elapsed wall time.
    assert telemetry.cost_usd is None
    assert telemetry.duration_ms is None
    assert telemetry.elapsed_ms == 125
    assert telemetry.num_turns == 1
    assert telemetry.is_error is False

    assert telemetry.usage is not None
    assert telemetry.usage.input_tokens == 11
    assert telemetry.usage.cached_input_tokens == 7
    assert telemetry.usage.output_tokens == 5
    assert telemetry.usage.reasoning_output_tokens == 3


def test_run_invokes_host_codex_argv(mock_popen: MagicMock, tmp_path: Path) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    CodexRuntime(model="gpt-test").run("do work", cwd=tmp_path, node="implement")

    assert mock_popen.call_args.args[0] == [
        "codex",
        "-C",
        str(tmp_path),
        "--model",
        "gpt-test",
        "-s",
        "workspace-write",
        "exec",
        "--json",
        "do work",
    ]
    assert mock_popen.call_args.kwargs["cwd"] == tmp_path


def test_ignores_unparseable_lines(mock_popen: MagicMock, tmp_path: Path) -> None:
    proc = MagicMock()
    proc.stdout = io.StringIO(
        "not json\n"
        + json.dumps(_SUCCESS_EVENTS[0])
        + "\n\n"
        + json.dumps(_SUCCESS_EVENTS[1])
        + "\n"
        + json.dumps(_SUCCESS_EVENTS[4])
        + "\n"
    )
    proc.stderr = io.StringIO("")
    proc.returncode = 0
    proc.wait.return_value = 0
    mock_popen.return_value = proc

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.output == "implemented"
    assert result.telemetry.thread_id == "thread-123"
    assert result.telemetry.num_turns == 1


def test_nonzero_exit_raises_crash(mock_popen: MagicMock, tmp_path: Path) -> None:
    proc = _fake_proc(
        [
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "partial answer"},
            }
        ],
        returncode=1,
    )
    proc.stderr = io.StringIO("boom")
    mock_popen.return_value = proc

    with pytest.raises(AgentCrashError) as exc_info:
        CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert exc_info.value.node == "implement"
    assert exc_info.value.exit_code == 1
    assert exc_info.value.transcript == "partial answer"
    assert exc_info.value.stderr == "boom"
    assert exc_info.value.telemetry is not None
    assert exc_info.value.telemetry.runtime == "codex"


def test_nonzero_exit_reads_stderr_for_logging(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proc = _fake_proc([], returncode=2)
    proc.stderr = io.StringIO("permission denied\n")
    mock_popen.return_value = proc

    with caplog.at_level("ERROR", logger="pycastle.runtime"):
        with pytest.raises(AgentCrashError):
            CodexRuntime().run("p", cwd=tmp_path, node="review")

    assert "permission denied" in caplog.text
    proc.wait.assert_called_once()


def test_item_selection_crash_retains_private_output_without_normal_logging(
    mock_popen: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    proc = _fake_proc(
        [
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "private candidate body",
                },
            }
        ],
        returncode=2,
    )
    proc.stderr = io.StringIO("private provider stderr\n")
    mock_popen.return_value = proc

    with caplog.at_level("ERROR", logger="pycastle.runtime"):
        with pytest.raises(AgentCrashError) as exc_info:
            CodexRuntime().run("p", cwd=tmp_path, node="item-selection")

    assert exc_info.value.transcript == "private candidate body"
    assert exc_info.value.stderr == "private provider stderr\n"
    assert "private candidate body" not in caplog.text
    assert "private provider stderr" not in caplog.text
    assert "private details retained locally" in caplog.text


def test_verbose_item_selection_crash_may_stream_private_stderr(
    mock_popen: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    proc = _fake_proc([], returncode=2)
    proc.stderr = io.StringIO("private verbose stderr\n")
    mock_popen.return_value = proc

    with caplog.at_level("ERROR", logger="pycastle.runtime"):
        with pytest.raises(AgentCrashError):
            CodexRuntime(verbose=True).run("p", cwd=tmp_path, node="item-selection")

    assert "private verbose stderr" in caplog.text


def test_empty_stream_with_clean_exit_degrades_gracefully(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # No events and a zero exit: empty output, bare telemetry, no usage, no
    # thread id, and no crash.
    mock_popen.return_value = _fake_proc([], returncode=0)

    result = CodexRuntime().run("p", cwd=tmp_path, node="plan")

    assert result.output == ""
    assert result.telemetry.runtime == "codex"
    assert result.telemetry.node == "plan"
    assert result.telemetry.thread_id is None
    assert result.telemetry.usage is None
    assert result.telemetry.num_turns == 0
    assert result.telemetry.is_error is False


def test_non_dict_usage_does_not_throw(mock_popen: MagicMock, tmp_path: Path) -> None:
    # A turn.completed whose usage is not a mapping must parse with usage=None
    # rather than raising.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.completed", "usage": "not-a-dict"},
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.telemetry.usage is None
    assert result.telemetry.num_turns == 1


def test_non_dict_item_does_not_throw(mock_popen: MagicMock, tmp_path: Path) -> None:
    # A malformed item.completed whose item is not a mapping (null, a string)
    # must be skipped rather than crashing the parse loop. A well-formed
    # agent_message after it is still captured, so one junk event does not
    # swallow the rest of the stream.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": None},
        {"type": "item.completed", "item": "not-a-dict"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "kept"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.output == "kept"
    assert result.telemetry.thread_id == "t"


def test_multiple_agent_messages_concatenate_in_order(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "first "}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "second"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.output == "first second"


def test_stream_without_turn_completed_degrades_no_crash(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # A stream that ends before any turn.completed (e.g. the agent answered but
    # the process was cut short) still yields the captured output and thread id;
    # telemetry just degrades — no usage, num_turns stays 0, and no crash.
    events = [
        {"type": "thread.started", "thread_id": "thread-789"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "partial answer"},
        },
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.output == "partial answer"
    assert result.telemetry.thread_id == "thread-789"
    assert result.telemetry.num_turns == 0
    assert result.telemetry.usage is None
    assert result.telemetry.is_error is False


def test_multiple_turns_count_each_and_keep_last_usage(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # num_turns counts every turn.completed, and the usage record is the last
    # turn's (it overwrites earlier turns), so a multi-turn run reports the final
    # cumulative counts Codex emits rather than the first turn's.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 4,
                "output_tokens": 9,
                "reasoning_output_tokens": 6,
            },
        },
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.telemetry.num_turns == 2
    assert result.telemetry.usage is not None
    assert result.telemetry.usage.input_tokens == 12
    assert result.telemetry.usage.cached_input_tokens == 4
    assert result.telemetry.usage.output_tokens == 9
    assert result.telemetry.usage.reasoning_output_tokens == 6


def test_codex_telemetry_leaves_claude_cache_fields_none(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Codex maps onto cached_input_tokens / reasoning_output_tokens; the
    # Claude-only cache fields must stay None so the two vocabularies never
    # collide in one TokenUsage record.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    usage = result.telemetry.usage
    assert usage is not None
    assert usage.cached_input_tokens == 7
    assert usage.reasoning_output_tokens == 3
    # Claude's vocabulary is absent for a Codex run.
    assert usage.cache_creation_input_tokens is None
    assert usage.cache_read_input_tokens is None
    # Codex reports no cost or runtime-side duration.
    assert result.telemetry.cost_usd is None
    assert result.telemetry.duration_ms is None


def test_empty_agent_message_text_contributes_nothing(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # An agent_message item carrying no text (empty or missing) adds nothing to
    # the output, so empty narration never injects blank strings into the result.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": ""}},
        {"type": "item.completed", "item": {"type": "agent_message"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "real"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run("p", cwd=tmp_path, node="implement")

    assert result.output == "real"


def test_verbose_surfaces_codex_reasoning_when_emitted(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # If codex exec --json emits a reasoning item, --verbose captures it the same
    # way claude thinking is captured: a [THINKING:<node>] line plus the sink.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "weighing the approach"},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)
    captured: list[tuple[str, str, str]] = []

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = CodexRuntime(
            verbose=True,
            transcript_sink=lambda node, tag, text: captured.append((node, tag, text)),
        ).run("p", cwd=tmp_path, node="implement")

    assert "[THINKING:implement] weighing the approach" in caplog.text
    assert ("implement", "THINKING", "weighing the approach") in captured
    # Reasoning never folds into output; only agent_message prose does.
    assert result.output == "done"


def test_verbose_surfaces_codex_agent_message_output(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The headline thinking-less case: a codex run with only an agent_message (no
    # reasoning item) is still legible under --verbose because the prose narration
    # is surfaced as an [OUTPUT:<node>] line and persisted to the sink.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)
    captured: list[tuple[str, str, str]] = []

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = CodexRuntime(
            verbose=True,
            transcript_sink=lambda node, tag, text: captured.append((node, tag, text)),
        ).run("p", cwd=tmp_path, node="implement")

    assert "[OUTPUT:implement] done" in caplog.text
    assert ("implement", "OUTPUT", "done") in captured
    assert result.output == "done"


def test_verbose_logs_unavailable_when_no_codex_reasoning(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # When codex emits no reasoning item, --verbose logs once that reasoning text
    # is unavailable; the agent_message still produces an [OUTPUT:<node>] line so
    # the run stays legible, and output and telemetry are unchanged.
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = CodexRuntime(verbose=True).run("p", cwd=tmp_path, node="implement")

    assert "codex reasoning text is unavailable" in caplog.text
    assert "[OUTPUT:implement] implemented" in caplog.text
    assert result.output == "implemented"
    assert result.telemetry.num_turns == 1


def test_non_verbose_drops_codex_reasoning(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The behaviour-unchanged guard: without --verbose, no thinking lines and no
    # unavailable note, and the sink is never called.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {"type": "reasoning", "text": "weighing the approach"},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)
    captured: list[tuple[str, str, str]] = []

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = CodexRuntime(
            transcript_sink=lambda node, tag, text: captured.append((node, tag, text))
        ).run("p", cwd=tmp_path, node="implement")

    assert "[THINKING:" not in caplog.text
    assert "[OUTPUT:" not in caplog.text
    assert "unavailable" not in caplog.text
    assert captured == []
    assert result.output == "done"


def test_in_docker_threads_verbose_and_sink(tmp_path: Path) -> None:
    # in_docker forwards verbose and the sink onto the constructed codex runtime.
    sink = MagicMock()
    runtime = CodexRuntime.in_docker(
        image="sha256:" + ("a" * 64),
        workspace=tmp_path,
        verbose=True,
        transcript_sink=sink,
    )
    assert runtime.verbose is True
    assert runtime.transcript_sink is sink


def test_verbose_flattens_codex_reasoning_content_list(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Codex often carries reasoning as a content/summary list of parts rather than
    # a plain text string. The parts are joined into one string so neither the log
    # line nor the sink ever sees a raw Python list repr.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "first part "},
                    {"type": "summary_text", "text": "second part"},
                ],
            },
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)
    captured: list[tuple[str, str, str]] = []

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        CodexRuntime(
            verbose=True,
            transcript_sink=lambda node, tag, text: captured.append((node, tag, text)),
        ).run("p", cwd=tmp_path, node="implement")

    assert "[THINKING:implement] first part second part" in caplog.text
    assert ("implement", "THINKING", "first part second part") in captured


def test_verbose_does_not_crash_on_malformed_reasoning_type(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A malformed item whose type is not a string must not crash the parse (the
    # "reasoning" membership test would otherwise raise on a non-string); the run
    # finishes and falls back to the unavailable note.
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"type": 123}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {}},
    ]
    mock_popen.return_value = _fake_proc(events)

    with caplog.at_level("INFO", logger="pycastle.runtime"):
        result = CodexRuntime(verbose=True).run("p", cwd=tmp_path, node="implement")

    assert result.output == "done"
    assert "codex reasoning text is unavailable" in caplog.text


def test_docker_runtime_wraps_inner_argv_into_docker_run(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    """A docker-sandboxed Codex run invokes ``docker run`` wrapping codex.

    The Docker argv carries CODEX_HOME, the codex auth volume, and the bypass
    flag on the inner codex argv. The same JSONL parsing applies to stdout.
    """

    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = CodexRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    result = runtime.run("a prompt", cwd=tmp_path, node="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "sha256:" + ("a" * 64) in argv
    image_idx = argv.index("sha256:" + ("a" * 64))
    # The inner argv starts the codex command and carries the bypass flag.
    inner = argv[image_idx + 1 :]
    assert inner[0] == "codex"
    assert CODEX_DOCKER_BYPASS in inner
    # The Docker path is not double-flagged: it carries the bypass, not the
    # scoped host -s workspace-write sandbox.
    assert "workspace-write" not in inner
    assert "-s" not in inner
    # CODEX_HOME and the codex auth volume are pinned, not Claude's.
    assert "pycastle-codex-auth:/pycastle/auth" in argv
    assert "CODEX_HOME=/pycastle/auth" in argv
    assert "CLAUDE_CONFIG_DIR=/pycastle/auth" not in argv
    # Same JSONL parsing as the host path.
    assert result.output == "implemented"
    assert result.telemetry.thread_id == "thread-123"


def test_docker_runtime_runs_both_runtime_and_commands_in_container(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = CodexRuntime.in_docker(image="sha256:" + ("a" * 64), workspace=tmp_path)
    runtime.run("p", cwd=tmp_path, node="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[0] == "docker"
    assert argv[0] != "codex"


def test_host_runtime_invokes_bare_codex(mock_popen: MagicMock, tmp_path: Path) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    CodexRuntime().run("p", cwd=tmp_path, node="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[0] == "codex"
    # The host path never carries the docker-only bypass flag.
    assert CODEX_DOCKER_BYPASS not in argv
    # It carries the scoped host sandbox so codex may write the worktree (#35).
    assert argv[argv.index("-s") + 1] == "workspace-write"
