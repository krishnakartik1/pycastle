"""The ``pycastle`` command-line entry point."""

from __future__ import annotations

import argparse
import datetime
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from . import __version__, sandbox
from .commands import run_cmd
from .compatibility import FixtureCompatibilityError
from .issues import GitHubIssueSource
from .orchestrator import (
    ITEM_SELECTION_END_POLICY_HALT,
    PruneError,
    prune_run_branches,
)
from .orchestrator import run_batch as run_loop
from .preflight import (
    PreflightError,
    check_required_commands,
)
from .readiness import (
    AgentImagePreparationError,
    DefaultReadinessAdapter,
    ReadinessConfiguration,
    ReadinessOutcome,
    ReadinessReport,
    Status,
    evaluate_readiness,
    prepare_agent_image,
    render_human,
    render_json,
)
from .runtime import ClaudeRuntime, CodexRuntime, Runtime, make_runtime
from .scaffold import (
    FixtureExistsError,
    SandboxChoice,
    read_sandbox,
    scaffold_fixture,
)
from .upgrade import FixtureUpgradeError, upgrade_fixture

logger = logging.getLogger("pycastle")

FIXTURE_DIR = Path(".pycastle")
DOCKERFILE_NAME = "Dockerfile"
RUNTIMES = ("stub", "claude", "codex")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``init``, ``sandbox``, and ``run``."""
    parser = argparse.ArgumentParser(
        prog="pycastle",
        description="A reusable autonomous development loop.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser(
        "init", help="Scaffold a .pycastle/ Project fixture into this repo"
    )
    init_parser.add_argument(
        "--sandbox",
        choices=("host", "docker"),
        default=None,
        help=(
            "Sandbox recorded in the Project fixture. When omitted from an "
            "attached terminal, init requires an explicit host or Docker choice."
        ),
    )
    sub.add_parser("upgrade", help="Migrate this repo's Project fixture")
    prune_parser = sub.add_parser(
        "prune", help="Delete run branches whose PRs are no longer open"
    )
    prune_parser.add_argument(
        "--include-no-pr",
        action="store_true",
        help="Also delete Run branches with no associated pull request",
    )

    runtime_parser = sub.add_parser("runtime", help="Manage a Runtime")
    runtime_sub = runtime_parser.add_subparsers(dest="runtime_command", required=True)
    login = runtime_sub.add_parser("login", help="Explicitly authenticate a Runtime")
    login.add_argument("--runtime", choices=RUNTIMES, default="claude")
    login.add_argument("--sandbox", choices=("host", "docker"), default=None)

    doctor_parser = sub.add_parser(
        "doctor", help="Check one Run configuration without starting a Run"
    )
    doctor_parser.add_argument("-i", "--iterations", type=_positive_int, default=1)
    doctor_parser.add_argument("--runtime", choices=RUNTIMES, default="claude")
    doctor_parser.add_argument("--assignee", type=_non_empty, default="@me")
    doctor_parser.add_argument("-u", "--include-unassigned", action="store_true")
    doctor_parser.add_argument("--sandbox", choices=("host", "docker"), default=None)
    doctor_parser.add_argument("--json", action="store_true")

    run_parser = sub.add_parser("run", help="Run the autonomous loop")
    run_parser.add_argument(
        "-i",
        "--iterations",
        type=_positive_int,
        default=1,
        help="Max work items to process",
    )
    run_parser.add_argument(
        "--runtime",
        choices=RUNTIMES,
        default="claude",
        help="Runtime to drive the loop (default: claude; stub is selectable)",
    )
    run_parser.add_argument(
        "--assignee",
        type=_non_empty,
        default="@me",
        help="Only work issues assigned to this login (default: the gh user)",
    )
    run_parser.add_argument(
        "-u",
        "--include-unassigned",
        action="store_true",
        help="Also work issues that have no assignee",
    )
    run_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Capture and surface the Runtime reasoning trace, live as "
            "[THINKING:<node>] lines and persisted under "
            ".pycastle/runs/<run_id>/. High-volume; off by default."
        ),
    )
    run_parser.add_argument(
        "--sandbox",
        choices=("host", "docker"),
        default=None,
        help=(
            "Sandbox for Runtime nodes. When omitted, use the explicit choice "
            "recorded in .pycastle/sandbox; a missing or invalid marker fails "
            "Run readiness."
        ),
    )
    return parser


def _resolve_repo() -> str:
    """Resolve the current repo as ``owner/name`` via ``gh``."""
    result = run_cmd(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture=True,
    )
    return (result.stdout or "").strip()


def _resolve_base_branch() -> str:
    """Resolve the current branch PyCastle branches from and PRs back into."""
    result = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True)
    return (result.stdout or "").strip()


def _resolve_assignee(login: str) -> str:
    """Resolve ``@me`` to the current gh login; pass other values through."""
    if login and login != "@me":
        return login
    result = run_cmd(["gh", "api", "user", "--jq", ".login"], capture=True)
    resolved = (result.stdout or "").strip()
    if not resolved:
        raise PreflightError("Could not resolve the current GitHub login.")
    return resolved


def _resolve_sandbox(flag: str | None) -> str:
    """Resolve an explicit flag or exact fixture marker, without a default."""
    if flag is not None:
        return flag
    recorded = read_sandbox(FIXTURE_DIR)
    if recorded in ("host", "docker"):
        return recorded
    return ""


def _build_runtime(
    runtime_name: str,
    sandbox_kind: str,
    workspace: Path,
    *,
    image: str | None = None,
    verbose: bool = False,
) -> Runtime:
    """Build the Runtime for a run, sandboxed in Docker when asked.

    ``--sandbox docker`` wraps each node's inner Runtime argv into a
    ``docker run`` argv, so both the Runtime and the commands it invokes run
    inside the Agent image. ``image`` is the immutable identity pinned by Run
    readiness and is threaded into the Docker wrapper.
    Both Claude and Codex support this; every other combination runs the runtime
    on the host as before. The docker-vs-host choice is orthogonal to which
    runtime runs.

    ``verbose`` turns on transcript capture (thinking + output, #48/#52). It is
    threaded into the real Claude/Codex runtimes as a constructor attribute, so
    the ``Runtime.run`` signature stays unchanged. The per-issue transcript sink
    is bound later by the orchestrator (which owns ``run_id`` and the issue
    number); here the runtime is built only with ``verbose`` so live
    ``[THINKING:<node>]`` and ``[OUTPUT:<node>]`` surfacing turns on. With
    ``verbose`` off the host path stays the bare :func:`make_runtime` runtime, so
    nothing changes.
    """
    if sandbox_kind == "docker":
        if image is None:
            raise ValueError("Docker Runtime requires a pinned Agent image")
        if runtime_name == "claude":
            return ClaudeRuntime.in_docker(
                workspace=workspace, image=image, verbose=verbose
            )
        if runtime_name == "codex":
            return CodexRuntime.in_docker(
                workspace=workspace, image=image, verbose=verbose
            )
        raise NotImplementedError(
            f"The Docker sandbox for the {runtime_name!r} runtime is not "
            "available; run it with --sandbox host for now."
        )
    if verbose and runtime_name == "claude":
        return ClaudeRuntime(verbose=True)
    if verbose and runtime_name == "codex":
        return CodexRuntime(verbose=True)
    return make_runtime(runtime_name)


def _make_run_id() -> str:
    """Return a timestamp-based run id for a fresh run."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _positive_int(value: str) -> int:
    """Parse a strictly positive CLI integer."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_empty(value: str) -> str:
    """Parse a non-empty CLI string without changing the supplied value."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _readiness_configuration(args: argparse.Namespace) -> ReadinessConfiguration:
    """Resolve Doctor/Run inputs once using the shared CLI defaults."""
    sandbox_kind = _resolve_sandbox(args.sandbox)

    def resolve(argv: list[str]) -> str:
        try:
            result = run_cmd(argv, capture=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return (result.stdout or "").strip()

    repository = resolve(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    )
    base_branch = resolve(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    default_branch = resolve(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ]
    )
    assignee = (
        args.assignee
        if args.assignee != "@me"
        else resolve(["gh", "api", "user", "--jq", ".login"])
    )
    return ReadinessConfiguration(
        repository=repository,
        base_branch=base_branch,
        github_default_branch=default_branch or None,
        runtime=args.runtime,
        sandbox=sandbox_kind,
        agent_image=None,
        assignee=assignee,
        include_unassigned=args.include_unassigned,
        item_limit=args.iterations,
    )


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Evaluate and render one non-destructive readiness snapshot."""
    report = _evaluate_cli_readiness(args)
    output = render_json(report) if args.json else render_human(report)
    print(output)
    return 0 if report.outcome is not ReadinessOutcome.NOT_READY else 1


def _evaluate_cli_readiness(args: argparse.Namespace) -> ReadinessReport:
    """Resolve and evaluate the readiness configuration used by Doctor and Run."""
    configuration = _readiness_configuration(args)
    cleanup_reporter = (
        (lambda message: print(message, file=sys.stderr))
        if args.command == "doctor"
        else None
    )
    with DefaultReadinessAdapter(
        FIXTURE_DIR,
        Path.cwd(),
        include_item_content=args.command == "run",
        # Human Doctor may show a first build's progress. Run and JSON keep
        # child output captured so stdout remains a single report document.
        stream_image_build=args.command == "doctor" and not args.json,
        cleanup_reporter=cleanup_reporter,
    ) as adapter:
        progress = None
        if args.command == "doctor" and args.json:

            def report_progress(
                event: str, check_id: str, status: Status | None
            ) -> None:
                print(
                    f"Doctor {check_id}: {status.value if status else event}",
                    file=sys.stderr,
                )

            progress = report_progress
        return evaluate_readiness(
            configuration, adapter.dependencies(), progress=progress
        )


def _run_has_complete_frozen_batch(report: ReadinessReport) -> bool:
    """Return whether every eligible Item has matching Run-only content."""
    frozen = report.frozen_inputs
    if frozen is None or frozen.items != report.selected_items:
        return False
    configuration = report.configuration
    if (
        frozen.sandbox != configuration.sandbox
        or frozen.runtime != configuration.runtime
        or frozen.agent_image != configuration.agent_image
    ):
        return False
    if len(report.selected_items) != len(report.eligible_items):
        return False
    return all(
        selected.number == eligible.number and selected.title == eligible.title
        for selected, eligible in zip(
            report.selected_items, report.eligible_items, strict=True
        )
    )


def _cmd_run(args: argparse.Namespace) -> int:
    """Dispatch ``pycastle run``: work up to ``--iterations`` issues into one PR."""
    # Readiness is deliberately the first Run operation. It may prepare a
    # canonical Agent image, but creates no Run ID, record, branch,
    # worktree, claim, node, Setup invocation, or ordinary Gate invocation.
    report = _evaluate_cli_readiness(args)
    if report.outcome is ReadinessOutcome.NO_WORK:
        logger.info("Nothing to do.")
        return 0
    if report.outcome is ReadinessOutcome.NOT_READY:
        for check in report.checks:
            if check.status in {Status.FAIL, Status.BLOCKED}:
                logger.error("Readiness %s: %s", check.id, check.summary)
        return 1
    if not _run_has_complete_frozen_batch(report):
        logger.error("Readiness did not return a complete matching frozen snapshot.")
        return 1

    configuration = report.configuration
    workspace = Path.cwd()
    if configuration.sandbox == "docker":
        image = configuration.agent_image
        if image is None:  # Defensive: a ready Docker report always has an image.
            raise PreflightError("No Agent image was resolved.")
        runtime = _build_runtime(
            configuration.runtime,
            configuration.sandbox,
            workspace,
            image=image,
            verbose=args.verbose,
        )
    else:
        runtime = _build_runtime(
            configuration.runtime,
            configuration.sandbox,
            workspace,
            verbose=args.verbose,
        )
    repo = configuration.repository
    base_branch = configuration.base_branch
    assignee = configuration.assignee
    issue_source = GitHubIssueSource(repo)

    outcome = run_loop(
        runtime=runtime,
        issue_source=issue_source,
        selected=report.selected_items,
        fixture_dir=FIXTURE_DIR,
        repo=repo,
        base_branch=base_branch,
        assignee=assignee,
        run_id=_make_run_id(),
        iterations=configuration.item_limit,
        include_unassigned=configuration.include_unassigned,
        verbose=args.verbose,
        frozen_inputs=report.frozen_inputs,
    )
    if not outcome.selected:
        if outcome.selection_end == ITEM_SELECTION_END_POLICY_HALT:
            logger.info("Project policy halted Item selection.")
        elif not outcome.succeeded:
            logger.error(
                "Run %s stopped during Item selection; details are in local "
                "Run records.",
                outcome.run_id,
            )
            return 1
        else:
            logger.info("Nothing to do.")
        return 0
    logger.info(
        "Run %s worked %d issue(s), merged %s; PR opened: %s",
        outcome.run_id,
        len(outcome.issues),
        outcome.completed,
        outcome.pr_opened,
    )
    return 0 if outcome.pr_opened and outcome.succeeded else 1


def _cmd_prune(args: argparse.Namespace) -> int:
    """Delete remote Run branches after their pull requests close or merge."""
    deleted = prune_run_branches(
        repo=_resolve_repo(), cwd=Path.cwd(), include_no_pr=args.include_no_pr
    )
    if deleted:
        logger.info(
            "Deleted %d stale run branch(es): %s", len(deleted), ", ".join(deleted)
        )
    else:
        logger.info("No stale run branches found.")
    return 0


def _cmd_runtime_login(args: argparse.Namespace) -> int:
    """Run the selected Runtime's explicit native authentication flow."""
    if args.runtime == "stub":
        logger.error("The Stub Runtime does not support login.")
        return 2
    sandbox_kind = _resolve_sandbox(args.sandbox)
    if sandbox_kind not in {"host", "docker"}:
        logger.error("Select host or docker, or record it in .pycastle/sandbox.")
        return 2
    convention = sandbox.RUNTIME_CONFIG[args.runtime]
    if sandbox_kind == "host":
        result = run_cmd(list(convention.login_args))
    else:
        try:
            image = prepare_agent_image(
                FIXTURE_DIR, runner=run_cmd, cwd=Path.cwd(), capture_build=False
            )
        except AgentImagePreparationError as exc:
            raise PreflightError(str(exc)) from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PreflightError(
                "Failed to build and pin the canonical Agent image."
            ) from exc
        result = run_cmd(sandbox.build_login_command(args.runtime, image=image))
    if getattr(result, "returncode", 1) != 0:
        logger.error("Runtime login failed.")
        return 1
    logger.info("The %s Runtime login completed.", args.runtime)
    return 0


class SandboxSelectionError(Exception):
    """Raised when initialization cannot obtain an explicit Sandbox choice."""


def _prompt_sandbox() -> SandboxChoice:
    """Prompt until an attached maintainer explicitly chooses host or Docker."""
    while True:
        try:
            answer = input("Sandbox: [h]ost or [d]ocker? ").strip().lower()
        except EOFError as exc:
            raise SandboxSelectionError from exc
        if answer in {"host", "h"}:
            return "host"
        if answer in {"docker", "d"}:
            return "docker"
        logger.error("Choose 'host' or 'docker'.")


def _cmd_init(args: argparse.Namespace) -> int:
    """Dispatch ``pycastle init``: scaffold the Project fixture into the cwd.

    Uses an explicit ``--sandbox`` choice without reading stdin, or prompts an
    attached maintainer for host or Docker when omitted. Refuses to clobber an existing
    ``.pycastle/`` so a project's prompts, gate, and graph shape are never
    silently replaced.
    """
    if args.sandbox is not None:
        choice = cast(SandboxChoice, args.sandbox)
    else:
        if not sys.stdin.isatty():
            logger.error(
                "A Sandbox choice is required for non-interactive initialization. "
                "Run `pycastle init --sandbox host` or "
                "`pycastle init --sandbox docker`."
            )
            return 2
        try:
            choice = _prompt_sandbox()
        except SandboxSelectionError:
            logger.error(
                "No Sandbox was selected. Run `pycastle init --sandbox host` or "
                "`pycastle init --sandbox docker`."
            )
            return 2
    try:
        written = scaffold_fixture(Path.cwd(), sandbox=choice)
    except FixtureExistsError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Scaffolded the PyCastle Project fixture (Sandbox: %s) into .pycastle/:",
        choice,
    )
    for path in written:
        logger.info("  %s", path.relative_to(Path.cwd()))
    logger.info(
        "Next: configure .pycastle/setup when durable preparation is needed; "
        "replace the fail-closed .pycastle/gate with the project's verification "
        "policy; for Docker use, extend .pycastle/Dockerfile at the project "
        "toolchain section; then review and commit the complete .pycastle/ "
        "Project fixture."
    )
    return 0


def _cmd_upgrade() -> int:
    """Migrate the Project fixture and leave a reviewable unstaged diff."""
    result = upgrade_fixture(Path.cwd())
    if result.changed:
        detail = ", ".join(result.applied_versions) or "already-corrected targets"
        logger.info(
            "Upgraded the Project fixture from %s to %s (migrations: %s).",
            result.fixture_version,
            result.runner_version,
            detail,
        )
    else:
        logger.info(
            "Project fixture %s is compatible with PyCastle %s; no migration applies.",
            result.fixture_version,
            result.runner_version,
        )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run preflight, and dispatch the chosen command."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    required = ["git", "gh"]
    if args.command in {"run", "doctor"}:
        # Resolve the effective sandbox (flag -> .pycastle/sandbox marker)
        # before readiness so the command inventory matches where the
        # Run will execute. Invalid or missing selection remains empty and is
        # reported by the shared evaluator. The value is written back onto
        # args so _cmd_run reads the same decision.
        args.sandbox = _resolve_sandbox(args.sandbox)
        if args.sandbox == "docker":
            # Docker is the isolation boundary: the host needs docker, not the
            # agent CLI (which lives inside the image).
            required.append("docker")
        elif args.runtime in {"claude", "codex"}:
            required.append(args.runtime)
    if args.command == "runtime" and args.runtime_command == "login":
        selected = _resolve_sandbox(args.sandbox)
        if args.runtime != "stub" and selected in {"host", "docker"}:
            required.append("docker" if selected == "docker" else args.runtime)
    try:
        if args.command not in {"doctor", "run"}:
            check_required_commands(required)
        if args.command == "doctor":
            return _cmd_doctor(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "upgrade":
            return _cmd_upgrade()
        if args.command == "prune":
            return _cmd_prune(args)
        if args.command == "runtime":
            return _cmd_runtime_login(args)
    except (
        FixtureCompatibilityError,
        FixtureUpgradeError,
        PreflightError,
        PruneError,
    ) as exc:
        # Covers both preflight (missing commands) and a failed on-demand image
        # build, which raises PreflightError rather than running a missing image.
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    return 2
