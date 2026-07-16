from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pycastle import cli, sandbox
from pycastle.models import IssueRef
from pycastle.readiness import (
    CHECK_IDS,
    CheckResult,
    DefaultReadinessAdapter,
    EligibleItem,
    ReadinessConfiguration,
    ReadinessDependencies,
    ReadinessOutcome,
    Status,
    evaluate_readiness,
    render_human,
    render_json,
)


def test_readiness_has_three_explicit_overall_outcomes() -> None:
    def passing(_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        return CheckResult(Status.PASS, "ok")

    ready = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=passing,
            eligible_items=lambda _configuration: [EligibleItem(1, "One")],
        ),
    )
    no_work = evaluate_readiness(
        configuration(),
        ReadinessDependencies(probe=passing, eligible_items=lambda _configuration: []),
    )
    not_ready = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda check_id, _configuration: CheckResult(
                Status.FAIL if check_id == "working_tree" else Status.PASS, "result"
            ),
            eligible_items=lambda _configuration: [EligibleItem(1, "One")],
        ),
    )

    assert ready.outcome is ReadinessOutcome.READY
    assert no_work.outcome is ReadinessOutcome.NO_WORK
    assert not_ready.outcome is ReadinessOutcome.NOT_READY
    assert json.loads(render_json(no_work))["outcome"] == "no_work"


def test_no_work_skips_execution_coordination_checks() -> None:
    called: list[str] = []

    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        called.append(check_id)
        return CheckResult(Status.PASS, "ok")

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(probe=probe, eligible_items=lambda _configuration: []),
    )

    assert report.outcome is ReadinessOutcome.NO_WORK
    statuses = {check.id: check.status for check in report.checks}
    for check_id in ("agent_image", "runtime", "runtime_authentication"):
        assert statuses[check_id] is Status.NOT_APPLICABLE
        assert check_id not in called


def _valid_fixture(path: Path) -> Path:
    fixture = path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (fixture / "version").write_text("0.1.0\n")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run, execution_graph, runtime_node\n"
        "run = build_run(\n"
        " before=execution_graph(start='prepare', nodes=[runtime_node('prepare', 'before.md')]),\n"
        " item=execution_graph(start='work', nodes=[runtime_node('work', 'item.md')]),\n"
        " after=execution_graph(start='report', nodes=[runtime_node('report', 'after.md')]),\n"
        ")\n"
    )
    for name in ("before.md", "item.md", "after.md"):
        (prompts / name).write_text(name)
    gate = fixture / "gate"
    gate.write_text("#!/bin/sh\nexit 0\n")
    gate.chmod(0o755)
    setup = fixture / "setup"
    setup.write_text("#!/bin/sh\nexit 0\n")
    setup.chmod(0o755)
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
            (
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                "refs/heads/main",
            ): "0123456789abcdef0123456789abcdef01234567\trefs/heads/main\n",
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

    assert report.outcome is ReadinessOutcome.READY
    assert [check.id for check in report.checks] == list(CHECK_IDS)
    assert {check.id: check.status for check in report.checks}[
        "agent_image"
    ] is Status.NOT_APPLICABLE
    assert {check.id: check.status for check in report.checks}[
        "runtime_authentication"
    ] is Status.NOT_APPLICABLE
    assert report.eligible_items == (EligibleItem(2, "Two"), EligibleItem(9, "Nine"))
    assert report.frozen_inputs is not None
    assert (
        report.frozen_inputs.base_commit == "0123456789abcdef0123456789abcdef01234567"
    )
    frozen_setup = next(
        file
        for file in report.frozen_inputs.project_fixture.files
        if file.relative_path == "setup"
    )
    (fixture / "setup").write_text("#!/bin/sh\nexit 9\n")
    assert frozen_setup.content == b"#!/bin/sh\nexit 0\n"
    assert not any(call[-1] == "--check-tools" for call in runner.calls)
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


def test_fixture_structure_accepts_out_of_order_runtime_and_gate_nodes(
    tmp_path: Path,
) -> None:
    fixture = _valid_fixture(tmp_path)
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,runtime_node,gate_node\n"
        "run=build_run(item=execution_graph(start='work',nodes=["
        "gate_node('verify'),runtime_node('work','item.md',on_success='verify')]))\n"
    )

    result = DefaultReadinessAdapter(fixture, tmp_path).check_fixture_structure(
        configuration()
    )

    assert result.status is Status.PASS


def test_github_repository_requires_matching_identity(tmp_path: Path) -> None:
    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 0, '{"nameWithOwner":"other/repo"}', ""
        )

    result = DefaultReadinessAdapter(
        tmp_path, tmp_path, runner=runner
    ).check_github_repository(configuration())

    assert result.status is Status.FAIL


def test_base_branch_fails_when_github_default_could_not_be_resolved(
    tmp_path: Path,
) -> None:
    config = ReadinessConfiguration(
        **{**configuration().__dict__, "github_default_branch": None}
    )

    result = DefaultReadinessAdapter(
        tmp_path,
        tmp_path,
        runner=lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "", ""),
    ).check_base_branch(config)

    assert result.status is Status.FAIL
    assert "default" in result.summary.lower()


def test_doctor_human_and_json_outputs_are_complete_and_single_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda check_id, _configuration: CheckResult(
                Status.FAIL if check_id == "working_tree" else Status.PASS,
                "checkout dirty" if check_id == "working_tree" else "ready",
                remediation="clean checkout" if check_id == "working_tree" else None,
            ),
            eligible_items=lambda _configuration: [EligibleItem(4, "Unicode ✓")],
        ),
    )
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)

    assert cli.main(["doctor", "--runtime", "stub", "--sandbox", "host"]) == 1
    human = capsys.readouterr().out
    assert human == render_human(report) + "\n"
    assert all(check_id in human for check_id in CHECK_IDS)
    assert "Fix: clean checkout" in human
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


def docker_configuration(
    runtime: str = "claude", image: str = "example/agent:ready"
) -> ReadinessConfiguration:
    return ReadinessConfiguration(
        **{
            **configuration().__dict__,
            "runtime": runtime,
            "sandbox": "docker",
            "agent_image": image,
        }
    )


class DockerRecordingRunner:
    def __init__(self, *, fail_gate: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.fail_gate = fail_gate

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        call = tuple(argv)
        self.calls.append((call, kwargs))
        returncode = 1 if self.fail_gate and "--check-tools" in call else 0
        return subprocess.CompletedProcess(argv, returncode, "", "")


def test_cleanup_failure_is_safe_and_does_not_expose_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    diagnostics: list[str] = []
    adapter = DefaultReadinessAdapter(
        tmp_path, tmp_path, cleanup_reporter=diagnostics.append
    )
    disposable = adapter._readiness_workspace()
    monkeypatch.setattr(
        "pycastle.readiness.shutil.rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("credential path")),
    )

    adapter.close()

    assert diagnostics == ["Doctor cleanup could not complete."]
    assert str(disposable) not in diagnostics[0]
    assert adapter._docker_workspace is None


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_docker_authentication_uses_resolved_image_and_canonical_auth_volume(
    tmp_path: Path, runtime: str
) -> None:
    runner = DockerRecordingRunner()
    config = docker_configuration(runtime, "registry.example/agent:digest")
    adapter = DefaultReadinessAdapter(tmp_path, tmp_path, runner=runner)

    result = adapter.check_runtime_authentication(config)

    assert result.status is Status.PASS
    call, kwargs = runner.calls[0]
    assert call == tuple(
        sandbox.build_status_command(runtime, image=config.agent_image)
    )
    assert kwargs == {"cwd": tmp_path, "capture": True, "timeout": 15.0}
    other = "codex" if runtime == "claude" else "claude"
    assert sandbox.auth_volume(other) not in call


def test_unknown_runtime_authentication_fails_without_running_command(
    tmp_path: Path,
) -> None:
    runner = DockerRecordingRunner()
    config = docker_configuration(runtime="unknown")
    adapter = DefaultReadinessAdapter(tmp_path, tmp_path, runner=runner)

    result = adapter.check_runtime_authentication(config)

    assert result.status is Status.FAIL
    assert result.summary == "The selected Runtime has no authentication convention."
    assert runner.calls == []


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
    [(0, "line one\nline two"), (0, "x" * 101)],
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

    assert calls == [
        check_id
        for check_id in CHECK_IDS
        if check_id not in {"eligible_items", "frozen_execution_inputs"}
    ]
    assert [check.id for check in report.checks] == list(CHECK_IDS)
    assert report.outcome is ReadinessOutcome.READY
    document = json.loads(render_json(report))
    assert list(document) == [
        "schema_version",
        "outcome",
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
    assert report.outcome is ReadinessOutcome.NOT_READY


def test_zero_items_is_a_successful_no_work_outcome() -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=lambda _configuration: [],
        ),
    )
    check = next(check for check in report.checks if check.id == "eligible_items")
    assert check.id == "eligible_items"
    assert check.status is Status.PASS
    assert report.outcome is ReadinessOutcome.NO_WORK


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

    check = next(check for check in report.checks if check.id == "eligible_items")
    assert check.id == "eligible_items"
    assert check.status is Status.FAIL
    assert report.eligible_items == ()
    assert report.outcome is ReadinessOutcome.NOT_READY


@pytest.mark.parametrize(
    "items",
    [
        None,
        (EligibleItem(1, "One"),),
        [EligibleItem(0, "Zero")],
        [EligibleItem(-1, "Negative")],
        [EligibleItem(True, "Boolean")],
        [EligibleItem("1", "String")],
        [EligibleItem(1, None)],
    ],
)
def test_invalid_item_values_make_doctor_unready(items: object) -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=lambda _configuration: items,  # type: ignore[return-value]
        ),
    )

    assert (
        next(check for check in report.checks if check.id == "eligible_items").status
        is Status.FAIL
    )
    assert report.eligible_items == ()
    assert report.outcome is ReadinessOutcome.NOT_READY


def test_invalid_probe_result_becomes_a_failed_check_and_evaluation_continues() -> None:
    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        if check_id == "working_tree":
            return None  # type: ignore[return-value]
        return CheckResult(Status.PASS, "Ready")

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=probe,
            eligible_items=lambda _configuration: [EligibleItem(1, "One")],
        ),
    )
    by_id = {check.id: check for check in report.checks}

    assert by_id["working_tree"].status is Status.FAIL
    assert by_id["base_branch"].status is Status.PASS
    assert len(report.checks) == len(CHECK_IDS)


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


def test_runtime_timeout_is_a_failure_and_blocks_only_authentication(
    tmp_path: Path,
) -> None:
    argv = ("codex", "--version")
    adapter = DefaultReadinessAdapter(
        tmp_path,
        tmp_path,
        runner=ScriptedRuntimeRunner(
            {argv: subprocess.TimeoutExpired(argv, 15, output="credential=secret")}
        ),
    )

    result = adapter.check_runtime(runtime_configuration("codex"))

    assert result.status is Status.FAIL
    assert "secret" not in repr(result)


def test_report_drops_unknown_and_oversized_facts() -> None:
    secret = "credential=do-not-report"
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda check_id, _configuration: CheckResult(
                Status.PASS,
                "Ready",
                {
                    "raw_output": secret,
                    "version": "v" * 1000,
                    "commands": ["git", secret],
                    "missing": ["gh"],
                },
            ),
            eligible_items=lambda _configuration: [EligibleItem(1, "Safe")],
        ),
    )

    rendered = render_json(report)
    assert secret not in rendered
    assert "raw_output" not in rendered
    assert len(rendered) < 20_000


def test_report_rejects_hostile_resolved_configuration_values() -> None:
    secret = "credential=do-not-report"
    config = ReadinessConfiguration(
        **{
            **configuration().__dict__,
            "repository": secret,
            "base_branch": "main\x1b[2J" + secret,
            "assignee": secret,
        }
    )
    report = evaluate_readiness(
        config,
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=lambda _configuration: [EligibleItem(1, "One")],
        ),
    )

    assert secret not in render_json(report)
    assert secret not in render_human(report)


def test_keyboard_interrupt_stops_evaluation_without_a_partial_report() -> None:
    calls: list[str] = []

    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        calls.append(check_id)
        if check_id == "working_tree":
            raise KeyboardInterrupt
        return CheckResult(Status.PASS, "Ready")

    with pytest.raises(KeyboardInterrupt):
        evaluate_readiness(
            configuration(),
            ReadinessDependencies(probe=probe, eligible_items=lambda _config: []),
        )

    assert calls == ["required_commands", "git_repository", "working_tree"]


def test_progress_is_concise_and_deterministically_ordered() -> None:
    events: list[tuple[str, str, Status | None]] = []
    evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=lambda _configuration: [EligibleItem(1, "One")],
        ),
        progress=lambda *event: events.append(event),
    )

    assert events[::2] == [("start", check_id, None) for check_id in CHECK_IDS]
    assert [event[1] for event in events[1::2]] == list(CHECK_IDS)


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


@pytest.mark.parametrize(
    ("assignee", "list_returncode"),
    [
        ("", 0),  # Failed `@me` resolution must not look like zero matching Items.
        ("octocat", 1),  # Failed `gh issue list` must not look like an empty list.
    ],
)
def test_external_item_resolution_failures_are_not_empty_batch_noops(
    tmp_path: Path,
    assignee: str,
    list_returncode: int,
) -> None:
    config = ReadinessConfiguration(
        **{**configuration().__dict__, "assignee": assignee}
    )

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            list_returncode,
            "[]" if list_returncode == 0 else "",
            "GitHub query failed" if list_returncode else "",
        )

    adapter = DefaultReadinessAdapter(
        tmp_path,
        tmp_path,
        runner=runner,
        include_item_content=True,
    )
    report = evaluate_readiness(
        config,
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "ready"),
            eligible_items=adapter.eligible_items,
        ),
    )
    eligible = next(check for check in report.checks if check.id == "eligible_items")

    assert eligible.status is Status.FAIL
    assert eligible.facts.get("count") is None
    assert report.outcome is ReadinessOutcome.NOT_READY


@pytest.mark.parametrize(
    ("eligible_items", "selected_items"),
    [
        ((EligibleItem(1, "One"),), ()),
        (
            (EligibleItem(1, "One"), EligibleItem(2, "Two")),
            (IssueRef(number=1, title="One"),),
        ),
        (
            (EligibleItem(1, "One"),),
            (IssueRef(number=1, title="Different"),),
        ),
    ],
)
def test_run_rejects_incomplete_or_mismatched_frozen_batch_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    eligible_items: tuple[EligibleItem, ...],
    selected_items: tuple[IssueRef, ...],
) -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "ready"),
            eligible_items=lambda _configuration: list(eligible_items),
        ),
    )
    object.__setattr__(report, "selected_items", selected_items)

    side_effect = MagicMock(side_effect=AssertionError("Run side effect started"))
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(cli, "_make_run_id", side_effect)
    monkeypatch.setattr(cli, "_build_runtime", side_effect)
    monkeypatch.setattr(cli, "run_loop", side_effect)

    args = cli.build_parser().parse_args(["run", "--runtime", "stub"])
    args.sandbox = "host"
    assert cli._cmd_run(args) == 1
    side_effect.assert_not_called()


def test_readiness_freezes_full_items_but_reports_only_safe_metadata() -> None:
    item = IssueRef(
        number=7,
        title="Seven",
        body="private body",
        assignees=["octocat"],
    )
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "ready"),
            eligible_items=lambda _configuration: [item],
        ),
    )

    item.body = "mutated"
    item.assignees.append("someone")

    assert report.eligible_items == (EligibleItem(7, "Seven"),)
    assert report.selected_items[0].body == "private body"
    assert report.selected_items[0].assignees == ["octocat"]
    document = json.loads(render_json(report))
    assert document["eligible_items"] == [{"number": 7, "title": "Seven"}]
    assert "selected_items" not in document


def test_run_main_does_not_use_legacy_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "check_required_commands",
        lambda _commands: pytest.fail("Run bypassed the readiness evaluator"),
    )
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 23)

    assert cli.main(["run", "--runtime", "stub", "--sandbox", "host"]) == 23


def test_run_re_evaluates_after_doctor_and_freezes_only_current_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def snapshot(number: int) -> object:
        def freeze(config: object, items: tuple[IssueRef, ...]) -> object:
            frozen = MagicMock()
            frozen.items = items
            frozen.sandbox = config.sandbox
            frozen.runtime = config.runtime
            frozen.agent_image = config.agent_image
            return frozen

        return evaluate_readiness(
            configuration(),
            ReadinessDependencies(
                probe=lambda _id, _configuration: CheckResult(Status.PASS, "ready"),
                eligible_items=lambda _configuration: [
                    IssueRef(number=number, title=f"Item {number}")
                ],
                freeze_inputs=freeze,
            ),
        )

    evaluations = MagicMock(side_effect=[snapshot(1), snapshot(2)])
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", evaluations)
    monkeypatch.setattr(cli, "_build_runtime", MagicMock())
    monkeypatch.setattr(cli, "GitHubIssueSource", MagicMock())
    monkeypatch.setattr(cli, "_make_run_id", lambda: "current")
    run_loop = MagicMock()
    run_loop.return_value = MagicMock(
        selected=[2],
        issues=[],
        completed=[],
        pr_opened=True,
        succeeded=True,
        run_id="current",
    )
    monkeypatch.setattr(cli, "run_loop", run_loop)

    doctor_args = cli.build_parser().parse_args(
        ["doctor", "--runtime", "stub", "--sandbox", "host"]
    )
    run_args = cli.build_parser().parse_args(
        ["run", "--runtime", "stub", "--sandbox", "host"]
    )

    assert cli._cmd_doctor(doctor_args) == 0
    assert cli._cmd_run(run_args) == 0
    assert evaluations.call_count == 2
    selected = run_loop.call_args.kwargs["selected"]
    assert [item.number for item in selected] == [2]
