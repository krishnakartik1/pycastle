"""CLI argument parsing, preflight, and command dispatch."""

from __future__ import annotations

import subprocess
from importlib.metadata import version
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from packaging.version import Version

from pycastle import cli, readiness
from pycastle import migrations as fixture_migrations
from pycastle.cli import build_parser, main
from pycastle.graph import load_run
from pycastle.issues import IssueRef
from pycastle.orchestrator import IssueOutcome, RunOutcome
from pycastle.preflight import PreflightError
from pycastle.readiness import (
    CHECK_IDS,
    EligibleItem,
    FrozenReadinessInputs,
    ReadinessCheck,
    ReadinessConfiguration,
    ReadinessOutcome,
    ReadinessReport,
    Status,
)
from pycastle.runtime import StubRuntime
from pycastle.upgrade import FixtureMigration


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
            frozen.items = selected
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
            eligible_items=(EligibleItem(1, "One"),),
            selected_items=selected,
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


def test_cli_completes_host_item_through_explicit_runtime_gate_graph(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drive CLI dispatch, real Git worktrees, and deterministic external adapters."""
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "work.md").write_text("work")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,runtime_node,gate_node\n"
        "run=build_run(item=execution_graph(start='work',nodes=["
        "gate_node('verify'),runtime_node('work','work.md',on_success='verify')]))\n"
    )
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
    assert timeline.read_text().splitlines() == ["setup", "setup", "setup", "gate"]
    source.claim.assert_called_once_with(1, assignee="krishna")
    assert any(
        call[:3] == ["gh", "pr", "create"] and "--draft" in call
        for call in process_calls
    )
    assert any(call[:3] == ["gh", "pr", "ready"] for call in process_calls)


def _run_cycle_from_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, repair: bool
) -> tuple[int, MagicMock, list[list[str]], list[RunOutcome], list[str]]:
    fixture = tmp_path / ".pycastle"
    prompts = fixture / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "work.md").write_text("work")
    (prompts / "repair.md").write_text("repair")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,runtime_node,gate_node\n"
        "run=build_run(item=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md',on_success='verify'),"
        "gate_node('verify',on_failure='repair'),"
        "runtime_node('repair','repair.md',on_success='verify')]))\n"
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
    assert nodes == ["work", "repair"]
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
    assert nodes == ["work"] + ["repair"] * 10
    timeline = (tmp_path / "timeline").read_text().splitlines()
    assert timeline.count("gate") == 10
    # Run bootstrap, initial work, and ten visits to each cycle node.
    assert timeline.count("setup") == 22
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
    (prompts / "work.md").write_text("work")
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,runtime_node\n"
        "run=build_run(item=execution_graph(start='work',nodes=["
        "runtime_node('work','work.md')]))\n"
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
            eligible_items=tuple(EligibleItem(x.number, x.title) for x in selected),
            selected_items=selected,
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
    for name in ("item", "review", "report", "repair"):
        (prompts / f"{name}.md").write_text(name)
    (fixture / "main.py").write_text(
        "from pycastle.graph import build_run,execution_graph,runtime_node,gate_node\n"
        "run=build_run(\n"
        " item=execution_graph(start='item-work',nodes=["
        "runtime_node('item-work','item.md',on_success='item-verify'),"
        "gate_node('item-verify')]),\n"
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
            eligible_items=tuple(EligibleItem(x.number, x.title) for x in selected),
            selected_items=selected,
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
        "item-work",
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
