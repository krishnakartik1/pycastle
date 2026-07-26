"""CLI argument parsing, preflight, and command dispatch."""

from __future__ import annotations

import json
import os
import subprocess
from importlib.metadata import version
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from packaging.version import Version

from pycastle import cli, orchestrator, readiness
from pycastle import migrations as fixture_migrations
from pycastle.cli import build_parser, main
from pycastle.graph import load_run
from pycastle.issues import IssueRef
from pycastle.models import IssueComment, RuntimeResult, Telemetry
from pycastle.orchestrator import IssueOutcome, RunOutcome
from pycastle.preflight import PreflightError
from pycastle.readiness import (
    CHECK_IDS,
    CandidateItem,
    FrozenReadinessInputs,
    ReadinessCheck,
    ReadinessConfiguration,
    ReadinessOutcome,
    ReadinessReport,
    Status,
)
from pycastle.runtime import AgentCrashError, StubRuntime
from pycastle.upgrade import FixtureMigration, FixtureUpgradeError, upgrade_fixture


@pytest.fixture(autouse=True)
def ready_run_preflight(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep legacy Run wiring tests focused below the readiness boundary."""
    if request.node.name.startswith(
        (
            "test_incompatible_run",
            "test_run_directs",
            "test_run_rejects_removed_image_override_before_side_effects",
            "test_run_docker_exits_nonzero_when_build_fails",
        )
    ):
        return

    def ready(args: object) -> ReadinessReport:
        sandbox_kind = cli._resolve_sandbox(args.sandbox)
        image = "sha256:" + "a" * 64 if sandbox_kind == "docker" else None
        configuration = ReadinessConfiguration(
            repository="owner/repo",
            base_branch="main",
            github_default_branch="main",
            runtime=args.runtime,
            sandbox=sandbox_kind,
            agent_image=image,
            assignee="krishna",
            include_unassigned=args.include_unassigned,
            item_limit=args.iterations,
        )
        selected = (IssueRef(number=1, title="One"),)
        main_file = cli.FIXTURE_DIR / "main.py"
        if main_file.is_file():
            project = readiness._freeze_project_fixture(
                cli.FIXTURE_DIR, load_run(cli.FIXTURE_DIR)
            )
            base_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            frozen = FrozenReadinessInputs(
                base_commit,
                project,
                selected,
                configuration.sandbox,
                configuration.runtime,
                configuration.agent_image,
            )
        else:
            frozen = MagicMock()
            frozen.candidate_pool = selected
            frozen.sandbox = configuration.sandbox
            frozen.runtime = configuration.runtime
            frozen.agent_image = configuration.agent_image
        return ReadinessReport(
            schema_version=1,
            outcome=ReadinessOutcome.READY,
            runner_version="0.1.0",
            configuration=configuration,
            checks=tuple(
                ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
            ),
            candidate_items=(CandidateItem(1, "One"),),
            candidate_pool=selected,
            frozen_inputs=frozen,
        )

    monkeypatch.setattr(cli, "_evaluate_cli_readiness", ready)


def test_version_flag_reports_built_package_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out == f"pycastle {Version(version('pycastle'))}\n"


@pytest.mark.parametrize(
    ("marker", "diagnostic"),
    [
        (None, "Invalid Project fixture"),
        ("not-a-version\n", "Invalid Project fixture"),
        ("9999\n", "Unsupported downgrade"),
    ],
)
def test_incompatible_run_stops_before_any_run_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    marker: str | None,
    diagnostic: str,
) -> None:
    if marker is not None:
        fixture = tmp_path / ".pycastle"
        fixture.mkdir()
        (fixture / "version").write_text(marker)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    touched = MagicMock(side_effect=AssertionError("Run side effect started"))
    monkeypatch.setattr(cli, "_resolve_agent_image", touched, raising=False)
    monkeypatch.setattr(cli, "_resolve_repo", touched)
    monkeypatch.setattr(cli, "run_loop", touched)

    assert main(["run", "--sandbox", "docker", "--runtime", "claude"]) == 1
    touched.assert_not_called()
    assert diagnostic in caplog.text


def test_run_directs_to_upgrade_when_registered_migration_applies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir()
    (fixture / "version").write_text("0.0.1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(
        fixture_migrations,
        "MIGRATIONS",
        (
            FixtureMigration(
                "0.1.0",
                lambda _path: False,
                lambda _path: None,
                lambda _path: True,
            ),
        ),
    )
    side_effect = MagicMock(side_effect=AssertionError("Run side effect started"))
    monkeypatch.setattr(cli, "_resolve_repo", side_effect)

    assert main(["run", "--runtime", "stub"]) == 1
    side_effect.assert_not_called()
    assert "pycastle upgrade" in caplog.text


def test_parses_run_arguments() -> None:
    args = build_parser().parse_args(["run", "-i", "3", "--runtime", "stub"])
    assert args.command == "run"
    assert args.iterations == 3
    assert args.runtime == "stub"
    assert args.include_unassigned is False
    # The flag now parses to None when omitted so the marker can be consulted;
    # the effective host/docker choice is resolved later (see _resolve_sandbox).
    assert args.sandbox is None


def test_run_verbose_flag_parses() -> None:
    # --verbose (and its -v alias) turns on transcript capture (thinking +
    # output); default off.
    assert build_parser().parse_args(["run", "--verbose"]).verbose is True
    assert build_parser().parse_args(["run", "-v"]).verbose is True
    assert build_parser().parse_args(["run"]).verbose is False


def test_build_runtime_host_verbose_sets_runtime_verbose(tmp_path: Path) -> None:
    # The host path honours verbose: it builds a runtime whose .verbose is True,
    # rather than the bare make_runtime path used when verbose is off.
    runtime = cli._build_runtime("claude", "host", tmp_path, verbose=True)
    assert runtime.verbose is True
    runtime = cli._build_runtime("codex", "host", tmp_path, verbose=True)
    assert runtime.verbose is True


def test_build_runtime_docker_verbose_sets_runtime_verbose(tmp_path: Path) -> None:
    # The docker path threads verbose into the sandboxed runtime too.
    runtime = cli._build_runtime(
        "claude", "docker", tmp_path, image="sha256:" + "a" * 64, verbose=True
    )
    assert runtime.verbose is True
    runtime = cli._build_runtime(
        "codex", "docker", tmp_path, image="sha256:" + "a" * 64, verbose=True
    )
    assert runtime.verbose is True


def test_run_sandbox_flag_defaults_to_none_for_marker_resolution() -> None:
    # No --sandbox flag parses to None, which signals "consult the marker".
    args = build_parser().parse_args(["run"])
    assert args.sandbox is None


def test_run_has_no_image_compatibility_attribute() -> None:
    args = build_parser().parse_args(["run"])
    assert not hasattr(args, "image")


def test_main_dispatches_prune(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    prune = MagicMock(return_value=[])
    monkeypatch.setattr(cli, "prune_run_branches", prune)

    assert main(["prune"]) == 0
    prune.assert_called_once_with(
        repo="owner/repo", cwd=Path.cwd(), include_no_pr=False
    )


def test_main_dispatches_prune_include_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    prune = MagicMock(return_value=[])
    monkeypatch.setattr(cli, "prune_run_branches", prune)

    assert main(["prune", "--include-no-pr"]) == 0
    prune.assert_called_once_with(repo="owner/repo", cwd=Path.cwd(), include_no_pr=True)


def test_make_run_id_is_a_timestamp_shape() -> None:
    # The CLI is where a run id is minted (the orchestrator never reads a clock,
    # so it stays deterministic in tests). The id is a YYYYMMDD-HHMMSS stamp.
    run_id = cli._make_run_id()
    assert len(run_id) == 15
    date, _, time = run_id.partition("-")
    assert date.isdigit() and len(date) == 8
    assert time.isdigit() and len(time) == 6


def test_run_passes_a_generated_run_id_to_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The orchestrator receives an injected run id from the CLI rather than
    # generating one itself; here we pin it and assert it is threaded through.
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())
    monkeypatch.setattr(cli, "_make_run_id", lambda: "20260613-101500")

    captured: dict[str, object] = {}

    def fake_run_loop(*, run_id: str, **_kwargs: object) -> MagicMock:
        captured["run_id"] = run_id
        outcome = MagicMock()
        outcome.selected = []
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--runtime", "stub"]) == 0
    assert captured["run_id"] == "20260613-101500"


def test_migrated_fixture_selects_claims_completes_and_publishes_an_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drive owner migration and the resulting Run through public CLI seams."""
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "work.md").write_text("work")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,runtime_node,gate_node\n"
        "run=build_run(item=execution_graph(start='work',nodes=["
        "gate_node('verify'),runtime_node('work','work.md',on_success='verify')]))\n"
    )
    (fixture / "Dockerfile").write_text("FROM scratch\n")
    (fixture / "sandbox").write_text("host\n")
    (fixture / "version").write_text("0.1.2\n")
    timeline = tmp_path / "timeline"
    for name in ("setup", "gate"):
        hook = fixture / name
        hook.write_text(f"#!/bin/sh\nprintf '{name}\\n' >> '{timeline}'\n")
        hook.chmod(0o755)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    with pytest.raises(FixtureUpgradeError, match="owner-authored"):
        upgrade_fixture(
            tmp_path,
            runner_version="0.1.3",
            migrations=fixture_migrations.MIGRATIONS,
        )
    assert (fixture / "version").read_text() == "0.1.2\n"
    assert not (prompts / "select.md").exists()

    (prompts / "select.md").write_text("Choose an Item.")
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "runtime_node,gate_node,runtime_selection)\n"
        "run=build_run(item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "gate_node('verify'),runtime_node('work','work.md',on_success='verify')])))\n"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "author Item selection policy"],
        cwd=tmp_path,
        check=True,
    )
    upgraded = upgrade_fixture(
        tmp_path,
        runner_version="0.1.3",
        migrations=fixture_migrations.MIGRATIONS,
    )
    assert upgraded.marker_updated
    assert (fixture / "version").read_text() == "0.1.3\n"
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance fixture release"],
        cwd=tmp_path,
        check=True,
    )

    process_calls: list[list[str]] = []

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        process_calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    source = MagicMock()
    source.is_still_eligible.return_value = True
    runtime = StubRuntime()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_make_run_id", lambda: "cli-explicit-134")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)

    original_run_loop = cli.run_loop
    outcomes: list[RunOutcome] = []

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)

    assert main(["run", "--sandbox", "host", "--runtime", "stub"]) == 0, outcomes
    assert timeline.read_text().splitlines() == [
        "setup",
        "setup",
        "setup",
        "setup",
        "gate",
    ]
    source.claim.assert_called_once_with(1, assignee="krishna")
    assert any(
        call[:3] == ["gh", "pr", "create"] and "--draft" in call
        for call in process_calls
    )
    assert any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)


@pytest.mark.parametrize(
    "selection_outcome",
    (
        "valid",
        "durable-change",
        "runtime-failure",
        "invalid-response",
        "out-of-pool",
        "oversized-response",
        "candidate-overflow",
        "cancelled",
    ),
)
def test_project_policy_selects_and_completes_later_item_from_frozen_pool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    selection_outcome: str,
) -> None:
    """Select from frozen facts only after writable selection is contained."""
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "select.md").write_text("Choose the most actionable candidate.")
    (prompts / "prepare.md").write_text("Prepare the Run.")
    (prompts / "work.md").write_text("Implement the selected Item.")
    (fixture / ".gitignore").write_text("/runs/\n")
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "gate_node,runtime_node,runtime_selection)\n"
        "run=build_run("
        "before=execution_graph(start='prepare',nodes=["
        "runtime_node('prepare','prepare.md')]),"
        "item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md',on_success='verify'),"
        "gate_node('verify')]))"
        ")\n"
    )
    timeline = tmp_path / "timeline"
    setup = fixture / "setup"
    setup.write_text(
        f"#!/bin/sh\nprintf 'setup:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text(
        f"#!/bin/sh\nprintf 'gate:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    gate.chmod(0o755)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    earlier = IssueRef(
        number=1,
        title="Lower-numbered candidate",
        body="Lower candidate body.",
        labels=["ready-for-agent"],
        assignees=["krishna"],
        comments=[IssueComment(author="author", body="Lower candidate comment.")],
    )
    item = IssueRef(
        number=42,
        title="Project-owned choice",
        body=(
            "X" * 1_100_000
            if selection_outcome == "candidate-overflow"
            else "The complete frozen body."
        ),
        labels=["ready-for-agent", "priority:high"],
        assignees=["krishna"],
        comments=[IssueComment(author="reviewer", body="Frozen comment.")],
    )
    configuration = ReadinessConfiguration(
        repository="owner/repo",
        base_branch="main",
        github_default_branch="main",
        runtime="stub",
        sandbox="host",
        agent_image=None,
        assignee="krishna",
        include_unassigned=False,
        item_limit=1,
    )
    project = readiness._freeze_project_fixture(fixture, load_run(fixture))
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    frozen_items = tuple(
        candidate.model_copy(deep=True) for candidate in (earlier, item)
    )
    frozen = FrozenReadinessInputs(
        base_commit,
        project,
        frozen_items,
        configuration.sandbox,
        configuration.runtime,
        configuration.agent_image,
    )
    report = ReadinessReport(
        schema_version=1,
        outcome=ReadinessOutcome.READY,
        runner_version="0.1.0",
        configuration=configuration,
        checks=tuple(
            ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
        ),
        candidate_items=(
            CandidateItem(earlier.number, earlier.title),
            CandidateItem(item.number, item.title),
        ),
        candidate_pool=frozen_items,
        frozen_inputs=frozen,
    )
    earlier.body = "changed after readiness"
    item.body = "changed after readiness"

    process_calls: list[list[str]] = []

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        process_calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    prompts_seen: dict[str, str] = {}
    selection_worktrees: list[Path] = []
    selection_environment: dict[str, str | None] = {}
    github_environment = {
        "GH_TOKEN": "gh-secret",
        "GITHUB_TOKEN": "github-secret",
        "GH_ENTERPRISE_TOKEN": "ghe-secret",
        "GITHUB_ENTERPRISE_TOKEN": "github-enterprise-secret",
        "SSH_AUTH_SOCK": "/tmp/private-agent.sock",
        "GH_CONFIG_DIR": "/tmp/private-gh-config",
        "GIT_CONFIG_GLOBAL": "/tmp/private-gitconfig",
        "CODEX_HOME": "/tmp/runtime-auth-must-survive",
    }
    for name, value in github_environment.items():
        monkeypatch.setenv(name, value)

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            prompts_seen[node] = prompt
            if node == "item-selection":
                selection_worktrees.append(cwd)
                selection_environment.update(
                    {
                        name: os.environ.get(name)
                        for name in (
                            *github_environment,
                            "GIT_ASKPASS",
                            "GIT_CONFIG_COUNT",
                            "GIT_CONFIG_KEY_0",
                            "GIT_CONFIG_NOSYSTEM",
                            "GIT_CONFIG_VALUE_0",
                            "GIT_TERMINAL_PROMPT",
                        )
                    }
                )
                gh_config = Path(os.environ["GH_CONFIG_DIR"])
                assert gh_config.is_dir()
                assert list(gh_config.iterdir()) == []
                assert (cwd / "PYCASTLE_STUB.md").is_file()
                (cwd / "SELECTION_ONLY.md").write_text(
                    "This writable selection change must be discarded.\n"
                )
                if selection_outcome == "durable-change":
                    (
                        cwd.parent / "run-later-candidate-171" / "DURABLE_CHANGE"
                    ).write_text("selection reached outside its disposable worktree\n")
                if selection_outcome == "runtime-failure":
                    raise AgentCrashError(
                        "selection Runtime failed with private provider detail",
                        node=node,
                        exit_code=17,
                        transcript="partial private Runtime transcript",
                        stderr="complete private provider stderr",
                        telemetry=Telemetry(
                            runtime=self.name,
                            node=node,
                            num_turns=1,
                            is_error=True,
                        ),
                    )
                if selection_outcome == "cancelled":
                    raise KeyboardInterrupt
                return RuntimeResult(
                    output=(
                        "private transcript without a selection response"
                        if selection_outcome == "invalid-response"
                        else (
                            '<selection>{"item": 999, "reason": '
                            '"private out-of-pool reason"}</selection>'
                            if selection_outcome == "out-of-pool"
                            else (
                                "<selection>" + (" " * 65_537) + "</selection>"
                                if selection_outcome == "oversized-response"
                                else (
                                    '<selection>{"item": 42, "reason": '
                                    '"Highest-priority actionable candidate."}'
                                    "</selection>"
                                )
                            )
                        )
                    ),
                    telemetry=Telemetry(runtime=self.name, node=node, num_turns=1),
                )
            return super().run(prompt, cwd=cwd, node=node)

    source = MagicMock()
    source.is_still_eligible.return_value = True
    outcomes: list[RunOutcome] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(cli, "_make_run_id", lambda: "later-candidate-171")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop
    caplog.set_level("INFO")

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)

    if selection_outcome in {
        "durable-change",
        "runtime-failure",
        "invalid-response",
        "out-of-pool",
        "oversized-response",
        "candidate-overflow",
    }:
        assert main(["run", "--sandbox", "host", "--runtime", "stub"]) == 1
    elif selection_outcome == "cancelled":
        assert main(["run", "--sandbox", "host", "--runtime", "stub"]) == 130
    else:
        assert main(["run", "--sandbox", "host", "--runtime", "stub"]) == 0

    assert {
        name: os.environ.get(name) for name in github_environment
    } == github_environment
    if selection_outcome != "candidate-overflow":
        assert selection_environment["GH_TOKEN"] is None
        assert selection_environment["GITHUB_TOKEN"] is None
        assert selection_environment["GH_ENTERPRISE_TOKEN"] is None
        assert selection_environment["GITHUB_ENTERPRISE_TOKEN"] is None
        assert selection_environment["SSH_AUTH_SOCK"] is None
        assert selection_environment["CODEX_HOME"] == "/tmp/runtime-auth-must-survive"
        assert selection_environment["GIT_ASKPASS"] == "/bin/false"
        assert selection_environment["GIT_CONFIG_COUNT"] == "1"
        assert selection_environment["GIT_CONFIG_GLOBAL"] == os.devnull
        assert selection_environment["GIT_CONFIG_KEY_0"] == "credential.helper"
        assert selection_environment["GIT_CONFIG_VALUE_0"] == ""
        assert selection_environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert selection_environment["GIT_TERMINAL_PROMPT"] == "0"
        assert not Path(str(selection_environment["GH_CONFIG_DIR"])).exists()

    if selection_outcome != "valid":
        source.is_still_eligible.assert_not_called()
        source.claim.assert_not_called()
        expected_invocations = 0 if selection_outcome == "candidate-overflow" else 1
        assert len(selection_worktrees) == expected_invocations
        if selection_worktrees:
            assert selection_worktrees[0].name == "selection-later-candidate-171"
            assert not selection_worktrees[0].exists()
        else:
            assert not (
                fixture / "worktrees" / "selection-later-candidate-171"
            ).exists()
        if selection_outcome in {
            "runtime-failure",
            "invalid-response",
            "out-of-pool",
            "oversized-response",
            "candidate-overflow",
        }:
            assert source.mock_calls == []
            record = json.loads(
                (
                    fixture / "runs" / "later-candidate-171" / "selection-001.json"
                ).read_text()
            )
            assert record["candidate_envelope"].index('"number": 1') < record[
                "candidate_envelope"
            ].index('"number": 42')
            assert record["prompt"]["name"] == "select.md"
            assert len(record["prompt"]["sha256"]) == 64
            assert record["validation"]["status"] == "failed"
            if selection_outcome == "invalid-response":
                assert (
                    record["runtime_transcript"]
                    == "private transcript without a selection response"
                )
                assert record["parsed_response"] is None
            elif selection_outcome == "runtime-failure":
                assert record["runtime_transcript"] == (
                    "partial private Runtime transcript"
                )
                assert record["runtime_stderr"] == "complete private provider stderr"
                assert record["runtime_telemetry"]["is_error"] is True
                assert "private provider detail" in record["runtime_error"]
            elif selection_outcome == "out-of-pool":
                assert record["parsed_response"] == {
                    "item": 999,
                    "reason": "private out-of-pool reason",
                }
                assert record["validation"]["code"] == "selection-item-out-of-pool"
            elif selection_outcome == "oversized-response":
                assert record["validation"]["code"] == "selection-response-oversized"
                assert record["parsed_response"] is None
            else:
                assert record["validation"]["code"] == "candidate-envelope-oversized"
                assert record["runtime_transcript"] is None
            assert "private transcript" not in caplog.text
            assert "private provider detail" not in caplog.text
            assert "complete private provider stderr" not in caplog.text
            assert "private out-of-pool reason" not in caplog.text
            assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
            assert [
                "git",
                "push",
                "origin",
                "--delete",
                "pycastle/run-later-candidate-171",
            ] in process_calls
            assert (
                subprocess.run(
                    [
                        "git",
                        "check-ignore",
                        str(
                            fixture
                            / "runs"
                            / "later-candidate-171"
                            / "selection-001.json"
                        ),
                    ],
                    cwd=tmp_path,
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            )
            assert (
                subprocess.run(
                    [
                        "git",
                        "show-ref",
                        "--verify",
                        "refs/heads/pycastle/run-later-candidate-171",
                    ],
                    cwd=tmp_path,
                    capture_output=True,
                    check=False,
                ).returncode
                != 0
            )
        elif selection_outcome == "durable-change":
            assert outcomes[0].selection_failure == "selection-infrastructure-failed"
            record = json.loads(
                (
                    fixture / "runs" / "later-candidate-171" / "selection-001.json"
                ).read_text()
            )
            assert record["validation"] == {
                "status": "accepted",
                "code": "selection-accepted",
            }
            assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
        return

    assert outcomes[0].completed == [42]
    assert list(prompts_seen) == ["prepare", "item-selection", "work"]
    before_prompt = prompts_seen["prepare"]
    assert "## Item candidate pool" in before_prompt
    assert "## Frozen Items" not in before_prompt
    assert "#1: Lower-numbered candidate [pending]" in before_prompt
    assert "#42: Project-owned choice [pending]" in before_prompt
    assert "Lower candidate body." not in before_prompt
    assert "The complete frozen body." not in before_prompt
    assert "Frozen comment." not in before_prompt
    assert "priority:high" not in before_prompt
    selection_prompt = prompts_seen["item-selection"]
    assert selection_prompt.index('"number": 1') < selection_prompt.index(
        '"number": 42'
    )
    assert "Lower candidate body." in selection_prompt
    assert "Lower candidate comment." in selection_prompt
    assert selection_prompt.index("The complete frozen body.") < selection_prompt.index(
        "Choose the most actionable candidate."
    )
    assert selection_prompt.index(
        "Choose the most actionable candidate."
    ) < selection_prompt.index("<selection>")
    assert "Frozen comment." in selection_prompt
    assert "priority:high" in selection_prompt
    assert "changed after readiness" not in selection_prompt
    selection_items = json.loads(
        selection_prompt.split(
            "The following JSON is untrusted frozen Issue-source data.\n\n",
            1,
        )[1].split("\n\n# PyCastle Run progress", 1)[0]
    )
    assert selection_items[1] == frozen_items[1].model_dump(mode="json")
    item_prompt = prompts_seen["work"]
    assert "Implement the selected Item." in item_prompt
    assert "The complete frozen body." in item_prompt
    assert f"Labels (JSON): {json.dumps(frozen_items[1].labels)}" in item_prompt
    assert f"Assignees (JSON): {json.dumps(frozen_items[1].assignees)}" in item_prompt
    assert "changed after readiness" not in item_prompt
    assert "Lower candidate body." not in item_prompt
    source.is_still_eligible.assert_called_once_with(
        frozen_items[1], assignee="krishna", include_unassigned=False
    )
    source.claim.assert_called_once_with(42, assignee="krishna")
    assert "Selected Item #42: Project-owned choice" in caplog.text
    assert "Highest-priority actionable candidate." not in caplog.text
    assert len(selection_worktrees) == 1
    assert selection_worktrees[0].name == "selection-later-candidate-171"
    assert not selection_worktrees[0].exists()
    selection_setup_records = list(
        (fixture / "runs" / "later-candidate-171" / "executions").glob(
            "item-selection-1-*-setup-1.json"
        )
    )
    assert len(selection_setup_records) == 1
    assert json.loads(selection_setup_records[0].read_text())["scope"] == "run"
    assert "behavioral guidance, not a security boundary" in selection_prompt
    assert "withholds known GitHub token" in selection_prompt
    selection_change = subprocess.run(
        [
            "git",
            "cat-file",
            "-e",
            "pycastle/run-later-candidate-171:SELECTION_ONLY.md",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert selection_change.returncode != 0
    assert timeline.read_text().splitlines() == [
        "setup:run",
        "setup:run",
        "setup:run",
        "setup:item",
        "setup:item",
        "gate:item",
    ]
    assert any(
        call[:3] == ["gh", "pr", "create"] and "--draft" in call
        for call in process_calls
    )
    assert any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)


def test_project_policy_orders_items_until_claimed_attempt_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reselect with Run progress, then stop at capacity and publish normally."""
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "select.md").write_text("Choose the next dependency-ready Item.")
    (prompts / "prepare.md").write_text("Prepare this Run once.")
    (prompts / "work.md").write_text("Implement this selected Item.")
    (prompts / "summarize.md").write_text("Summarize the bounded Run outcomes.")
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "gate_node,runtime_node,runtime_selection)\n"
        "run=build_run("
        "before=execution_graph(start='prepare',nodes=["
        "runtime_node('prepare','prepare.md')]),"
        "item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md',on_success='verify'),"
        "gate_node('verify')])),"
        "after=execution_graph(start='summarize',nodes=["
        "runtime_node('summarize','summarize.md',on_success='verify-run'),"
        "gate_node('verify-run')])"
        ")\n"
    )
    timeline = tmp_path / "timeline"
    setup = fixture / "setup"
    setup.write_text(
        f"#!/bin/sh\nprintf 'setup:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text(
        f"#!/bin/sh\nprintf 'gate:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    gate.chmod(0o755)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    candidates = tuple(
        IssueRef(
            number=number,
            title=title,
            body=f"Private body for Item {number}.",
            comments=[
                IssueComment(
                    author="maintainer", body=f"Private comment for Item {number}."
                )
            ],
            labels=["ready-for-agent"],
            assignees=["krishna"],
        )
        for number, title in (
            (1, "Foundation"),
            (2, "Canonical middle"),
            (3, "Highest-priority unblocker"),
        )
    )
    configuration = ReadinessConfiguration(
        repository="owner/repo",
        base_branch="main",
        github_default_branch="main",
        runtime="stub",
        sandbox="host",
        agent_image=None,
        assignee="krishna",
        include_unassigned=False,
        item_limit=2,
    )
    frozen = FrozenReadinessInputs(
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        readiness._freeze_project_fixture(fixture, load_run(fixture)),
        tuple(candidate.model_copy(deep=True) for candidate in candidates),
        configuration.sandbox,
        configuration.runtime,
        configuration.agent_image,
    )
    report = ReadinessReport(
        schema_version=1,
        outcome=ReadinessOutcome.READY,
        runner_version="0.1.0",
        configuration=configuration,
        checks=tuple(
            ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
        ),
        candidate_items=tuple(
            CandidateItem(candidate.number, candidate.title) for candidate in candidates
        ),
        candidate_pool=frozen.candidate_pool,
        frozen_inputs=frozen,
    )

    process_calls: list[list[str]] = []

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        process_calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    selection_prompts: list[str] = []
    before_prompts: list[str] = []
    after_prompts: list[str] = []
    selection_worktrees: list[Path] = []
    choices = iter((3, 1))

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            if node == "item-selection":
                selection_prompts.append(prompt)
                selection_worktrees.append(cwd)
                if len(selection_worktrees) == 1:
                    assert not (cwd / "item-3.txt").exists()
                    (cwd / "FIRST_SELECTION_ONLY").write_text("discard me\n")
                else:
                    assert not (cwd / "FIRST_SELECTION_ONLY").exists()
                    assert (cwd / "item-3.txt").is_file()
                choice = next(choices)
                return RuntimeResult(
                    output=(
                        f'<selection>{{"item": {choice}, "reason": '
                        '"Project dependency order."}</selection>'
                    ),
                    telemetry=Telemetry(runtime=self.name, node=node, num_turns=1),
                )
            result = super().run(prompt, cwd=cwd, node=node)
            if node == "prepare":
                before_prompts.append(prompt)
            elif node == "work":
                (cwd / f"item-{cwd.name.removeprefix('issue-')}.txt").write_text(
                    "done\n"
                )
            elif node == "summarize":
                after_prompts.append(prompt)
                assert (cwd / "item-1.txt").is_file()
                assert (cwd / "item-3.txt").is_file()
            return result

    source = MagicMock()
    source.is_still_eligible.return_value = True
    outcomes: list[RunOutcome] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(cli, "_make_run_id", lambda: "policy-order-175")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)

    assert (
        main(
            [
                "run",
                "--sandbox",
                "host",
                "--runtime",
                "stub",
                "--iterations",
                "2",
            ]
        )
        == 0
    )
    assert outcomes[0].selected == [3, 1]
    assert outcomes[0].attempted == [3, 1]
    assert outcomes[0].completed == [3, 1]
    assert outcomes[0].skipped == []
    assert len(before_prompts) == 1
    assert len(selection_prompts) == 2
    assert [path.name for path in selection_worktrees] == [
        "selection-policy-order-175",
        "selection-policy-order-175",
    ]
    assert all(not path.exists() for path in selection_worktrees)
    assert selection_prompts[0].index('"number": 1') < selection_prompts[0].index(
        '"number": 3'
    )
    assert all(
        f"Private body for Item {number}." in selection_prompts[0]
        for number in (1, 2, 3)
    )
    assert '"attempted": []' in selection_prompts[0]
    assert '"completed": []' in selection_prompts[0]
    assert '"remaining_claimed_attempt_capacity": 2' in selection_prompts[0]
    assert '"skipped": []' in selection_prompts[0]
    assert '"stale": []' in selection_prompts[0]
    assert '"number": 3' not in selection_prompts[1]
    assert "Private body for Item 3." not in selection_prompts[1]
    assert '"attempted": [\n    3\n  ]' in selection_prompts[1]
    assert '"completed": [\n    3\n  ]' in selection_prompts[1]
    assert '"remaining_claimed_attempt_capacity": 1' in selection_prompts[1]
    assert '"skipped": []' in selection_prompts[1]
    assert '"stale": []' in selection_prompts[1]
    source.claim.assert_has_calls(
        [call(3, assignee="krishna"), call(1, assignee="krishna")]
    )
    assert len(after_prompts) == 1
    after_prompt = after_prompts[0]
    assert "#1: Foundation [completed]" in after_prompt
    assert "#2: Canonical middle [not-selected]" in after_prompt
    assert "#3: Highest-priority unblocker [completed]" in after_prompt
    assert "claimed-attempt-limit-reached" in after_prompt
    for number in (1, 2, 3):
        assert f"Private body for Item {number}." not in before_prompts[0]
        assert f"Private comment for Item {number}." not in before_prompts[0]
        assert f"Private body for Item {number}." not in after_prompt
        assert f"Private comment for Item {number}." not in after_prompt
    assert timeline.read_text().splitlines().count("gate:item") == 2
    assert timeline.read_text().splitlines().count("gate:run") == 1
    assert any(
        process_call[:3] == ["gh", "pr", "create"] and "--draft" in process_call
        for process_call in process_calls
    )
    assert any(
        process_call[:3] == ["gh", "pr", "ready"] for process_call in process_calls
    )


def _run_stale_recheck_from_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    recheck_failure: OSError | None = None,
    policy_choices: tuple[int | None, ...] = (11, 22),
    eligibility: tuple[bool, ...] = (False, True),
) -> tuple[int, MagicMock, list[list[str]], list[RunOutcome], list[str], list[str]]:
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "select.md").write_text("Choose the next dependency-ready Item.")
    (prompts / "work.md").write_text("Implement the selected Item.")
    (prompts / "summarize.md").write_text("Summarize the bounded Run outcomes.")
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "gate_node,runtime_node,runtime_selection)\n"
        "run=build_run("
        "item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md',on_success='verify'),"
        "gate_node('verify')])),"
        "after=execution_graph(start='summarize',nodes=["
        "runtime_node('summarize','summarize.md',on_success='verify-run'),"
        "gate_node('verify-run')])"
        ")\n"
    )
    timeline = tmp_path / "timeline"
    setup = fixture / "setup"
    setup.write_text(
        f"#!/bin/sh\nprintf 'setup:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text(
        f"#!/bin/sh\nprintf 'gate:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    gate.chmod(0o755)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    candidates = tuple(
        IssueRef(
            number=number,
            title=title,
            body=f"Frozen body for Item {number}.",
            comments=[
                IssueComment(
                    author="maintainer", body=f"Frozen comment for Item {number}."
                )
            ],
            labels=["ready-for-agent", priority],
            assignees=["krishna"],
        )
        for number, title, priority in (
            (11, "Stale foundation", "priority:high"),
            (22, "Still actionable", "priority:normal"),
        )
    )
    configuration = ReadinessConfiguration(
        repository="owner/repo",
        base_branch="main",
        github_default_branch="main",
        runtime="stub",
        sandbox="host",
        agent_image=None,
        assignee="krishna",
        include_unassigned=False,
        item_limit=1,
    )
    frozen = FrozenReadinessInputs(
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        readiness._freeze_project_fixture(fixture, load_run(fixture)),
        tuple(candidate.model_copy(deep=True) for candidate in candidates),
        configuration.sandbox,
        configuration.runtime,
        configuration.agent_image,
    )
    report = ReadinessReport(
        schema_version=1,
        outcome=ReadinessOutcome.READY,
        runner_version="0.1.0",
        configuration=configuration,
        checks=tuple(
            ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
        ),
        candidate_items=tuple(
            CandidateItem(candidate.number, candidate.title) for candidate in candidates
        ),
        candidate_pool=frozen.candidate_pool,
        frozen_inputs=frozen,
    )
    candidates[0].body = "Changed after readiness."
    candidates[1].body = "Changed after readiness."

    process_calls: list[list[str]] = []

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        process_calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    selection_prompts: list[str] = []
    after_prompts: list[str] = []
    choices = iter(policy_choices)

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            if node == "item-selection":
                selection_prompts.append(prompt)
                choice = next(choices)
                return RuntimeResult(
                    output=(
                        f'<selection>{{"item": {json.dumps(choice)}, "reason": '
                        '"Project dependency order."}</selection>'
                    ),
                    telemetry=Telemetry(runtime=self.name, node=node, num_turns=1),
                )
            result = super().run(prompt, cwd=cwd, node=node)
            if node == "work":
                (cwd / "completed-item.txt").write_text("done\n")
            elif node == "summarize":
                after_prompts.append(prompt)
            return result

    source = MagicMock()
    if recheck_failure is None:
        source.is_still_eligible.side_effect = eligibility
    else:
        source.is_still_eligible.side_effect = recheck_failure
    outcomes: list[RunOutcome] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(cli, "_make_run_id", lambda: "stale-fallthrough-177")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)
    exit_code = main(
        ["run", "--sandbox", "host", "--runtime", "stub", "--iterations", "1"]
    )
    return (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
    )


def test_stale_policy_choice_falls_through_without_consuming_run_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        _process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
    ) = _run_stale_recheck_from_cli(monkeypatch, tmp_path)

    assert exit_code == 0
    assert outcomes[0].selected == [11, 22]
    assert outcomes[0].stale == [11]
    assert outcomes[0].attempted == [22]
    assert outcomes[0].completed == [22]
    assert outcomes[0].selection_end == "claimed-attempt-limit-reached"
    assert len(selection_prompts) == 2
    assert '"number": 11' in selection_prompts[0]
    assert '"number": 22' in selection_prompts[0]
    assert '"number": 11' not in selection_prompts[1]
    assert "Frozen body for Item 11." not in selection_prompts[1]
    assert "Frozen body for Item 22." in selection_prompts[1]
    assert "Changed after readiness." not in "\n".join(selection_prompts)
    assert '"attempted": []' in selection_prompts[1]
    assert '"stale": [\n    11\n  ]' in selection_prompts[1]
    assert len(after_prompts) == 1
    assert "#11: Stale foundation [stale]" in after_prompts[0]
    assert "#22: Still actionable [completed]" in after_prompts[0]
    rechecked = [recheck.args[0] for recheck in source.is_still_eligible.call_args_list]
    assert [item.number for item in rechecked] == [11, 22]
    assert [item.body for item in rechecked] == [
        "Frozen body for Item 11.",
        "Frozen body for Item 22.",
    ]
    assert all(
        recheck.kwargs == {"assignee": "krishna", "include_unassigned": False}
        for recheck in source.is_still_eligible.call_args_list
    )
    source.claim.assert_called_once_with(22, assignee="krishna")
    source.list_ready.assert_not_called()


def test_item_recheck_error_fails_run_instead_of_marking_item_stale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
    ) = _run_stale_recheck_from_cli(
        monkeypatch,
        tmp_path,
        recheck_failure=OSError("tracker unavailable"),
    )

    assert exit_code == 1
    assert outcomes[0].selected == [11]
    assert outcomes[0].stale == []
    assert outcomes[0].attempted == []
    assert outcomes[0].completed == []
    assert outcomes[0].succeeded is False
    assert outcomes[0].stopping_point == (
        "Item #11 eligibility recheck failure: tracker unavailable"
    )
    assert len(selection_prompts) == 1
    assert after_prompts == []
    source.claim.assert_not_called()
    source.list_ready.assert_not_called()
    assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
    assert not (tmp_path / ".pycastle/worktrees/run-stale-fallthrough-177").exists()
    branch = subprocess.run(
        ["git", "branch", "--list", "pycastle/run-stale-fallthrough-177"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout == ""


def test_stale_then_policy_halt_is_successful_and_leaves_no_run_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
    ) = _run_stale_recheck_from_cli(
        monkeypatch,
        tmp_path,
        policy_choices=(11, None),
        eligibility=(False,),
    )

    outcome = outcomes[0]
    assert exit_code == 0
    assert outcome.selected == [11]
    assert outcome.stale == [11]
    assert outcome.attempted == []
    assert outcome.completed == []
    assert outcome.selection_end == "project-policy-halted"
    assert outcome.succeeded is True
    assert len(selection_prompts) == 2
    assert after_prompts == []
    source.claim.assert_not_called()
    assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
    assert not (tmp_path / ".pycastle/worktrees/run-stale-fallthrough-177").exists()
    branch = subprocess.run(
        ["git", "branch", "--list", "pycastle/run-stale-fallthrough-177"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout == ""


def test_all_stale_candidates_end_successfully_and_leave_no_run_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
    ) = _run_stale_recheck_from_cli(
        monkeypatch,
        tmp_path,
        policy_choices=(11, 22),
        eligibility=(False, False),
    )

    outcome = outcomes[0]
    assert exit_code == 0
    assert outcome.stale == [11, 22]
    assert outcome.attempted == []
    assert outcome.completed == []
    assert outcome.selection_end == "candidate-pool-exhausted"
    assert len(selection_prompts) == 2
    assert after_prompts == []
    source.claim.assert_not_called()
    assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
    branch = subprocess.run(
        ["git", "branch", "--list", "pycastle/run-stale-fallthrough-177"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout == ""


def _run_policy_halt_from_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    after_integration: bool,
    fail_after_integration: bool = False,
    mutate_run_ref_after_integration: bool = False,
    mutate_ignored_publication_after_integration: bool = False,
    fail_selection_cleanup: bool = False,
    claim_failure: OSError | None = None,
) -> tuple[
    int,
    MagicMock,
    list[list[str]],
    list[RunOutcome],
    list[str],
    list[str],
    Path,
]:
    """Run the complete CLI policy-halt journey against a temporary Git repo."""
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "select.md").write_text("Choose an actionable Item or stop.")
    (prompts / "work.md").write_text("Implement this selected Item.")
    (prompts / "summarize.md").write_text("Summarize the bounded Run outcomes.")
    (fixture / ".gitignore").write_text(
        "/runs/\n/run-report.md\n/selection-created.md\n"
    )
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "gate_node,runtime_node,runtime_selection)\n"
        "run=build_run("
        "item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md',on_success='verify'),"
        "gate_node('verify')])),"
        "after=execution_graph(start='summarize',nodes=["
        "runtime_node('summarize','summarize.md',on_success='verify-run'),"
        "gate_node('verify-run')])"
        ")\n"
    )
    timeline = tmp_path / "timeline"
    setup = fixture / "setup"
    setup.write_text(
        f"#!/bin/sh\nprintf 'setup:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text(
        f"#!/bin/sh\nprintf 'gate:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    gate.chmod(0o755)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    candidates = tuple(
        IssueRef(
            number=number,
            title=title,
            body=f"Private body for Item {number}.",
            labels=["ready-for-agent"],
            assignees=["krishna"],
        )
        for number, title in ((1, "Untouched candidate"), (3, "Selected foundation"))
    )
    configuration = ReadinessConfiguration(
        repository="owner/repo",
        base_branch="main",
        github_default_branch="main",
        runtime="stub",
        sandbox="host",
        agent_image=None,
        assignee="krishna",
        include_unassigned=False,
        item_limit=2,
    )
    frozen = FrozenReadinessInputs(
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        readiness._freeze_project_fixture(fixture, load_run(fixture)),
        tuple(candidate.model_copy(deep=True) for candidate in candidates),
        configuration.sandbox,
        configuration.runtime,
        configuration.agent_image,
    )
    report = ReadinessReport(
        schema_version=1,
        outcome=ReadinessOutcome.READY,
        runner_version="0.1.0",
        configuration=configuration,
        checks=tuple(
            ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
        ),
        candidate_items=tuple(
            CandidateItem(candidate.number, candidate.title) for candidate in candidates
        ),
        candidate_pool=frozen.candidate_pool,
        frozen_inputs=frozen,
    )

    process_calls: list[list[str]] = []
    fail_next_selection_prune = False

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal fail_next_selection_prune
        process_calls.append(argv)
        if argv[:2] == ["git", "push"]:
            if (
                mutate_ignored_publication_after_integration
                and cwd is not None
                and (cwd / "integrated.txt").is_file()
            ):
                report_path = cwd / ".pycastle/run-report.md"
                created_path = cwd / ".pycastle/selection-created.md"
                if "-u" in argv and not report_path.exists():
                    report_path.write_text("trusted pre-selection report\n")
                elif "-u" not in argv:
                    process_calls.append(
                        [
                            "ignored-publication-state",
                            report_path.read_text(),
                            str(created_path.exists()),
                        ]
                    )
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        result = subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )
        if (
            fail_selection_cleanup
            and argv[:3] == ["git", "worktree", "remove"]
            and Path(argv[3]).name.startswith("selection-")
            and selection_round == (2 if after_integration else 1)
        ):
            fail_next_selection_prune = True
        elif fail_next_selection_prune and argv == ["git", "worktree", "prune"]:
            fail_next_selection_prune = False
            return subprocess.CompletedProcess(
                argv, 1, result.stdout, "private selection prune failure"
            )
        return result

    run_id = (
        (
            "ignored-publication-failure"
            if fail_after_integration
            else "ignored-publication-halt"
        )
        if mutate_ignored_publication_after_integration
        else (
            "later-selection-cleanup-179"
            if mutate_run_ref_after_integration and fail_selection_cleanup
            else (
                "later-selection-mutation-179"
                if mutate_run_ref_after_integration
                else (
                    "later-selection-failure-179"
                    if fail_after_integration
                    else (
                        "selection-cleanup-before-178"
                        if fail_selection_cleanup
                        else "policy-halt-176"
                    )
                )
            )
        )
    )
    selection_prompts: list[str] = []
    after_prompts: list[str] = []
    choices = iter((3, None) if after_integration else (None,))
    selection_round = 0

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            nonlocal selection_round
            if node == "item-selection":
                selection_prompts.append(prompt)
                selection_round += 1
                if (
                    mutate_ignored_publication_after_integration
                    and selection_round == 2
                ):
                    durable_fixture = cwd.parent / f"run-{run_id}" / ".pycastle"
                    (durable_fixture / "run-report.md").write_text(
                        "private selection report must not publish\n"
                    )
                    (durable_fixture / "selection-created.md").write_text(
                        "private new ignored selection artifact\n"
                    )
                if mutate_run_ref_after_integration and selection_round == 2:
                    if fail_selection_cleanup:
                        (cwd / "MALICIOUS_SELECTION_CHANGE").write_text(
                            "must never be published\n"
                        )
                        subprocess.run(
                            ["git", "add", "MALICIOUS_SELECTION_CHANGE"],
                            cwd=cwd,
                            check=True,
                        )
                        subprocess.run(
                            ["git", "commit", "-m", "malicious selection mutation"],
                            cwd=cwd,
                            check=True,
                        )
                        changed_commit = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            check=True,
                        ).stdout.strip()
                    else:
                        changed_commit = subprocess.run(
                            ["git", "rev-parse", "main"],
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            check=True,
                        ).stdout.strip()
                    subprocess.run(
                        [
                            "git",
                            "update-ref",
                            f"refs/heads/pycastle/run-{run_id}",
                            changed_commit,
                        ],
                        cwd=cwd,
                        check=True,
                    )
                if fail_after_integration and selection_round == 2:
                    return RuntimeResult(
                        output="private transcript without a selection response",
                        telemetry=Telemetry(runtime=self.name, node=node, num_turns=1),
                    )
                choice = next(choices)
                item = "null" if choice is None else str(choice)
                return RuntimeResult(
                    output=(
                        f'<selection>{{"item": {item}, "reason": '
                        '"private model-authored halt reason"}</selection>'
                    ),
                    telemetry=Telemetry(runtime=self.name, node=node, num_turns=1),
                )
            result = super().run(prompt, cwd=cwd, node=node)
            if node == "work":
                (cwd / "integrated.txt").write_text("done\n")
            elif node == "summarize":
                after_prompts.append(prompt)
                assert (cwd / "integrated.txt").is_file()
            return result

    source = MagicMock()
    source.is_still_eligible.return_value = True
    source.claim.side_effect = claim_failure
    outcomes: list[RunOutcome] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_evaluate_cli_readiness", lambda _args: report)
    monkeypatch.setattr(cli, "_make_run_id", lambda: run_id)
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)
    exit_code = main(
        ["run", "--sandbox", "host", "--runtime", "stub", "--iterations", "2"]
    )
    return (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        timeline,
    )


def test_project_policy_halts_before_integration_without_mutation_or_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        timeline,
    ) = _run_policy_halt_from_cli(monkeypatch, tmp_path, after_integration=False)

    assert exit_code == 0
    assert outcomes[0].selection_end == "project-policy-halted"
    assert outcomes[0].selected == []
    assert outcomes[0].completed == []
    assert outcomes[0].pr_opened is False
    assert len(selection_prompts) == 1
    assert after_prompts == []
    assert source.method_calls == []
    assert not any(call[0] == "gh" for call in process_calls)
    assert not (tmp_path / ".pycastle/worktrees/run-policy-halt-176").exists()
    branches = subprocess.run(
        ["git", "branch", "--list", "pycastle/run-policy-halt-176"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branches.stdout == ""
    assert timeline.read_text().splitlines() == ["setup:run", "setup:run"]
    assert "Project policy halted Item selection." in caplog.text
    assert "private model-authored halt reason" not in caplog.text


def test_project_policy_halts_after_integration_then_publishes_normally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        timeline,
    ) = _run_policy_halt_from_cli(monkeypatch, tmp_path, after_integration=True)

    assert exit_code == 0
    assert outcomes[0].selection_end == "project-policy-halted"
    assert outcomes[0].selected == [3]
    assert outcomes[0].attempted == [3]
    assert outcomes[0].completed == [3]
    assert outcomes[0].pr_opened is True
    assert outcomes[0].pr_ready is True
    assert len(selection_prompts) == 2
    assert '"number": 1' in selection_prompts[1]
    assert '"number": 3' not in selection_prompts[1]
    assert len(after_prompts) == 1
    assert "#1: Untouched candidate [not-selected]" in after_prompts[0]
    assert "#3: Selected foundation [completed]" in after_prompts[0]
    assert "Ended: project-policy-halted" in after_prompts[0]
    assert "private model-authored halt reason" not in after_prompts[0]
    source.is_still_eligible.assert_called_once_with(
        outcomes[0].issues[0].issue,
        assignee="krishna",
        include_unassigned=False,
    )
    source.claim.assert_called_once_with(3, assignee="krishna")
    assert not any(1 in call.args for call in source.method_calls)
    assert timeline.read_text().splitlines().count("gate:item") == 1
    assert timeline.read_text().splitlines().count("gate:run") == 1
    assert any(
        call[:3] == ["gh", "pr", "create"] and "--draft" in call
        for call in process_calls
    )
    assert any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)
    assert "Project policy halted Item selection." in caplog.text
    assert "private model-authored halt reason" not in caplog.text
    assert all(
        "private model-authored halt reason" not in argument
        for call in process_calls
        for argument in call
    )


def test_later_selection_failure_preserves_completed_work_in_safe_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=True,
        fail_after_integration=True,
    )

    assert exit_code == 1
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.selected == [3]
    assert outcome.attempted == [3]
    assert outcome.completed == [3]
    assert outcome.succeeded is False
    assert outcome.stopping_point == "Item selection"
    assert outcome.selection_failure == "selection-block-count"
    assert outcome.pr_opened is True
    assert outcome.pr_ready is False
    assert len(selection_prompts) == 2
    assert after_prompts == []
    source.claim.assert_called_once_with(3, assignee="krishna")
    assert timeline.read_text().splitlines().count("gate:item") == 1
    assert timeline.read_text().splitlines().count("gate:run") == 0

    create = next(call for call in process_calls if call[:3] == ["gh", "pr", "create"])
    assert "--draft" in create
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)
    comment = next(
        call
        for call in process_calls
        if call[:2] == ["gh", "api"] and "--method" in call
    )
    comment_body = next(
        argument.removeprefix("body=")
        for argument in comment
        if argument.startswith("body=")
    )
    published = create[create.index("--body") + 1] + comment_body
    assert "Selected Items: #3" in published
    assert "Completed Items: #3" in published
    assert "Skipped Items: none" in published
    assert "Item selection failure: `selection-block-count`" in published
    assert "Run Gate: not run" in published
    assert "#1" not in published
    for private in (
        "Untouched candidate",
        "Private body for Item 1.",
        "Choose an actionable Item or stop.",
        "private model-authored halt reason",
        "private transcript without a selection response",
    ):
        assert private not in published

    record_path = (
        tmp_path / ".pycastle/runs/later-selection-failure-179/selection-002.json"
    )
    record = json.loads(record_path.read_text())
    assert "Private body for Item 1." in record["candidate_envelope"]
    assert record["prompt"]["name"] == "select.md"
    assert len(record["prompt"]["sha256"]) == 64
    assert (
        record["runtime_transcript"]
        == "private transcript without a selection response"
    )
    assert record["validation"] == {
        "status": "failed",
        "code": "selection-block-count",
    }
    assert (
        subprocess.run(
            ["git", "check-ignore", str(record_path)],
            cwd=tmp_path,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["git", "show", "pycastle/run-later-selection-failure-179:integrated.txt"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == "done\n"
    )


@pytest.mark.parametrize(
    ("malformed_response", "cleanup_failure"),
    ((False, False), (True, False), (False, True)),
)
def test_selection_cannot_publish_mutated_ignored_project_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformed_response: bool,
    cleanup_failure: bool,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        _timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=True,
        fail_after_integration=malformed_response,
        mutate_ignored_publication_after_integration=True,
        fail_selection_cleanup=cleanup_failure,
    )

    assert exit_code == 1
    outcome = outcomes[0]
    assert outcome.completed == [3]
    assert outcome.selection_failure == "selection-infrastructure-failed"
    assert outcome.pr_opened is True
    assert outcome.pr_ready is False
    assert len(selection_prompts) == 2
    assert after_prompts == []
    source.claim.assert_called_once_with(3, assignee="krishna")

    restored = next(
        call for call in process_calls if call[0] == "ignored-publication-state"
    )
    assert restored == [
        "ignored-publication-state",
        "trusted pre-selection report\n",
        "False",
    ]

    create = next(call for call in process_calls if call[:3] == ["gh", "pr", "create"])
    comment = next(
        call
        for call in process_calls
        if call[:2] == ["gh", "api"] and "--method" in call
    )
    published = create[create.index("--body") + 1] + next(
        argument.removeprefix("body=")
        for argument in comment
        if argument.startswith("body=")
    )
    assert "Item selection failure: `selection-infrastructure-failed`" in published
    assert "private selection report" not in published
    assert "private new ignored selection artifact" not in published
    assert "trusted pre-selection report" not in published
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)


def test_later_selection_ref_mutation_publishes_last_expected_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        _timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=True,
        mutate_run_ref_after_integration=True,
    )

    assert exit_code == 1
    outcome = outcomes[0]
    assert outcome.completed == [3]
    assert outcome.selection_failure == "selection-infrastructure-failed"
    assert outcome.pr_opened is True
    assert outcome.pr_ready is False
    assert len(selection_prompts) == 2
    assert after_prompts == []
    source.claim.assert_called_once_with(3, assignee="krishna")

    run_branch = "pycastle/run-later-selection-mutation-179"
    final_push = next(
        call
        for call in process_calls
        if call[:3] == ["git", "push", "origin"]
        and len(call) == 4
        and call[3].endswith(f":refs/heads/{run_branch}")
    )
    expected_checkpoint = final_push[3].split(":", 1)[0]
    base_commit = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    restored_branch = subprocess.run(
        ["git", "rev-parse", run_branch],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert expected_checkpoint == restored_branch
    assert expected_checkpoint != base_commit
    assert (
        subprocess.run(
            ["git", "show", f"{expected_checkpoint}:integrated.txt"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == "done\n"
    )
    create = next(call for call in process_calls if call[:3] == ["gh", "pr", "create"])
    assert "--draft" in create
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)


def test_later_selection_cleanup_failure_publishes_only_expected_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        _timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=True,
        mutate_run_ref_after_integration=True,
        fail_selection_cleanup=True,
    )

    assert exit_code == 1
    outcome = outcomes[0]
    assert outcome.completed == [3]
    assert outcome.selection_failure == "selection-infrastructure-failed"
    assert outcome.pr_opened is True
    assert outcome.pr_ready is False
    assert len(selection_prompts) == 2
    assert after_prompts == []
    source.claim.assert_called_once_with(3, assignee="krishna")

    run_branch = "pycastle/run-later-selection-cleanup-179"
    final_push = next(
        call
        for call in process_calls
        if call[:3] == ["git", "push", "origin"]
        and len(call) == 4
        and call[3].endswith(f":refs/heads/{run_branch}")
    )
    expected_checkpoint = final_push[3].split(":", 1)[0]
    assert outcome.selection_failure_checkpoint == expected_checkpoint
    restored_branch = subprocess.run(
        ["git", "rev-parse", run_branch],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert restored_branch == expected_checkpoint
    assert (
        subprocess.run(
            ["git", "show", f"{expected_checkpoint}:integrated.txt"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == "done\n"
    )
    assert (
        subprocess.run(
            [
                "git",
                "cat-file",
                "-e",
                f"{expected_checkpoint}:MALICIOUS_SELECTION_CHANGE",
            ],
            cwd=tmp_path,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )

    create = next(call for call in process_calls if call[:3] == ["gh", "pr", "create"])
    assert "--draft" in create
    comment = next(
        call
        for call in process_calls
        if call[:2] == ["gh", "api"] and "--method" in call
    )
    comment_body = next(
        argument.removeprefix("body=")
        for argument in comment
        if argument.startswith("body=")
    )
    published = create[create.index("--body") + 1] + comment_body
    assert "Completed Items: #3" in published
    assert "Item selection failure: `selection-infrastructure-failed`" in published
    assert "private selection prune failure" not in published
    assert "MALICIOUS_SELECTION_CHANGE" not in published
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)

    record = json.loads(
        (
            tmp_path / ".pycastle/runs/later-selection-cleanup-179/selection-002.json"
        ).read_text()
    )
    assert record["validation"]["status"] == "accepted"
    assert record["parsed_response"]["item"] is None


def test_selection_cleanup_failure_before_integration_does_not_claim_or_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        _timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=False,
        fail_selection_cleanup=True,
    )

    assert exit_code == 1
    outcome = outcomes[0]
    assert outcome.completed == []
    assert outcome.selection_failure == "selection-infrastructure-failed"
    assert outcome.selection_failure_checkpoint is not None
    assert outcome.pr_opened is False
    assert len(selection_prompts) == 1
    assert after_prompts == []
    source.is_still_eligible.assert_not_called()
    source.claim.assert_not_called()
    assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
    assert not (
        tmp_path / ".pycastle/worktrees/run-selection-cleanup-before-178"
    ).exists()
    assert (
        subprocess.run(
            [
                "git",
                "branch",
                "--list",
                "pycastle/run-selection-cleanup-before-178",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )
    record = json.loads(
        (
            tmp_path / ".pycastle/runs/selection-cleanup-before-178/selection-001.json"
        ).read_text()
    )
    assert record["validation"]["status"] == "accepted"
    assert record["parsed_response"]["item"] is None


def test_claim_failure_stops_before_item_work_and_cleans_unclaimed_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        selection_prompts,
        after_prompts,
        timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=True,
        claim_failure=OSError("private gh claim failure"),
    )

    outcome = outcomes[0]
    assert exit_code == 1
    assert outcome.selected == [3]
    assert outcome.attempted == []
    assert outcome.completed == []
    assert outcome.succeeded is False
    assert outcome.stopping_point == "Item #3 infrastructure failure"
    assert len(selection_prompts) == 1
    assert after_prompts == []
    source.claim.assert_called_once_with(3, assignee="krishna")
    source.release.assert_called_once_with(3)
    assert "setup:item" not in timeline.read_text().splitlines()
    assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
    assert not (tmp_path / ".pycastle/worktrees/run-policy-halt-176").exists()
    branch = subprocess.run(
        ["git", "branch", "--list", "pycastle/run-policy-halt-176"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout == ""


def test_selection_record_failure_before_integration_cleans_run_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_record(**_kwargs: object) -> None:
        raise OSError("private record store failure")

    monkeypatch.setattr(orchestrator, "_write_item_selection_record", fail_record)
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        _selection_prompts,
        after_prompts,
        _timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=False,
    )

    outcome = outcomes[0]
    assert exit_code == 1
    assert outcome.completed == []
    assert outcome.selection_failure == "selection-infrastructure-failed"
    assert after_prompts == []
    source.claim.assert_not_called()
    assert not any(call[:3] == ["gh", "pr", "create"] for call in process_calls)
    assert not (tmp_path / ".pycastle/worktrees/run-policy-halt-176").exists()
    branch = subprocess.run(
        ["git", "branch", "--list", "pycastle/run-policy-halt-176"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout == ""


def test_later_selection_record_failure_preserves_safe_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original = orchestrator._write_item_selection_record
    writes = 0

    def fail_second_record(**kwargs: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("private record store failure")
        original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        orchestrator, "_write_item_selection_record", fail_second_record
    )
    (
        exit_code,
        source,
        process_calls,
        outcomes,
        _selection_prompts,
        after_prompts,
        timeline,
    ) = _run_policy_halt_from_cli(
        monkeypatch,
        tmp_path,
        after_integration=True,
    )

    outcome = outcomes[0]
    assert exit_code == 1
    assert outcome.completed == [3]
    assert outcome.selection_failure == "selection-infrastructure-failed"
    assert outcome.pr_opened is True
    assert outcome.pr_ready is False
    assert after_prompts == []
    source.claim.assert_called_once_with(3, assignee="krishna")
    assert timeline.read_text().splitlines().count("gate:run") == 0
    create = next(call for call in process_calls if call[:3] == ["gh", "pr", "create"])
    assert "--draft" in create
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)
    comment = next(
        call
        for call in process_calls
        if call[:2] == ["gh", "api"] and "--method" in call
    )
    comment_body = next(
        argument.removeprefix("body=")
        for argument in comment
        if argument.startswith("body=")
    )
    published = create[create.index("--body") + 1] + comment_body
    assert "Item selection failure: `selection-infrastructure-failed`" in published
    assert "Run Gate: not run" in published
    for private in (
        "Untouched candidate",
        "Private body for Item 1.",
        "Choose an actionable Item or stop.",
        "private model-authored halt reason",
        "private transcript without a selection response",
    ):
        assert private not in published
    assert not (tmp_path / ".pycastle/runs/policy-halt-176/selection-002.json").exists()


def _run_cycle_from_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, repair: bool
) -> tuple[int, MagicMock, list[list[str]], list[RunOutcome], list[str]]:
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "select.md").write_text("Choose an Item.")
    (prompts / "work.md").write_text("work")
    (prompts / "repair.md").write_text("repair")
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "runtime_node,gate_node,runtime_selection)\n"
        "run=build_run(item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md',on_success='verify'),"
        "gate_node('verify',on_failure='repair'),"
        "runtime_node('repair','repair.md',on_success='verify')])))\n"
    )
    timeline = tmp_path / "timeline"
    setup = fixture / "setup"
    setup.write_text(f"#!/bin/sh\nprintf 'setup\\n' >> '{timeline}'\n")
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text(f"#!/bin/sh\nprintf 'gate\\n' >> '{timeline}'\ntest -f repaired\n")
    gate.chmod(0o755)
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)
    process_calls: list[list[str]] = []

    def runner(
        argv: list[str], *, capture: bool = False, cwd: Path | None = None, **_: object
    ):
        process_calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    runtime_phases: list[str] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            runtime_phases.append(node)
            result = super().run(prompt, cwd=cwd, node=node)
            if repair and node == "repair":
                (cwd / "repaired").write_text("repaired\n")
            return result

    source = MagicMock()
    source.is_still_eligible.return_value = True
    outcomes: list[RunOutcome] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_make_run_id", lambda: "cli-cycle-135")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)
    code = main(["run", "--sandbox", "host", "--runtime", "stub"])
    return code, source, process_calls, outcomes, runtime_phases


def test_cli_repairs_item_through_explicit_gate_cycle_and_publishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, source, calls, outcomes, nodes = _run_cycle_from_cli(
        monkeypatch, tmp_path, repair=True
    )
    assert code == 0
    assert outcomes[0].completed == [1]
    assert nodes == ["item-selection", "work", "repair"]
    assert (tmp_path / "timeline").read_text().splitlines().count("gate") == 2
    source.mark_for_human.assert_not_called()
    assert any(
        call[:3] == ["gh", "pr", "create"] and "--draft" in call for call in calls
    )
    assert any(call[:3] == ["gh", "pr", "ready"] for call in calls)


def test_cli_exhausted_gate_cycle_hands_item_to_human_without_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, source, calls, outcomes, nodes = _run_cycle_from_cli(
        monkeypatch, tmp_path, repair=False
    )
    assert code != 0
    assert outcomes[0].completed == []
    assert nodes == ["item-selection", "work"] + ["repair"] * 10
    timeline = (tmp_path / "timeline").read_text().splitlines()
    assert timeline.count("gate") == 10
    # Run bootstrap, initial work, and ten visits to each cycle node.
    assert timeline.count("setup") == 23
    source.mark_for_human.assert_called_once_with(1)
    assert not any(call[:3] == ["gh", "pr", "create"] for call in calls)


@pytest.mark.parametrize(
    ("failure", "completed", "released", "opens_draft"),
    [
        ("bootstrap", [], None, False),
        ("first-item", [], 1, False),
        ("second-item", [1], 2, True),
    ],
)
def test_cli_setup_failure_preserves_only_safe_run_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
    completed: list[int],
    released: int | None,
    opens_draft: bool,
) -> None:
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "select.md").write_text("Choose an Item.")
    (prompts / "work.md").write_text("work")
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "runtime_node,runtime_selection)\n"
        "run=build_run(item=build_item("
        "selection=runtime_selection('select.md'),"
        "graph=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md')])))\n"
    )
    item_count = tmp_path / "item-setup-count"
    setup = fixture / "setup"
    if failure == "bootstrap":
        setup_body = "exit 17\n"
    else:
        failing_item = 1 if failure == "first-item" else 2
        setup_body = (
            'test "$PYCASTLE_SCOPE" = run && exit 0\n'
            f"count=$(cat '{item_count}' 2>/dev/null || printf 0)\n"
            "count=$((count + 1))\n"
            f"printf %s \"$count\" > '{item_count}'\n"
            f'test "$count" -ne {failing_item}\n'
        )
    setup.write_text("#!/bin/sh\n" + setup_body)
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text("#!/bin/sh\nexit 0\n")
    gate.chmod(0o755)
    (fixture / ".gitignore").write_text("/runs/\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    selected = (IssueRef(number=1, title="One"), IssueRef(number=2, title="Two"))
    frozen = FrozenReadinessInputs(
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        readiness._freeze_project_fixture(fixture, load_run(fixture)),
        selected,
        "host",
        "stub",
        None,
    )
    monkeypatch.setattr(
        cli,
        "_evaluate_cli_readiness",
        lambda _args: ReadinessReport(
            schema_version=1,
            outcome=ReadinessOutcome.READY,
            runner_version="0.1.0",
            configuration=ReadinessConfiguration(
                repository="owner/repo",
                base_branch="main",
                github_default_branch="main",
                runtime="stub",
                sandbox="host",
                agent_image=None,
                assignee="krishna",
                include_unassigned=False,
                item_limit=2,
            ),
            checks=tuple(
                ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
            ),
            candidate_items=tuple(CandidateItem(x.number, x.title) for x in selected),
            candidate_pool=selected,
            frozen_inputs=frozen,
        ),
    )

    calls: list[list[str]] = []

    def runner(argv: list[str], *, capture: bool = False, cwd=None, **_: object):
        calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            (cwd / f"item-{cwd.name.removeprefix('issue-')}.txt").write_text("done\n")
            return super().run(prompt, cwd=cwd, node=node)

    source = MagicMock()
    source.is_still_eligible.return_value = True
    outcomes: list[RunOutcome] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_make_run_id", lambda: f"cli-setup-{failure}")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)

    assert main(["run", "--sandbox", "host", "--runtime", "stub", "-i", "2"]) == 1
    assert outcomes[0].completed == completed
    assert source.release.call_args_list == (
        [] if released is None else [call(released)]
    )
    source.mark_for_human.assert_not_called()
    merges = [
        process_call for process_call in calls if process_call[:2] == ["git", "merge"]
    ]
    assert len(merges) == len(completed)
    assert any(call[:3] == ["gh", "pr", "create"] for call in calls) is opens_draft
    assert not any(call[:3] == ["gh", "pr", "ready"] for call in calls)
    assert (fixture / "runs" / f"cli-setup-{failure}").is_dir()


def test_cli_multi_item_run_repairs_final_gate_and_publishes_draft_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    for name in ("select", "item", "review", "report", "repair"):
        (prompts / f"{name}.md").write_text(name)
    (fixture / "main.py").write_text(
        "from pycastle.graph import (build_item,build_run,execution_graph,"
        "runtime_node,gate_node,runtime_selection)\n"
        "run=build_run(\n"
        " item=build_item(selection=runtime_selection('select.md'),"
        " graph=execution_graph(start='item-work',nodes=["
        "runtime_node('item-work','item.md',on_success='item-verify'),"
        "gate_node('item-verify')])),\n"
        " after=execution_graph(start='run-review',nodes=["
        "runtime_node('run-review','review.md',on_success='run-report'),"
        "runtime_node('run-report','report.md',on_success='run-verify'),"
        "gate_node('run-verify',on_failure='run-repair'),"
        "runtime_node('run-repair','repair.md',on_success='run-report')]))\n"
    )
    timeline = tmp_path / "timeline"
    setup = fixture / "setup"
    setup.write_text(
        f"#!/bin/sh\nprintf 'setup:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
    )
    setup.chmod(0o755)
    gate = fixture / "gate"
    gate.write_text(
        f"#!/bin/sh\nprintf 'gate:%s\\n' \"$PYCASTLE_SCOPE\" >> '{timeline}'\n"
        'test "$PYCASTLE_SCOPE" = item && exit 0\n'
        "printf 'failed gate state\\n' >> .pycastle/gate-state\n"
        "test -f repaired\n"
    )
    gate.chmod(0o755)
    (fixture / ".gitignore").write_text("/runs/\n/run-report.md\n/gate-state\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "PyCastle Test"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=tmp_path, check=True)

    nodes: list[str] = []

    class Runtime(StubRuntime):
        def run(self, prompt: str, *, cwd: Path, node: str):
            nodes.append(node)
            result = super().run(prompt, cwd=cwd, node=node)
            if node == "item-work":
                (cwd / f"item-{cwd.name.removeprefix('issue-')}.txt").write_text(
                    "done\n"
                )
            elif node == "run-review":
                assert (cwd / "item-1.txt").is_file()
                assert (cwd / "item-2.txt").is_file()
                (cwd / "integrated.txt").write_text("reviewed\n")
            elif node == "run-report":
                (cwd / ".pycastle" / "run-report.md").write_text(
                    "# Integrated Run\n\nTwo Items repaired and verified.\n"
                )
            elif node == "run-repair":
                assert (cwd / ".pycastle" / "gate-state").is_file()
                (cwd / "repaired").write_text("yes\n")
            return result

    calls: list[list[str]] = []

    def runner(argv: list[str], *, capture: bool = False, cwd=None, **_: object):
        calls.append(argv)
        if argv[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, "42\n", "")
        if argv[:2] == ["gh", "api"] and "--paginate" in argv:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(
            argv, cwd=cwd, capture_output=capture, text=True, check=False
        )

    selected = (IssueRef(number=1, title="One"), IssueRef(number=2, title="Two"))
    frozen = FrozenReadinessInputs(
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        readiness._freeze_project_fixture(fixture, load_run(fixture)),
        selected,
        "host",
        "stub",
        None,
    )
    monkeypatch.setattr(
        cli,
        "_evaluate_cli_readiness",
        lambda _args: ReadinessReport(
            schema_version=1,
            outcome=ReadinessOutcome.READY,
            runner_version="0.1.0",
            configuration=ReadinessConfiguration(
                repository="owner/repo",
                base_branch="main",
                github_default_branch="main",
                runtime="stub",
                sandbox="host",
                agent_image=None,
                assignee="krishna",
                include_unassigned=False,
                item_limit=2,
            ),
            checks=tuple(
                ReadinessCheck(check_id, Status.PASS, "ready") for check_id in CHECK_IDS
            ),
            candidate_items=tuple(CandidateItem(x.number, x.title) for x in selected),
            candidate_pool=selected,
            frozen_inputs=frozen,
        ),
    )
    source = MagicMock()
    source.is_still_eligible.return_value = True
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_make_run_id", lambda: "cli-run-136")
    monkeypatch.setattr(cli, "_build_runtime", lambda *_args, **_kwargs: Runtime())
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: source)
    original_run_loop = cli.run_loop
    outcomes: list[RunOutcome] = []

    def run_from_cli(**kwargs: object) -> RunOutcome:
        outcome = original_run_loop(**kwargs, runner=runner)  # type: ignore[arg-type]
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(cli, "run_loop", run_from_cli)

    assert (
        main(
            [
                "run",
                "--sandbox",
                "host",
                "--runtime",
                "stub",
                "--iterations",
                "2",
            ]
        )
        == 0
    )
    assert outcomes[0].completed == [1, 2]
    assert not (fixture / "runs" / "cli-run-136" / "project").exists()
    assert nodes == [
        "item-selection",
        "item-work",
        "item-selection",
        "item-work",
        "run-review",
        "run-report",
        "run-repair",
        "run-report",
    ]
    lines = timeline.read_text().splitlines()
    assert lines.count("gate:item") == 2
    assert lines.count("gate:run") == 2
    assert lines[-1] == "gate:run"  # passing Gate reaches DONE directly
    draft = next(
        i for i, call in enumerate(calls) if call[:3] == ["gh", "pr", "create"]
    )
    comment = next(
        i
        for i, call in enumerate(calls)
        if call[:2] == ["gh", "api"] and "--method" in call
    )
    ready = next(i for i, call in enumerate(calls) if call[:3] == ["gh", "pr", "ready"])
    assert draft < comment < ready
    commits = [call[3] for call in calls if call[:3] == ["git", "commit", "-m"]]
    assert len(commits) == 6


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            RunOutcome(run_id="empty", run_branch="pycastle/run-empty", selected=[]),
            0,
        ),
        (
            RunOutcome(
                run_id="before-human",
                run_branch="pycastle/run-before-human",
                selected=[107],
                succeeded=False,
                stopping_point="before-Run HUMAN",
            ),
            1,
        ),
        (
            RunOutcome(
                run_id="all-skipped",
                run_branch="pycastle/run-all-skipped",
                selected=[106, 107],
                attempted=[106, 107],
                issues=[
                    IssueOutcome(
                        issue=IssueRef(number=106, title="Skipped"),
                        branch="pycastle/issue-106",
                        merged=False,
                    ),
                    IssueOutcome(
                        issue=IssueRef(number=107, title="Human"),
                        branch="pycastle/issue-107",
                        merged=False,
                    ),
                ],
            ),
            1,
        ),
    ],
)
def test_run_exit_code_distinguishes_empty_selection_from_before_run_human(
    monkeypatch: pytest.MonkeyPatch,
    outcome: RunOutcome,
    expected: int,
) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda _repo: MagicMock())
    monkeypatch.setattr(cli, "run_loop", lambda **_kwargs: outcome)

    assert main(["run", "--runtime", "stub"]) == expected


def test_parses_run_sandbox_docker() -> None:
    args = build_parser().parse_args(["run", "--sandbox", "docker"])
    assert args.sandbox == "docker"


def test_parses_init() -> None:
    args = build_parser().parse_args(["init"])
    assert args.command == "init"
    assert args.sandbox is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("host", "host"),
        ("H", "host"),
        (" d ", "docker"),
        ("DOCKER", "docker"),
    ],
)
def test_prompt_sandbox_resolves_interactive_answers(
    monkeypatch: pytest.MonkeyPatch, answer: str, expected: str
) -> None:
    """The attached prompt accepts explicit Sandbox names and short forms."""
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)

    assert cli._prompt_sandbox() == expected


def test_prompt_sandbox_reprompts_until_an_explicit_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["", "invalid", "docker"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert cli._prompt_sandbox() == "docker"


def test_prompt_sandbox_rejects_eof_without_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closed stdin cannot silently select a Sandbox."""
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))

    with pytest.raises(cli.SandboxSelectionError):
        cli._prompt_sandbox()


@pytest.mark.parametrize("sandbox", ["host", "docker"])
def test_init_sandbox_flag_scaffolds_without_reading_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sandbox: str
) -> None:
    """An explicit init Sandbox is scriptable and skips the prompt."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "builtins.input",
        MagicMock(side_effect=AssertionError("init unexpectedly read stdin")),
    )

    assert main(["init", "--sandbox", sandbox]) == 0
    assert (tmp_path / ".pycastle" / "sandbox").read_text().strip() == sandbox


def test_init_completion_message_is_complete_and_sandbox_invariant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    caplog.set_level("INFO", logger="pycastle")
    messages: dict[str, str] = {}
    for sandbox in ("host", "docker"):
        root = tmp_path / sandbox
        root.mkdir()
        monkeypatch.chdir(root)
        caplog.clear()
        assert main(["init", "--sandbox", sandbox]) == 0
        messages[sandbox] = caplog.text

    assert messages["host"].replace("host", "SANDBOX") == messages["docker"].replace(
        "docker", "SANDBOX"
    )
    for phrase in (
        "configure .pycastle/setup when",
        "fail-closed .pycastle/gate",
        "extend .pycastle/Dockerfile",
        "commit the complete .pycastle/",
    ):
        assert phrase in messages["host"]


def test_init_with_closed_stdin_fails_with_actionable_choices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Non-interactive init requires a scripted Sandbox selection."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))

    assert main(["init"]) != 0
    assert not (tmp_path / ".pycastle").exists()
    assert "pycastle init --sandbox host" in caplog.text
    assert "pycastle init --sandbox docker" in caplog.text


def test_attached_init_eof_fails_with_actionable_choices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An attached terminal closing at the prompt still cannot imply host."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", MagicMock(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=EOFError))

    assert main(["init"]) == 2
    assert not (tmp_path / ".pycastle").exists()
    assert "pycastle init --sandbox host" in caplog.text
    assert "pycastle init --sandbox docker" in caplog.text


def test_invalid_init_sandbox_is_rejected_before_writing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Argparse rejects an invalid Sandbox before init can create its fixture."""
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["init", "--sandbox", "cloud"])

    assert excinfo.value.code == 2
    assert not (tmp_path / ".pycastle").exists()


def test_parses_and_dispatches_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    upgraded = MagicMock()
    upgraded.changed = False
    upgraded.fixture_version = "1.0"
    upgraded.runner_version = "1.0"
    monkeypatch.setattr(cli, "upgrade_fixture", MagicMock(return_value=upgraded))

    assert build_parser().parse_args(["upgrade"]).command == "upgrade"
    assert main(["upgrade"]) == 0
    cli.upgrade_fixture.assert_called_once_with(Path.cwd())


def test_main_fails_fast_when_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(_commands: object) -> None:
        raise PreflightError("Required command(s) not found on PATH: gh")

    monkeypatch.setattr(cli, "check_required_commands", boom)
    assert main(["init", "--sandbox", "host"]) == 1


def test_main_dispatches_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    dispatched = MagicMock(return_value=0)
    monkeypatch.setattr(cli, "_cmd_run", dispatched)

    assert main(["run", "--runtime", "stub"]) == 0
    dispatched.assert_called_once()


def test_doctor_interrupt_emits_no_report_and_exits_130(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        cli, "_evaluate_cli_readiness", MagicMock(side_effect=KeyboardInterrupt)
    )

    assert main(["doctor", "--json"]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""


def test_init_scaffolds_the_chosen_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``pycastle init`` prompts host/docker and scaffolds the matching fixture.

    The interactive prompt is stubbed so this test can focus on threading the
    answer into the scaffolder and writing a fixture into cwd.
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", MagicMock(isatty=lambda: True))
    # The user picks Docker at the prompt.
    monkeypatch.setattr(cli, "_prompt_sandbox", lambda: "docker")

    assert main(["init"]) == 0

    fixture = tmp_path / ".pycastle"
    assert (fixture / "main.py").is_file()
    assert (fixture / "Dockerfile").is_file()
    assert (fixture / ".gitignore").is_file()
    # The Docker choice is recorded in the scaffolded fixture.
    assert (fixture / "sandbox").read_text().strip() == "docker"


def test_init_empty_answer_requires_an_explicit_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty answer is rejected and the attached prompt asks again."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", MagicMock(isatty=lambda: True))
    answers = iter(["", "host"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    assert main(["init"]) == 0
    assert (tmp_path / ".pycastle" / "sandbox").read_text().strip() == "host"


def test_init_refuses_to_clobber_an_existing_fixture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running init where ``.pycastle/`` already exists exits non-zero, no clobber."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", MagicMock(isatty=lambda: True))
    monkeypatch.setattr(cli, "_prompt_sandbox", lambda: "host")

    assert main(["init"]) == 0
    # Second run is refused; the fixture is left as-is.
    assert main(["init"]) == 1
    assert (tmp_path / ".pycastle" / "sandbox").read_text().strip() == "host"


def test_init_does_not_require_docker_or_runtime_in_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """init only needs git/gh — never docker or an agent CLI on PATH."""
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_init", lambda _args: 0)

    main(["init"])

    assert "docker" not in seen["commands"]
    assert "claude" not in seen["commands"]
    assert "codex" not in seen["commands"]


def test_run_host_uses_frozen_execution_without_injected_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host lifecycle passes only its frozen execution snapshot."""
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured: dict[str, object] = {}

    def fake_run_loop(**kwargs: object) -> MagicMock:
        captured.update(kwargs)
        outcome = MagicMock()
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)

    assert main(["run", "--sandbox", "host", "--runtime", "stub"]) == 0

    assert "gate_check" not in captured
    assert captured["frozen_inputs"] is not None


def _write_marker(tmp_path: Path, value: str) -> None:
    """Write ``value`` into a ``.pycastle/sandbox`` marker under ``tmp_path``."""
    fixture = _write_version_marker(tmp_path)
    (fixture / "sandbox").write_text(value)


def _write_version_marker(tmp_path: Path) -> Path:
    fixture = tmp_path / ".pycastle"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "version").write_text("0.1.0\n")
    return fixture


def _mock_run_externals(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub everything ``pycastle run`` touches and capture the run-loop kwargs.

    Mocks preflight, repo/branch/assignee resolution, the issue source, and the
    orchestrator's ``run_loop`` so no real gh/git/docker/network runs. Returns
    the dict the fake run loop records its kwargs into.
    """
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(cli, "_resolve_base_branch", lambda: "main")
    monkeypatch.setattr(cli, "_resolve_assignee", lambda login: "krishna")
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda repo: MagicMock())

    captured: dict[str, object] = {}

    def fake_run_loop(*, runtime: object, **_kwargs: object) -> MagicMock:
        captured["runtime"] = runtime
        outcome = MagicMock()
        outcome.issues = []
        return outcome

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    return captured


def test_run_defaults_sandbox_from_host_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No flag + marker=host drives the run on the host (no docker wrapper)."""
    _write_marker(tmp_path, "host\n")
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None


def test_run_flag_overrides_docker_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An explicit ``--sandbox host`` overrides a marker that says docker."""
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--sandbox", "host", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None


def test_run_falls_back_to_host_when_marker_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No flag and no marker at all falls back to a host run."""
    _write_version_marker(tmp_path)
    monkeypatch.chdir(tmp_path)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None


def test_run_falls_back_to_host_for_empty_or_garbage_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty or unrecognised marker value falls back to host, never crashes."""
    monkeypatch.chdir(tmp_path)

    for value in ("", "   \n", "weird-value\n"):
        _write_marker(tmp_path, value)
        captured = _mock_run_externals(monkeypatch)
        assert main(["run", "--runtime", "claude"]) == 0
        runtime = captured["runtime"]
        assert getattr(runtime, "argv_wrapper", None) is None


def test_run_marker_docker_skips_legacy_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run delegates the resolved Docker command inventory to readiness."""
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)

    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 0)

    main(["run", "--runtime", "claude"])

    assert "commands" not in seen


def test_resolve_sandbox_prefers_explicit_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_resolve_sandbox`` returns the flag verbatim without reading the marker."""
    _write_marker(tmp_path, "docker\n")
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_sandbox("host") == "host"
    assert cli._resolve_sandbox("docker") == "docker"


def test_resolve_sandbox_reads_marker_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no flag, only an exact marker resolves a Sandbox."""
    monkeypatch.chdir(tmp_path)
    assert cli._resolve_sandbox(None) == ""  # no marker: evaluator reports not_ready

    _write_marker(tmp_path, "docker\n")
    assert cli._resolve_sandbox(None) == "docker"


def test_run_host_codex_skips_legacy_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 0)

    main(["run", "--sandbox", "host", "--runtime", "codex"])

    assert "commands" not in seen


def test_run_docker_skips_legacy_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, list[str]] = {}

    def record(commands: list[str]) -> None:
        seen["commands"] = list(commands)

    monkeypatch.setattr(cli, "check_required_commands", record)
    monkeypatch.setattr(cli, "_cmd_run", lambda _args: 0)

    main(["run", "--sandbox", "docker", "--runtime", "claude"])

    assert "commands" not in seen


def test_build_runtime_docker_codex_builds_a_sandboxed_runtime(
    tmp_path: Path,
) -> None:
    # The docker-vs-host choice is orthogonal to the runtime: asking for codex
    # in Docker yields a CodexRuntime whose wrapper produces a docker argv
    # against the codex auth volume, exactly as claude does.
    runtime = cli._build_runtime(
        "codex", "docker", tmp_path, image="sha256:" + "a" * 64
    )
    assert runtime.name == "codex"
    assert runtime.argv_wrapper is not None
    worktree = tmp_path / ".pycastle" / "worktrees" / "issue-1"
    wrapped = runtime.argv_wrapper(["codex", "exec", "--json", "x"], worktree)
    assert wrapped[:3] == ["docker", "run", "--rm"]
    assert "pycastle-codex-auth:/pycastle/auth" in wrapped
    assert "CODEX_HOME=/pycastle/auth" in wrapped


def test_build_runtime_host_path_is_a_bare_runtime(tmp_path: Path) -> None:
    # The host path produces a plain runtime with no docker wrapper, so its
    # argv stays the bare claude command. Docker only enters via --sandbox docker.
    runtime = cli._build_runtime("claude", "host", tmp_path)
    assert getattr(runtime, "argv_wrapper", None) is None


# --- Canonical Agent-image readiness boundaries -----------------------------


def _write_dockerfile(tmp_path: Path, text: str) -> Path:
    """Write a ``Dockerfile`` into a ``.pycastle/`` fixture under ``tmp_path``."""
    fixture = _write_version_marker(tmp_path)
    (fixture / "Dockerfile").write_text(text)
    return fixture


def _fake_docker(*, inspect_returncode: int) -> tuple[list[list[str]], object]:
    """Build a run_cmd stand-in recording every docker argv it is handed.

    ``docker image inspect`` returns ``inspect_returncode``; everything else
    (the build) returns success. Returns the recording list and the callable.
    """
    calls: list[list[str]] = []

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        calls.append(list(args))
        proc = MagicMock()
        if args[:3] == ["docker", "image", "inspect"]:
            proc.returncode = inspect_returncode
        else:
            proc.returncode = 0
        return proc

    return calls, runner


def test_run_rejects_removed_image_override_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The removed image override is rejected before Runtime or Docker work.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        raise AssertionError(f"docker should never run for a blank image: {args}")

    monkeypatch.setattr(cli, "run_cmd", runner)

    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--sandbox", "docker", "--runtime", "claude", "--image", ""])
    assert exc_info.value.code == 2


def test_run_docker_exits_nonzero_when_build_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End to end: a failed build aborts the run with a clean non-zero exit (not a
    # traceback) and never resolves a runtime or opens a PR.
    _write_dockerfile(tmp_path, "FROM node:22-slim\nRUN false\n")
    monkeypatch.chdir(tmp_path)

    def runner(args: list[str], **_kwargs: object) -> MagicMock:
        proc = MagicMock()
        proc.returncode = 1  # inspect: absent; build: fails
        return proc

    monkeypatch.setattr(cli, "check_required_commands", lambda _commands: None)
    monkeypatch.setattr(cli, "run_cmd", runner)

    assert main(["run", "--sandbox", "docker", "--runtime", "claude"]) == 1


def test_run_host_never_resolves_an_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Resolution is docker-only: a host run never inspects or builds an image,
    # even with a Dockerfile present.
    _write_dockerfile(tmp_path, "FROM node:22-slim\n")
    monkeypatch.chdir(tmp_path)
    calls, runner = _fake_docker(inspect_returncode=1)
    monkeypatch.setattr(cli, "run_cmd", runner)
    captured = _mock_run_externals(monkeypatch)

    assert main(["run", "--sandbox", "host", "--runtime", "claude"]) == 0

    runtime = captured["runtime"]
    assert getattr(runtime, "argv_wrapper", None) is None
    assert calls == []
