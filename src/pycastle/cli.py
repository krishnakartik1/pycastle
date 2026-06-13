"""The ``pycastle`` command-line entry point."""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from .commands import run_cmd
from .issues import GitHubIssueSource
from .orchestrator import run as run_loop
from .preflight import PreflightError, check_required_commands
from .runtime import make_runtime

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


def _cmd_run(args: argparse.Namespace) -> int:
    """Dispatch ``pycastle run``."""
    runtime = make_runtime(args.runtime)
    repo = _resolve_repo()
    base_branch = _resolve_base_branch()
    assignee = _resolve_assignee(args.assignee)
    issue_source = GitHubIssueSource(repo)

    outcome = run_loop(
        runtime=runtime,
        issue_source=issue_source,
        fixture_dir=FIXTURE_DIR,
        repo=repo,
        base_branch=base_branch,
        assignee=assignee,
        include_unassigned=args.include_unassigned,
    )
    if outcome.issue is None:
        logger.info("Nothing to do.")
        return 0
    logger.info("Worked #%s; PR opened: %s", outcome.issue.number, outcome.pr_opened)
    return 0 if outcome.pr_opened else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run preflight, and dispatch the chosen command."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    required = ["git", "gh"]
    if args.command == "run" and args.runtime in {"claude", "codex"}:
        required.append(args.runtime)
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
        logger.error("`pycastle sandbox setup` lands in a later slice.")
        return 2
    return 2
