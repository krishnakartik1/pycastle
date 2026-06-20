"""The real Codex adapter parses the JSONL event stream and resumes threads."""

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


def test_build_command_resume_places_thread_id(tmp_path: Path) -> None:
    runtime = CodexRuntime()
    cmd = runtime.build_command(
        "write handoff", cwd=tmp_path, resume_thread_id="thread-456"
    )
    assert cmd[-5:] == [
        "exec",
        "resume",
        "--json",
        "thread-456",
        "write handoff",
    ]


def test_build_command_host_resume_carries_workspace_write(tmp_path: Path) -> None:
    # The handoff path resumes a prior thread, and it must still scope writes to
    # the worktree: a host resume carries -s workspace-write before exec, just
    # like a fresh host run, or the retry attempt would silently no-op too (#35).
    cmd = CodexRuntime().build_command(
        "write handoff", cwd=tmp_path, resume_thread_id="thread-456"
    )
    assert cmd[cmd.index("-s") + 1] == "workspace-write"
    assert cmd.index("-s") < cmd.index("exec")
    assert CODEX_DOCKER_BYPASS not in cmd


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
    CodexRuntime().run("p", cwd=relative, phase="implement")

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

    CodexRuntime().run("p", cwd=absolute, phase="implement")

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
    from pycastle import sandbox

    monkeypatch.chdir(tmp_path)
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    relative = Path(".pycastle/worktrees/issue-3")
    runtime = CodexRuntime.in_docker(workspace=tmp_path)
    runtime.run("p", cwd=relative, phase="implement")

    argv = mock_popen.call_args.args[0]
    image_idx = argv.index(sandbox.DEFAULT_IMAGE)
    inner = argv[image_idx + 1 :]
    inner_dash_c_value = inner[inner.index("-C") + 1]
    assert Path(inner_dash_c_value).is_absolute()
    assert inner_dash_c_value == str(relative.resolve())


def test_parses_jsonl_into_output_and_telemetry(
    mock_popen: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the clock so elapsed_ms is deterministic (125 ms), mirroring Ralph.
    monkeypatch.setattr(
        "pycastle.runtime.time.perf_counter", MagicMock(side_effect=[10.0, 10.125])
    )
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    result = CodexRuntime(model="gpt-test").run(
        "do work", cwd=tmp_path, phase="implement"
    )

    # Only the agent_message prose becomes output; command/file-change items drop.
    assert result.output == "implemented"

    telemetry = result.telemetry
    assert telemetry.runtime == "codex"
    assert telemetry.phase == "implement"
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

    CodexRuntime(model="gpt-test").run("do work", cwd=tmp_path, phase="implement")

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


def test_resume_path_runs_resume_argv_and_keeps_thread_id(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # Resuming continues the same thread: the resume argv carries the thread id,
    # and a fresh thread.started in the resumed stream is surfaced again.
    events = [
        {"type": "thread.started", "thread_id": "thread-456"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "handoff written"},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
    ]
    mock_popen.return_value = _fake_proc(events)

    result = CodexRuntime().run(
        "write handoff",
        cwd=tmp_path,
        phase="handoff",
        resume_thread_id="thread-456",
    )

    assert mock_popen.call_args.args[0][-5:] == [
        "exec",
        "resume",
        "--json",
        "thread-456",
        "write handoff",
    ]
    assert result.output == "handoff written"
    assert result.telemetry.thread_id == "thread-456"


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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

    assert result.output == "implemented"
    assert result.telemetry.thread_id == "thread-123"
    assert result.telemetry.num_turns == 1


def test_nonzero_exit_raises_crash(mock_popen: MagicMock, tmp_path: Path) -> None:
    proc = _fake_proc([], returncode=1)
    proc.stderr = io.StringIO("boom")
    mock_popen.return_value = proc

    with pytest.raises(AgentCrashError) as exc_info:
        CodexRuntime().run("p", cwd=tmp_path, phase="implement")

    assert exc_info.value.phase == "implement"
    assert exc_info.value.exit_code == 1


def test_nonzero_exit_reads_stderr_for_logging(
    mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    proc = _fake_proc([], returncode=2)
    proc.stderr = io.StringIO("permission denied\n")
    mock_popen.return_value = proc

    with caplog.at_level("ERROR", logger="pycastle.runtime"):
        with pytest.raises(AgentCrashError):
            CodexRuntime().run("p", cwd=tmp_path, phase="review")

    assert "permission denied" in caplog.text
    proc.wait.assert_called_once()


def test_empty_stream_with_clean_exit_degrades_gracefully(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    # No events and a zero exit: empty output, bare telemetry, no usage, no
    # thread id, and no crash.
    mock_popen.return_value = _fake_proc([], returncode=0)

    result = CodexRuntime().run("p", cwd=tmp_path, phase="plan")

    assert result.output == ""
    assert result.telemetry.runtime == "codex"
    assert result.telemetry.phase == "plan"
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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

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

    result = CodexRuntime().run("p", cwd=tmp_path, phase="implement")

    assert result.output == "real"


def test_docker_runtime_wraps_inner_argv_into_docker_run(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    """A docker-sandboxed Codex run invokes ``docker run`` wrapping codex.

    The Docker argv carries CODEX_HOME, the codex auth volume, and the bypass
    flag on the inner codex argv. The same JSONL parsing applies to stdout.
    """
    from pycastle import sandbox

    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = CodexRuntime.in_docker(workspace=tmp_path)
    result = runtime.run("a prompt", cwd=tmp_path, phase="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert sandbox.DEFAULT_IMAGE in argv
    image_idx = argv.index(sandbox.DEFAULT_IMAGE)
    # The inner argv starts the codex command and carries the bypass flag.
    inner = argv[image_idx + 1 :]
    assert inner[0] == "codex"
    assert CODEX_DOCKER_BYPASS in inner
    # The Docker path is not double-flagged: it carries the bypass, not the
    # scoped host -s workspace-write sandbox.
    assert "workspace-write" not in inner
    assert "-s" not in inner
    # CODEX_HOME and the codex auth volume are pinned, not Claude's.
    assert "pycastle-codex-auth:/home/node/.codex" in argv
    assert "CODEX_HOME=/home/node/.codex" in argv
    assert "CLAUDE_CONFIG_DIR=/home/node/.claude" not in argv
    # Same JSONL parsing as the host path.
    assert result.output == "implemented"
    assert result.telemetry.thread_id == "thread-123"


def test_docker_runtime_runs_both_runtime_and_commands_in_container(
    mock_popen: MagicMock, tmp_path: Path
) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    runtime = CodexRuntime.in_docker(workspace=tmp_path)
    runtime.run("p", cwd=tmp_path, phase="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[0] == "docker"
    assert argv[0] != "codex"


def test_host_runtime_invokes_bare_codex(mock_popen: MagicMock, tmp_path: Path) -> None:
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    CodexRuntime().run("p", cwd=tmp_path, phase="implement")

    argv = mock_popen.call_args.args[0]
    assert argv[0] == "codex"
    # The host path never carries the docker-only bypass flag.
    assert CODEX_DOCKER_BYPASS not in argv
    # It carries the scoped host sandbox so codex may write the worktree (#35).
    assert argv[argv.index("-s") + 1] == "workspace-write"


def test_run_works_one_issue_end_to_end_via_codex(
    mock_popen: MagicMock, fixture_dir: Path, tmp_path: Path
) -> None:
    """The orchestrator completes an issue using the real Codex adapter.

    The agent is mocked at the subprocess boundary; every git/gh call is mocked
    through the orchestrator's runner — no real agent runs.
    """
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    def _ok(
        argv: list[str], *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        # A non-empty diff (exit 1) for the post-commit no-change check, so the
        # mocked agent's work is treated as a real change rather than a no-op.
        if argv[:3] == ["git", "diff", "--quiet"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")

    issue = IssueRef(number=9, title="Wire codex", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = MagicMock(side_effect=_ok)

    outcome = orchestrator.run_batch(
        runtime=make_runtime("codex"),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-090000",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    assert outcome.completed == [9]
    assert outcome.pr_opened is True
    # The real adapter ran through the graph: the codex CLI was invoked.
    assert mock_popen.call_args.args[0][0] == "codex"


def test_run_works_one_issue_end_to_end_via_codex_in_docker(
    mock_popen: MagicMock, fixture_dir: Path, tmp_path: Path
) -> None:
    """The orchestrator completes an issue using Codex inside the Docker sandbox."""
    mock_popen.return_value = _fake_proc(_SUCCESS_EVENTS)

    def _ok(
        argv: list[str], *_args: object, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        # A non-empty diff (exit 1) for the post-commit no-change check, so the
        # mocked agent's work is treated as a real change rather than a no-op.
        if argv[:3] == ["git", "diff", "--quiet"]:
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="")

    issue = IssueRef(number=10, title="Codex in docker", assignees=["krishna"])
    source = MagicMock()
    source.list_ready.return_value = [issue]
    runner = MagicMock(side_effect=_ok)

    outcome = orchestrator.run_batch(
        runtime=CodexRuntime.in_docker(workspace=tmp_path),
        issue_source=source,
        fixture_dir=fixture_dir,
        repo="owner/repo",
        base_branch="main",
        assignee="krishna",
        run_id="20260613-090000",
        iterations=1,
        workspace=tmp_path,
        worktree_root=tmp_path / "wt",
        runner=runner,
    )

    assert outcome.completed == [10]
    assert outcome.pr_opened is True
    # The agent ran in Docker, and the inner codex argv carried the bypass flag.
    argv = mock_popen.call_args.args[0]
    assert argv[0] == "docker"
    assert CODEX_DOCKER_BYPASS in argv
