"""The ``pycastle`` command-line entry point."""

from __future__ import annotations

import argparse
import datetime
import logging
from collections.abc import Sequence
from pathlib import Path

from . import sandbox
from .commands import run_cmd
from .issues import GitHubIssueSource
from .orchestrator import make_fixture_gate_check
from .orchestrator import run_batch as run_loop
from .preflight import PreflightError, check_required_commands
from .runtime import ClaudeRuntime, CodexRuntime, Runtime, make_runtime

logger = logging.getLogger("pycastle")

FIXTURE_DIR = Path(".pycastle")
RUNTIMES = ("stub", "claude", "codex")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``init``, ``sandbox``, and ``run``."""
    parser = argparse.ArgumentParser(
        prog="pycastle",
        description="A reusable autonomous development loop.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Scaffold a .pycastle/ project fixture (coming soon)")

    sandbox = sub.add_parser("sandbox", help="Manage the Docker agent sandbox")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)
    setup = sandbox_sub.add_parser(
        "setup", help="Log a runtime into its Docker auth volume (coming soon)"
    )
    setup.add_argument("--runtime", choices=RUNTIMES, default="claude")

    run_parser = sub.add_parser("run", help="Run the autonomous loop")
    run_parser.add_argument(
        "-i", "--iterations", type=int, default=1, help="Max work items to process"
    )
    run_parser.add_argument(
        "--runtime",
        choices=RUNTIMES,
        default="claude",
        help="Runtime to drive the loop (default: claude; stub is selectable)",
    )
    run_parser.add_argument(
        "--assignee",
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
        "--sandbox",
        choices=("host", "docker"),
        default="host",
        help=(
            "Where the runtime runs: on the host (default) or inside the "
            "Docker agent sandbox"
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


def _build_runtime(runtime_name: str, sandbox_kind: str, workspace: Path) -> Runtime:
    """Build the Runtime for a run, sandboxed in Docker when asked.

    ``--sandbox docker`` wraps each phase's inner agent argv into a
    ``docker run`` argv, so both the Runtime and the commands it invokes run
    inside the agent container. Both Claude and Codex support this; every other
    combination runs the runtime on the host as before. The docker-vs-host
    choice is orthogonal to which runtime runs.
    """
    if sandbox_kind == "docker":
        if runtime_name == "claude":
            return ClaudeRuntime.in_docker(workspace=workspace)
        if runtime_name == "codex":
            return CodexRuntime.in_docker(workspace=workspace)
        raise NotImplementedError(
            f"The Docker sandbox for the {runtime_name!r} runtime is not "
            "available; run it with --sandbox host for now."
        )
    return make_runtime(runtime_name)


def _make_run_id() -> str:
    """Return a timestamp-based run id for a fresh run."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _cmd_run(args: argparse.Namespace) -> int:
    """Dispatch ``pycastle run``: work up to ``--iterations`` issues into one PR."""
    workspace = Path.cwd()
    runtime = _build_runtime(args.runtime, args.sandbox, workspace)
    repo = _resolve_repo()
    base_branch = _resolve_base_branch()
    assignee = _resolve_assignee(args.assignee)
    issue_source = GitHubIssueSource(repo)

    # The quality gate is the project's own: it comes from the fixture's `gate`
    # file (run in each issue worktree after implement), not hardcoded here. With
    # no gate file the check falls back to always-pass, so a project without a
    # gate keeps the single-attempt behaviour. Wiring it here is what makes the
    # retry-with-handoff path reachable through the real `pycastle run` (#14).
    gate_check = make_fixture_gate_check(FIXTURE_DIR)

    outcome = run_loop(
        runtime=runtime,
        issue_source=issue_source,
        fixture_dir=FIXTURE_DIR,
        repo=repo,
        base_branch=base_branch,
        assignee=assignee,
        run_id=_make_run_id(),
        iterations=args.iterations,
        include_unassigned=args.include_unassigned,
        gate_check=gate_check,
    )
    if not outcome.issues:
        logger.info("Nothing to do.")
        return 0
    logger.info(
        "Run %s worked %d issue(s), merged %s; PR opened: %s",
        outcome.run_id,
        len(outcome.issues),
        outcome.completed,
        outcome.pr_opened,
    )
    return 0 if outcome.pr_opened else 1


def _cmd_sandbox_setup(args: argparse.Namespace) -> int:
    """Dispatch ``pycastle sandbox setup``: onboard a runtime's Docker auth.

    Both Claude and Codex log into their per-Runtime auth volume. Credential
    contents are never read or printed.

    Claude runs the interactive browser login and then confirms auth from a
    *fresh* container by having the agent answer a one-word prompt; the login
    needs a TTY and the headless token fallback is documented in
    :mod:`pycastle.sandbox`.

    Codex runs the device-authorization login (``codex login --device-auth``),
    which prints a code and a verification URL and polls in the background — no
    localhost callback and no TTY. The flow's own zero exit is the success
    signal, so no fresh-container status check is run.
    """
    if args.runtime == "codex":
        return _setup_codex()
    if args.runtime == "claude":
        return _setup_claude()
    logger.error(
        "`pycastle sandbox setup --runtime %s` is not supported.", args.runtime
    )
    return 2


def _setup_claude() -> int:
    """Run the Claude browser login, then confirm auth from a fresh container."""
    logger.info(
        "Logging the claude runtime into volume %s", sandbox.auth_volume("claude")
    )
    login = run_cmd(sandbox.build_login_command("claude"))
    if getattr(login, "returncode", 1) != 0:
        logger.error("Login failed; the auth volume was not onboarded.")
        return 1

    logger.info("Confirming auth from a fresh container...")
    status = run_cmd(sandbox.build_status_command("claude"))
    if getattr(status, "returncode", 1) != 0:
        logger.error("Fresh-container auth check failed; credentials are not usable.")
        return 1

    logger.info("The claude runtime is authenticated and ready.")
    return 0


def _setup_codex() -> int:
    """Run the Codex device-authorization login into its auth volume."""
    logger.info(
        "Logging the codex runtime into volume %s via the device-authorization "
        "flow; follow the printed code and URL to finish.",
        sandbox.auth_volume("codex"),
    )
    login = run_cmd(sandbox.build_login_command("codex"))
    if getattr(login, "returncode", 1) != 0:
        logger.error("Device-authorization login failed; the volume was not onboarded.")
        return 1

    logger.info("The codex runtime is authenticated and ready.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run preflight, and dispatch the chosen command."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    required = ["git", "gh"]
    if args.command == "run":
        if args.sandbox == "docker":
            # Docker is the isolation boundary: the host needs docker, not the
            # agent CLI (which lives inside the image).
            required.append("docker")
        elif args.runtime in {"claude", "codex"}:
            required.append(args.runtime)
    if args.command == "sandbox":
        required.append("docker")
    try:
        check_required_commands(required)
    except PreflightError as exc:
        logger.error("%s", exc)
        return 1

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "init":
        logger.error("`pycastle init` lands in a later slice.")
        return 2
    if args.command == "sandbox":
        return _cmd_sandbox_setup(args)
    return 2
