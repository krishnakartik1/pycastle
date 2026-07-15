"""The ``pycastle`` command-line entry point."""

from __future__ import annotations

import argparse
import datetime
import logging
from collections.abc import Sequence
from pathlib import Path

from . import __version__, sandbox
from .commands import run_cmd
from .compatibility import FixtureCompatibilityError, require_fixture_compatibility
from .issues import GitHubIssueSource
from .orchestrator import (
    PruneError,
    make_fixture_gate_check,
    make_fixture_setup,
    prune_run_branches,
)
from .orchestrator import run_batch as run_loop
from .preflight import (
    PreflightError,
    check_docker_gate_toolchain,
    check_required_commands,
)
from .runtime import ClaudeRuntime, CodexRuntime, Runtime, make_runtime
from .scaffold import FixtureExistsError, read_sandbox, scaffold_fixture
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

    sub.add_parser("init", help="Scaffold a .pycastle/ Project fixture into this repo")
    sub.add_parser("upgrade", help="Migrate this repo's Project fixture")
    sub.add_parser("prune", help="Delete run branches whose PRs are no longer open")

    sandbox = sub.add_parser("sandbox", help="Manage the Docker agent sandbox")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)
    setup = sandbox_sub.add_parser(
        "setup", help="Log a runtime into its Docker auth volume (coming soon)"
    )
    setup.add_argument("--runtime", choices=RUNTIMES, default="claude")
    setup.add_argument(
        "--image",
        default=None,
        help=(
            "Agent image to onboard auth against (bring-your-own-image). When "
            "omitted, .pycastle/Dockerfile is built on demand into a "
            "content-addressed tag -- the same image `run` uses. With neither a "
            "Dockerfile nor --image present, setup errors (run `pycastle init`)."
        ),
    )
    sandbox_sub.add_parser(
        "build",
        help="Build .pycastle/Dockerfile into its content-addressed agent image",
    )

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
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Capture and surface the agent's thinking/reasoning trace, live as "
            "[THINKING:<phase>] lines and persisted under "
            ".pycastle/runs/<run_id>/. High-volume; off by default."
        ),
    )
    run_parser.add_argument(
        "--sandbox",
        choices=("host", "docker"),
        default=None,
        help=(
            "Where the runtime runs: on the host or inside the Docker agent "
            "sandbox. Defaults to the choice recorded in .pycastle/sandbox at "
            "init time, or host when that marker is absent."
        ),
    )
    run_parser.add_argument(
        "--image",
        default=None,
        help=(
            "Agent image to run in the Docker sandbox (bring-your-own-image). "
            "When omitted, .pycastle/Dockerfile is built on demand into a "
            "content-addressed tag, else the default tag is used."
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
    """Resolve the effective sandbox for a run: flag, then marker, then host.

    An explicit ``--sandbox`` flag wins. With no flag, the choice
    ``pycastle init`` recorded in ``.pycastle/sandbox`` is used. A missing,
    empty, or unrecognised marker falls back to ``host`` so a run never crashes
    on a garbled marker -- only ``host`` and ``docker`` are honoured.
    """
    if flag is not None:
        return flag
    recorded = read_sandbox(FIXTURE_DIR)
    if recorded in ("host", "docker"):
        return recorded
    return "host"


def _build_image_for_dockerfile(dockerfile_text: str, fixture_dir: Path) -> str:
    """Build ``fixture_dir``'s Dockerfile into its content-addressed tag if absent.

    Derives the tag from the recipe text (ADR-0005), then builds only when the
    tag is not already present. A successful ``docker image inspect <tag>``
    (exit 0) means the cached image exists and the build is skipped; any
    non-zero exit is treated as "absent → build", and that one ``docker build``
    is the only build invoked. Returns the resolved tag.
    """
    tag = sandbox.image_tag_for_dockerfile(dockerfile_text)
    inspect = run_cmd(["docker", "image", "inspect", tag], capture=True)
    if getattr(inspect, "returncode", 1) == 0:
        logger.info("Agent image %s is already built; skipping build.", tag)
        return tag
    logger.info("Building the agent image %s from %s ...", tag, fixture_dir)
    build = run_cmd(["docker", "build", "-t", tag, str(fixture_dir)])
    if getattr(build, "returncode", 1) != 0:
        # A failed build must not be swallowed: returning the tag here would let
        # the run proceed against an image that was never built (or a stale tag
        # from an earlier build), failing opaquely deeper in `docker run`.
        raise PreflightError(
            f"Failed to build the agent image {tag} from {fixture_dir}; "
            "fix the Dockerfile and retry."
        )
    return tag


def _resolve_agent_image(image_flag: str | None, fixture_dir: Path) -> str:
    """Resolve the agent image for a Docker run by ADR-0005's precedence.

    1. ``image_flag`` given → return it verbatim. The Dockerfile is never read
       and ``docker`` is never invoked (pure bring-your-own-image). An empty or
       whitespace-only flag raises :class:`PreflightError`: it would otherwise
       slot into the ``docker run`` argv as the image name, shifting the real
       inner argv and failing opaquely deep in ``docker run``.
    2. No flag, ``fixture_dir/Dockerfile`` exists → build it on demand into its
       content-addressed tag (skipping the build when the tag already exists)
       and return that tag.
    3. No flag, no Dockerfile → fall back to :data:`sandbox.DEFAULT_IMAGE`.

    Only ever called for a Docker run; a host run never resolves an image.
    """
    if image_flag is not None:
        if not image_flag.strip():
            raise PreflightError(
                "The --image value is empty; pass a real agent image tag, or "
                "omit --image to build .pycastle/Dockerfile or use the default."
            )
        return image_flag
    dockerfile = fixture_dir / DOCKERFILE_NAME
    if dockerfile.is_file():
        # A present-but-unreadable Dockerfile surfaces here rather than being
        # treated as absent: the read raises, it is not swallowed.
        return _build_image_for_dockerfile(dockerfile.read_text(), fixture_dir)
    return sandbox.DEFAULT_IMAGE


def _build_runtime(
    runtime_name: str,
    sandbox_kind: str,
    workspace: Path,
    *,
    image: str = sandbox.DEFAULT_IMAGE,
    verbose: bool = False,
) -> Runtime:
    """Build the Runtime for a run, sandboxed in Docker when asked.

    ``--sandbox docker`` wraps each phase's inner agent argv into a
    ``docker run`` argv, so both the Runtime and the commands it invokes run
    inside the agent container. ``image`` is the already-resolved agent image
    (see :func:`_resolve_agent_image`) and is threaded into the docker wrapper.
    Both Claude and Codex support this; every other combination runs the runtime
    on the host as before. The docker-vs-host choice is orthogonal to which
    runtime runs.

    ``verbose`` turns on transcript capture (thinking + output, #48/#52). It is
    threaded into the real Claude/Codex runtimes as a constructor attribute, so
    the ``Runtime.run`` signature stays unchanged. The per-issue transcript sink
    is bound later by the orchestrator (which owns ``run_id`` and the issue
    number); here the runtime is built only with ``verbose`` so live
    ``[THINKING:<phase>]`` and ``[OUTPUT:<phase>]`` surfacing turns on. With
    ``verbose`` off the host path stays the bare :func:`make_runtime` runtime, so
    nothing changes.
    """
    if sandbox_kind == "docker":
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


def _cmd_run(args: argparse.Namespace) -> int:
    """Dispatch ``pycastle run``: work up to ``--iterations`` issues into one PR."""
    workspace = Path.cwd()
    # This is deliberately the first Run operation. In particular, a Docker
    # image build and all git/gh resolution wait until the fixture is known safe.
    require_fixture_compatibility(FIXTURE_DIR)
    # Resolve the agent image once, before the run loop, so a missing image is
    # built exactly once rather than per iteration. Resolution is docker-only.
    # The single --sandbox flag drives BOTH the runtime and the gate onto the
    # same side (#28): under docker the gate runs inside the SAME resolved agent
    # image as the phases, wrapped through the same sandbox wrapper; under host it
    # runs as a host subprocess (unchanged). Building the gate-check here, in the
    # branch that already resolves the image, keeps them in lockstep.
    if args.sandbox == "docker":
        image = _resolve_agent_image(args.image, FIXTURE_DIR)
        check_docker_gate_toolchain(
            FIXTURE_DIR,
            image=image,
            runtime_name=args.runtime,
            workspace=workspace,
        )
        runtime = _build_runtime(
            args.runtime, args.sandbox, workspace, image=image, verbose=args.verbose
        )
        gate_check = make_fixture_gate_check(
            FIXTURE_DIR,
            sandbox="docker",
            image=image,
            runtime_name=args.runtime,
            workspace=workspace,
        )
        setup = make_fixture_setup(
            FIXTURE_DIR,
            sandbox="docker",
            image=image,
            runtime_name=args.runtime,
            workspace=workspace,
        )
    else:
        runtime = _build_runtime(
            args.runtime, args.sandbox, workspace, verbose=args.verbose
        )
        gate_check = make_fixture_gate_check(FIXTURE_DIR)
        setup = make_fixture_setup(FIXTURE_DIR)
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
        run_id=_make_run_id(),
        iterations=args.iterations,
        include_unassigned=args.include_unassigned,
        gate_check=gate_check,
        setup=setup,
        verbose=args.verbose,
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


def _cmd_prune() -> int:
    """Delete remote Run branches after their pull requests close or merge."""
    deleted = prune_run_branches(repo=_resolve_repo(), cwd=Path.cwd())
    if deleted:
        logger.info(
            "Deleted %d stale run branch(es): %s", len(deleted), ", ".join(deleted)
        )
    else:
        logger.info("No stale run branches found.")
    return 0


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

    The auth image is resolved through the *same* :func:`_resolve_agent_image`
    path ``run`` uses, so setup onboards auth against the exact image a run will
    drive (ADR-0006). With no ``--image`` and no ``.pycastle/Dockerfile`` there
    is no buildable image, so setup errors with guidance (``pycastle init``)
    rather than onboarding auth against the unbuildable default tag -- the one
    place setup diverges from ``run``'s precedence, which there falls back to the
    default image.
    """
    if args.image is None and not (FIXTURE_DIR / DOCKERFILE_NAME).is_file():
        logger.error(
            "No %s found and no --image given; run `pycastle init` to scaffold "
            "a Project fixture with an agent Dockerfile first, or pass --image.",
            FIXTURE_DIR / DOCKERFILE_NAME,
        )
        return 1
    image = _resolve_agent_image(args.image, FIXTURE_DIR)
    if args.runtime == "codex":
        return _setup_codex(image)
    if args.runtime == "claude":
        return _setup_claude(image)
    logger.error(
        "`pycastle sandbox setup --runtime %s` is not supported.", args.runtime
    )
    return 2


def _cmd_sandbox_build(_args: argparse.Namespace) -> int:
    """Dispatch ``pycastle sandbox build``: build the Dockerfile's agent image.

    Builds ``.pycastle/Dockerfile`` into its content-addressed tag via the same
    build path a Docker run takes implicitly (see
    :func:`_build_image_for_dockerfile`), so there is one build path, not two.
    Errors with guidance and a non-zero exit when no Dockerfile is present,
    rather than silently falling back to the default image.
    """
    dockerfile = FIXTURE_DIR / DOCKERFILE_NAME
    if not dockerfile.is_file():
        logger.error(
            "No %s found; run `pycastle init` to scaffold a Project fixture "
            "with an agent Dockerfile first.",
            dockerfile,
        )
        return 1
    tag = _build_image_for_dockerfile(dockerfile.read_text(), FIXTURE_DIR)
    logger.info("The agent image %s is built and ready.", tag)
    return 0


def _setup_claude(image: str) -> int:
    """Run the Claude browser login, then confirm auth from a fresh container.

    ``image`` is the already-resolved agent image (the same one ``run`` uses;
    see :func:`_resolve_agent_image`); both the login and the fresh-container
    status check run against it. The per-Runtime auth *volume* is independent of
    the image and shared across projects (ADR-0002), so credentials onboarded
    here are reused by every run, whatever image it resolves.
    """
    logger.info(
        "Logging the claude runtime into volume %s", sandbox.auth_volume("claude")
    )
    login = run_cmd(sandbox.build_login_command("claude", image=image))
    if getattr(login, "returncode", 1) != 0:
        logger.error("Login failed; the auth volume was not onboarded.")
        return 1

    logger.info("Confirming auth from a fresh container...")
    status = run_cmd(sandbox.build_status_command("claude", image=image))
    if getattr(status, "returncode", 1) != 0:
        logger.error("Fresh-container auth check failed; credentials are not usable.")
        return 1

    logger.info("The claude runtime is authenticated and ready.")
    return 0


def _setup_codex(image: str) -> int:
    """Run the Codex device-authorization login into its auth volume.

    ``image`` is the already-resolved agent image (the same one ``run`` uses;
    see :func:`_resolve_agent_image`); the login runs against it. The per-Runtime
    auth *volume* is independent of the image and shared across projects
    (ADR-0002), so the onboarded credentials are reused by every run.
    """
    logger.info(
        "Logging the codex runtime into volume %s via the device-authorization "
        "flow; follow the printed code and URL to finish.",
        sandbox.auth_volume("codex"),
    )
    login = run_cmd(sandbox.build_login_command("codex", image=image))
    if getattr(login, "returncode", 1) != 0:
        logger.error("Device-authorization login failed; the volume was not onboarded.")
        return 1

    logger.info("The codex runtime is authenticated and ready.")
    return 0


def _prompt_sandbox() -> str:
    """Ask whether execution is host-first or Docker-first; default to host.

    The default is host-first because it needs no Docker image build to run the
    scaffolded fixture. An empty answer (Enter) takes the default; ``docker``
    (or ``d``) picks Docker-first. This interactive prompt is not unit-tested;
    the scaffolding it drives is.
    """
    answer = input("Execution: [H]ost-first or [d]ocker-first? [H] ").strip().lower()
    return "docker" if answer in {"docker", "d"} else "host"


def _cmd_init(_args: argparse.Namespace) -> int:
    """Dispatch ``pycastle init``: scaffold the Project fixture into the cwd.

    Prompts for host-first vs Docker-first, then writes the fixture to match.
    Refuses to clobber an existing ``.pycastle/`` so a project's prompts, gate,
    and graph shape are never silently replaced.
    """
    choice = _prompt_sandbox()
    try:
        written = scaffold_fixture(Path.cwd(), sandbox=choice)  # type: ignore[arg-type]
    except FixtureExistsError as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "Scaffolded the PyCastle Project fixture (%s-first) into .pycastle/:",
        choice,
    )
    for path in written:
        logger.info("  %s", path.relative_to(Path.cwd()))
    logger.info(
        "Edit .pycastle/prompts/, .pycastle/gate, and .pycastle/main.py to "
        "customize the loop, then run `pycastle run`."
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
    if args.command == "run":
        # Resolve the effective sandbox (flag -> .pycastle/sandbox marker ->
        # host) before preflight so the required-command set matches where the
        # run will actually execute. The resolved value is written back onto
        # args so _cmd_run reads the same decision.
        args.sandbox = _resolve_sandbox(args.sandbox)
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
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "upgrade":
            return _cmd_upgrade()
        if args.command == "prune":
            return _cmd_prune()
        if args.command == "sandbox":
            if args.sandbox_command == "build":
                return _cmd_sandbox_build(args)
            return _cmd_sandbox_setup(args)
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
    return 2
