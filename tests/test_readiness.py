from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pycastle import cli, sandbox
from pycastle.readiness import (
    CHECK_IDS,
    CheckResult,
    DefaultReadinessAdapter,
    EligibleItem,
    ReadinessConfiguration,
    ReadinessDependencies,
    Status,
    evaluate_readiness,
    render_human,
    render_json,
)


def _valid_fixture(path: Path) -> Path:
    fixture = path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (fixture / "version").write_text("0.1.0\n")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build, build_run, phase\n"
        "run = build_run(\n"
        " before=build(start='prepare', phases=[phase('prepare', 'before.md')]),\n"
        " item=build(start='work', phases=[phase('work', 'item.md')]),\n"
        " after=build(start='report', phases=[phase('report', 'after.md')]),\n"
        ")\n"
    )
    for name in ("before.md", "item.md", "after.md"):
        (prompts / name).write_text(name)
    gate = fixture / "gate"
    gate.write_text("#!/bin/sh\nexit 0\n")
    gate.chmod(0o755)
    return fixture


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        call = tuple(argv)
        self.calls.append(call)
        joined = " ".join(call)
        forbidden = (
            "docker",
            " worktree ",
            " commit",
            " push",
            " issue edit",
            " pr create",
        )
        assert not any(value in f" {joined} " for value in forbidden)
        outputs = {
            ("git", "symbolic-ref", "--quiet", "--short", "HEAD"): "main\n",
            ("git", "status", "--porcelain", "--untracked-files=all"): "",
            ("gh", "api", "repos/owner/repo", "--jq", ".permissions.push"): "true\n",
        }
        if call[:3] == ("gh", "repo", "view"):
            output = '{"nameWithOwner":"owner/repo"}\n'
        elif call[:3] == ("gh", "label", "list"):
            output = '[{"name":"ready-for-agent"},{"name":"ready-for-human"}]'
        elif call[:3] == ("gh", "issue", "list"):
            output = json.dumps(
                [
                    {
                        "number": 9,
                        "title": "Nine",
                        "labels": [{"name": "ready-for-agent"}],
                        "assignees": [{"login": "octocat"}],
                    },
                    {
                        "number": 2,
                        "title": "Two",
                        "labels": [{"name": "ready-for-agent"}],
                        "assignees": [{"login": "octocat"}],
                    },
                    {
                        "number": 1,
                        "title": "Other",
                        "labels": [{"name": "ready-for-agent"}],
                        "assignees": [{"login": "someone"}],
                    },
                ]
            )
        else:
            output = outputs.get(call, "")
        return subprocess.CompletedProcess(argv, 0, output, "")


def test_host_stub_production_adapter_reports_complete_ready_snapshot(
    tmp_path: Path,
) -> None:
    fixture = _valid_fixture(tmp_path)
    runner = RecordingRunner()
    adapter = DefaultReadinessAdapter(
        fixture, tmp_path, runner=runner, exists=lambda name: name in {"git", "gh"}
    )

    report = evaluate_readiness(configuration(), adapter.dependencies())

    assert report.ready
    assert [check.id for check in report.checks] == list(CHECK_IDS)
    assert {check.id: check.status for check in report.checks}[
        "agent_image"
    ] is Status.NOT_APPLICABLE
    assert {check.id: check.status for check in report.checks}[
        "runtime_authentication"
    ] is Status.NOT_APPLICABLE
    assert report.eligible_items == (EligibleItem(2, "Two"), EligibleItem(9, "Nine"))
    assert any(call[-1] == "--check-tools" for call in runner.calls)
    assert not (fixture / "__pycache__").exists()
    assert sys.dont_write_bytecode is False


def test_fixture_structure_rejects_symlinked_prompt(tmp_path: Path) -> None:
    fixture = _valid_fixture(tmp_path)
    prompt = fixture / "prompts" / "item.md"
    target = fixture / "prompts" / "target.md"
    target.write_text("target")
    prompt.unlink()
    os.symlink(target, prompt)
    adapter = DefaultReadinessAdapter(fixture, tmp_path)

    with pytest.raises(ValueError, match="prompt"):
        adapter.check_fixture_structure(configuration())


def test_github_repository_requires_matching_identity(tmp_path: Path) -> None:
    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, '{"nameWithOwner":"other/repo"}', ""
        )

    result = DefaultReadinessAdapter(
        tmp_path, tmp_path, runner=runner
    ).check_github_repository(configuration())

    assert result.status is Status.FAIL


def test_doctor_and_run_share_readiness_arguments_and_defaults() -> None:
    parser = cli.build_parser()
    doctor = parser.parse_args(["doctor"])
    run = parser.parse_args(["run"])
    fields = (
        "runtime",
        "sandbox",
        "image",
        "assignee",
        "include_unassigned",
        "iterations",
    )

    assert {field: getattr(doctor, field) for field in fields} == {
        field: getattr(run, field) for field in fields
    }
    assert not hasattr(doctor, "verbose")

    arguments = [
        "--runtime",
        "stub",
        "--sandbox",
        "host",
        "--image",
        "fixture/image",
        "--assignee",
        "octocat",
        "--include-unassigned",
        "--iterations",
        "3",
    ]
    doctor = parser.parse_args(["doctor", *arguments])
    run = parser.parse_args(["run", *arguments])
    assert {field: getattr(doctor, field) for field in fields} == {
        field: getattr(run, field) for field in fields
    }


def test_doctor_human_and_json_outputs_are_complete_and_single_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda check_id, _configuration: CheckResult(
                Status.FAIL if check_id == "gate_toolchain" else Status.PASS,
                "toolchain missing" if check_id == "gate_toolchain" else "ready",
                remediation="install tools" if check_id == "gate_toolchain" else None,
            ),
            eligible_items=lambda _configuration: [EligibleItem(4, "Unicode ✓")],
        ),
    )
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)

    assert cli.main(["doctor", "--runtime", "stub", "--sandbox", "host"]) == 1
    human = capsys.readouterr().out
    assert human == render_human(report) + "\n"
    assert all(check_id in human for check_id in CHECK_IDS)
    assert "Fix: install tools" in human
    assert "#4 Unicode ✓" in human

    assert cli.main(["doctor", "--runtime", "stub", "--sandbox", "host", "--json"]) == 1
    stdout = capsys.readouterr().out
    assert stdout.count("\n") == 1
    document = json.loads(stdout)
    assert document["schema_version"] == 1
    assert document["eligible_items"] == [{"number": 4, "title": "Unicode ✓"}]


def configuration() -> ReadinessConfiguration:
    return ReadinessConfiguration(
        repository="owner/repo",
        base_branch="main",
        github_default_branch="main",
        runtime="stub",
        sandbox="host",
        agent_image=None,
        assignee="octocat",
        include_unassigned=False,
        item_limit=2,
    )


def runtime_configuration(runtime: str) -> ReadinessConfiguration:
    return ReadinessConfiguration(
        **{
            **configuration().__dict__,
            "runtime": runtime,
        }
    )


class ScriptedRuntimeRunner:
    def __init__(
        self,
        responses: dict[
            tuple[str, ...],
            subprocess.CompletedProcess[str] | OSError | subprocess.TimeoutExpired,
        ],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        call = tuple(argv)
        self.calls.append((call, kwargs))
        response = self.responses[call]
        if isinstance(response, OSError | subprocess.TimeoutExpired):
            raise response
        return response


@pytest.mark.parametrize(
    ("runtime", "version", "status_argv"),
    [
        ("claude", "1.2.3 (Claude Code)\n", ("claude", "auth", "status")),
        ("codex", "codex-cli 4.5.6\n", ("codex", "login", "status")),
    ],
)
def test_host_runtime_ready_uses_native_non_interactive_status(
    tmp_path: Path, runtime: str, version: str, status_argv: tuple[str, ...]
) -> None:
    version_argv = (runtime, "--version")
    runner = ScriptedRuntimeRunner(
        {
            version_argv: subprocess.CompletedProcess(version_argv, 0, version, ""),
            status_argv: subprocess.CompletedProcess(
                status_argv, 0, "authenticated", ""
            ),
        }
    )
    adapter = DefaultReadinessAdapter(tmp_path, tmp_path, runner=runner)
    config = runtime_configuration(runtime)

    runtime_result = adapter.check_runtime(config)
    authentication_result = adapter.check_runtime_authentication(config)

    assert runtime_result.status is Status.PASS
    assert runtime_result.facts == {"version": version.strip()}
    assert authentication_result.status is Status.PASS
    assert [call for call, _kwargs in runner.calls] == [version_argv, status_argv]
    assert all(
        kwargs == {"cwd": tmp_path, "capture": True, "timeout": 15.0}
        for _call, kwargs in runner.calls
    )
    assert all("prompt" not in " ".join(call) for call, _kwargs in runner.calls)


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_host_runtime_missing_is_reported_by_required_commands(
    tmp_path: Path, runtime: str
) -> None:
    adapter = DefaultReadinessAdapter(
        tmp_path,
        tmp_path,
        runner=lambda *_args, **_kwargs: pytest.fail("missing Runtime was invoked"),
        exists=lambda command: command in {"git", "gh"},
    )

    result = adapter.check_required_commands(runtime_configuration(runtime))

    assert result.status is Status.FAIL
    assert result.facts == {"missing": [runtime]}
    assert result.remediation


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_host_runtime_unlaunchable_has_fixed_safe_failure(
    tmp_path: Path, runtime: str
) -> None:
    version_argv = (runtime, "--version")
    secret = "credential=do-not-report"
    runner = ScriptedRuntimeRunner({version_argv: PermissionError(secret)})
    adapter = DefaultReadinessAdapter(tmp_path, tmp_path, runner=runner)

    result = adapter.check_runtime(runtime_configuration(runtime))

    assert result.status is Status.FAIL
    assert result.summary == "Runtime is not launchable."
    assert result.remediation == "Install or repair the selected Runtime."
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("runtime", "status_argv"),
    [
        ("claude", ("claude", "auth", "status")),
        ("codex", ("codex", "login", "status")),
    ],
)
def test_host_runtime_authentication_failure_never_reports_child_output(
    tmp_path: Path, runtime: str, status_argv: tuple[str, ...]
) -> None:
    secret = "token=do-not-report"
    runner = ScriptedRuntimeRunner(
        {
            status_argv: subprocess.CompletedProcess(
                status_argv, 1, f"stdout {secret}", f"stderr {secret}"
            )
        }
    )
    adapter = DefaultReadinessAdapter(tmp_path, tmp_path, runner=runner)

    result = adapter.check_runtime_authentication(runtime_configuration(runtime))
    report = evaluate_readiness(
        runtime_configuration(runtime),
        ReadinessDependencies(
            probe=lambda check_id, _config: (
                result
                if check_id == "runtime_authentication"
                else CheckResult(Status.PASS, "Ready")
            ),
            eligible_items=lambda _config: [EligibleItem(1, "One")],
        ),
    )

    assert result.status is Status.FAIL
    assert result.facts == {}
    assert result.remediation == f"Authenticate {runtime} in the selected Sandbox."
    assert secret not in render_json(report)
    assert secret not in render_human(report)


@pytest.mark.parametrize(
    ("runtime", "status_argv"),
    [
        ("claude", ("claude", "auth", "status")),
        ("codex", ("codex", "login", "status")),
    ],
)
@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, "unsupported"), (0, "line one\nline two"), (0, "x" * 101)],
)
def test_host_runtime_version_unavailable_does_not_fail_readiness(
    tmp_path: Path,
    runtime: str,
    status_argv: tuple[str, ...],
    returncode: int,
    stdout: str,
) -> None:
    version_argv = (runtime, "--version")
    runner = ScriptedRuntimeRunner(
        {
            version_argv: subprocess.CompletedProcess(
                version_argv, returncode, stdout, "diagnostic"
            ),
            status_argv: subprocess.CompletedProcess(status_argv, 0, "ready", ""),
        }
    )
    adapter = DefaultReadinessAdapter(tmp_path, tmp_path, runner=runner)
    config = runtime_configuration(runtime)

    result = adapter.check_runtime(config)
    authentication = adapter.check_runtime_authentication(config)

    assert result.status is Status.PASS
    assert result.facts == {}
    assert authentication.status is Status.PASS
    assert [call for call, _kwargs in runner.calls] == [version_argv, status_argv]


@pytest.mark.parametrize(
    ("stdout", "expected_facts"),
    [
        (None, {}),
        (b"codex-cli 1.2.3", {}),
        ("", {}),
        (" \n\t", {}),
        ("v" * 100, {"version": "v" * 100}),
        ("v" * 101, {}),
        ("version: 1.2.3", {}),
    ],
)
def test_runtime_version_facts_are_bounded_and_allow_listed(
    tmp_path: Path, stdout: object, expected_facts: dict[str, str]
) -> None:
    argv = ("codex", "--version")
    result = subprocess.CompletedProcess(argv, 0, stdout, "ignored secret")
    adapter = DefaultReadinessAdapter(
        tmp_path, tmp_path, runner=ScriptedRuntimeRunner({argv: result})
    )

    readiness = adapter.check_runtime(runtime_configuration("codex"))

    assert readiness.status is Status.PASS
    assert readiness.facts == expected_facts


@pytest.mark.parametrize("error_kind", ["os_error", "timeout"])
@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_runtime_authentication_probe_errors_are_bounded_and_actionable(
    tmp_path: Path, runtime: str, error_kind: str
) -> None:
    argv = tuple(sandbox.RUNTIME_CONFIG[runtime].status_args)
    secret = "credential=do-not-report"
    error: OSError | subprocess.TimeoutExpired
    if error_kind == "os_error":
        error = OSError(secret)
    else:
        error = subprocess.TimeoutExpired(argv, 15.0, output=secret, stderr=secret)
    adapter = DefaultReadinessAdapter(
        tmp_path, tmp_path, runner=ScriptedRuntimeRunner({argv: error})
    )

    result = adapter.check_runtime_authentication(runtime_configuration(runtime))

    assert result.status is Status.FAIL
    assert result.facts == {}
    assert result.summary == "Runtime authentication status could not be checked."
    assert result.remediation == (
        f"Verify {runtime} and authenticate it in the selected Sandbox."
    )
    assert secret not in repr(result)


def test_report_has_stable_order_schema_and_number_title_only_items() -> None:
    calls: list[str] = []

    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        calls.append(check_id)
        if check_id == "runtime_authentication":
            return CheckResult(Status.NOT_APPLICABLE, "Stub needs no authentication")
        return CheckResult(Status.PASS, "Ready", {"safe": "fact"})

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=probe,
            eligible_items=lambda _configuration: [
                EligibleItem(7, "Seven"),
                EligibleItem(2, "Two"),
            ],
        ),
    )

    assert calls == list(CHECK_IDS[:-1])
    assert [check.id for check in report.checks] == list(CHECK_IDS)
    assert report.ready is True
    document = json.loads(render_json(report))
    assert list(document) == [
        "schema_version",
        "ready",
        "runner_version",
        "configuration",
        "checks",
        "eligible_items",
    ]
    assert document["schema_version"] == 1
    assert document["eligible_items"] == [
        {"number": 2, "title": "Two"},
        {"number": 7, "title": "Seven"},
    ]


def test_failed_prerequisite_blocks_dependents_and_independent_checks_continue() -> (
    None
):
    called: list[str] = []

    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        called.append(check_id)
        if check_id == "fixture_compatibility":
            return CheckResult(
                Status.FAIL, "Fixture is incompatible", remediation="upgrade"
            )
        return CheckResult(Status.PASS, "Ready")

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(probe=probe, eligible_items=lambda _configuration: []),
    )
    by_id = {check.id: check for check in report.checks}

    assert by_id["fixture_structure"].status is Status.BLOCKED
    assert by_id["sandbox"].status is Status.BLOCKED
    assert by_id["github_authentication"].status is Status.PASS
    assert "github_authentication" in called
    assert report.ready is False


def test_zero_items_is_a_failure_with_actionable_remediation() -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=lambda _configuration: [],
        ),
    )
    check = report.checks[-1]
    assert check.id == "eligible_items"
    assert check.status is Status.FAIL
    assert check.remediation
    assert report.ready is False


@pytest.mark.parametrize(
    "error",
    [
        AttributeError("Item metadata is not an object"),
        KeyError("number"),
        TypeError("Item metadata has an invalid shape"),
    ],
)
def test_invalid_item_metadata_makes_doctor_unready(error: Exception) -> None:
    def invalid_items(_configuration: ReadinessConfiguration) -> list[EligibleItem]:
        raise error

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=invalid_items,
        ),
    )

    check = report.checks[-1]
    assert check.id == "eligible_items"
    assert check.status is Status.FAIL
    assert report.eligible_items == ()
    assert report.ready is False


def test_child_diagnostics_are_not_retained_in_report() -> None:
    secret = "ghp_super_secret raw gate output"
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(
                Status.FAIL, "Probe failed", unsafe_detail=secret
            ),
            eligible_items=lambda _configuration: [],
        ),
    )
    assert secret not in render_json(report)


def test_probe_text_is_bounded() -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(
                Status.FAIL, "s" * 501, remediation="r" * 501
            ),
            eligible_items=lambda _configuration: [],
        ),
    )

    assert len(report.checks[0].summary) == 500
    assert report.checks[0].remediation is not None
    assert len(report.checks[0].remediation) == 500


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_doctor_and_run_reject_non_positive_item_limits(limit: str) -> None:
    from pycastle.cli import build_parser

    for command in ("doctor", "run"):
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args([command, "--iterations", limit])
        assert exc_info.value.code == 2


@pytest.mark.parametrize("flag", ["--assignee", "--image"])
def test_doctor_and_run_reject_empty_readiness_values(flag: str) -> None:
    from pycastle.cli import build_parser

    for command in ("doctor", "run"):
        with pytest.raises(SystemExit) as exc_info:
            build_parser().parse_args([command, flag, " "])
        assert exc_info.value.code == 2


def test_run_stops_before_first_side_effect_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda check_id, _configuration: CheckResult(
                Status.FAIL if check_id == "working_tree" else Status.PASS,
                "dirty" if check_id == "working_tree" else "ready",
            ),
            eligible_items=lambda _configuration: [EligibleItem(1, "One")],
        ),
    )

    def side_effect(*args: object, **kwargs: object) -> None:
        pytest.fail("Run side effect started")

    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(cli, "_make_run_id", side_effect)
    monkeypatch.setattr(cli, "_build_runtime", side_effect)
    monkeypatch.setattr(cli, "run_loop", side_effect)

    args = cli.build_parser().parse_args(["run", "--runtime", "stub"])
    args.sandbox = "host"
    assert cli._cmd_run(args) == 1


def test_run_maps_only_zero_eligible_items_to_noop_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "ready"),
            eligible_items=lambda _configuration: [],
        ),
    )
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(
        cli,
        "run_loop",
        lambda *args, **kwargs: pytest.fail("empty Run started a side effect"),
    )

    args = cli.build_parser().parse_args(["run", "--runtime", "stub"])
    args.sandbox = "host"
    assert cli._cmd_run(args) == 0
