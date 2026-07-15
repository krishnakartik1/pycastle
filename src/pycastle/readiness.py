"""Shared, non-destructive readiness evaluation for Doctor and Run."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__, sandbox
from .commands import command_exists, run_cmd
from .compatibility import check_fixture_compatibility
from .graph import PhaseGraph, RunDefinition, Terminal, load_run
from .issues import GitHubIssueSource, select_batch

SCHEMA_VERSION = 1
SHORT_TIMEOUT = 15.0
IMAGE_BUILD_TIMEOUT = 900.0
CHECK_IDS = (
    "required_commands",
    "git_repository",
    "working_tree",
    "base_branch",
    "fixture_compatibility",
    "fixture_structure",
    "sandbox",
    "agent_image",
    "runtime",
    "runtime_authentication",
    "gate_toolchain",
    "github_authentication",
    "github_repository",
    "github_permissions",
    "workflow_labels",
    "eligible_items",
)


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True)
class ReadinessConfiguration:
    repository: str
    base_branch: str
    github_default_branch: str | None
    runtime: str
    sandbox: str
    agent_image: str | None
    assignee: str
    include_unassigned: bool
    item_limit: int


@dataclass(frozen=True, order=True)
class EligibleItem:
    number: int
    title: str


@dataclass(frozen=True)
class CheckResult:
    status: Status
    summary: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    remediation: str | None = None
    # Adapters may use raw output while deciding a result. It is deliberately
    # excluded from ReadinessCheck and therefore can never be rendered.
    unsafe_detail: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ReadinessCheck:
    id: str
    status: Status
    summary: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    remediation: str | None = None


@dataclass(frozen=True)
class ReadinessReport:
    schema_version: int
    ready: bool
    runner_version: str
    configuration: ReadinessConfiguration
    checks: tuple[ReadinessCheck, ...]
    eligible_items: tuple[EligibleItem, ...]


Probe = Callable[[str, ReadinessConfiguration], CheckResult]
ItemLoader = Callable[[ReadinessConfiguration], list[EligibleItem]]


@dataclass(frozen=True)
class ReadinessDependencies:
    probe: Probe
    eligible_items: ItemLoader


_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "working_tree": ("git_repository",),
    "base_branch": ("git_repository",),
    "fixture_structure": ("fixture_compatibility",),
    "sandbox": ("fixture_compatibility",),
    "agent_image": ("fixture_compatibility", "sandbox"),
    "runtime": ("sandbox", "agent_image"),
    "runtime_authentication": ("runtime",),
    "gate_toolchain": ("fixture_structure", "sandbox", "agent_image"),
    "github_repository": ("github_authentication", "git_repository"),
    "github_permissions": ("github_authentication", "github_repository"),
    "workflow_labels": ("github_authentication", "github_repository"),
    "eligible_items": ("github_authentication", "github_repository"),
}


def evaluate_readiness(
    configuration: ReadinessConfiguration, dependencies: ReadinessDependencies
) -> ReadinessReport:
    """Evaluate every independent check and block only true dependants."""
    checks: list[ReadinessCheck] = []
    outcomes: dict[str, Status] = {}
    items: list[EligibleItem] = []
    for check_id in CHECK_IDS:
        failed = [
            prerequisite
            for prerequisite in _PREREQUISITES.get(check_id, ())
            if outcomes.get(prerequisite) in {Status.FAIL, Status.BLOCKED}
            or (
                prerequisite == "agent_image"
                and outcomes.get(prerequisite) is Status.NOT_APPLICABLE
                and configuration.sandbox == "docker"
            )
        ]
        if failed:
            result = CheckResult(
                Status.BLOCKED,
                "Blocked by failed prerequisite(s): " + ", ".join(failed),
                {"prerequisites": failed},
                "Resolve the prerequisite checks, then run Doctor again.",
            )
        elif check_id == "eligible_items":
            try:
                items = sorted(dependencies.eligible_items(configuration))
                result = (
                    CheckResult(
                        Status.PASS,
                        f"{len(items)} eligible Item(s) selected.",
                        {"count": len(items)},
                    )
                    if items
                    else CheckResult(
                        Status.FAIL,
                        "No eligible Items match the resolved Run configuration.",
                        {"count": 0},
                        "Assign or label an Item ready-for-agent, or adjust the assignee policy.",
                    )
                )
            except (
                OSError,
                ValueError,
                json.JSONDecodeError,
                subprocess.TimeoutExpired,
            ):
                result = CheckResult(
                    Status.FAIL,
                    "Eligible Items could not be listed safely.",
                    remediation="Verify GitHub access and retry Doctor.",
                )
        else:
            try:
                result = dependencies.probe(check_id, configuration)
            except subprocess.TimeoutExpired:
                result = CheckResult(
                    Status.FAIL,
                    "The readiness probe timed out.",
                    remediation="Verify the external tool is responsive and retry Doctor.",
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                result = CheckResult(
                    Status.FAIL,
                    "The readiness probe could not complete.",
                    remediation="Correct the configuration and retry Doctor.",
                )
        check = ReadinessCheck(
            check_id,
            result.status,
            result.summary[:500],
            dict(result.facts),
            result.remediation[:500] if result.remediation else None,
        )
        checks.append(check)
        outcomes[check_id] = result.status
    ready = all(c.status in {Status.PASS, Status.NOT_APPLICABLE} for c in checks)
    return ReadinessReport(
        SCHEMA_VERSION,
        ready,
        __version__,
        configuration,
        tuple(checks),
        tuple(items),
    )


def report_document(report: ReadinessReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "ready": report.ready,
        "runner_version": report.runner_version,
        "configuration": asdict(report.configuration),
        "checks": [
            {
                "id": check.id,
                "status": check.status.value,
                "summary": check.summary,
                "facts": dict(check.facts),
                "remediation": check.remediation,
            }
            for check in report.checks
        ],
        "eligible_items": [asdict(item) for item in report.eligible_items],
    }


def render_json(report: ReadinessReport) -> str:
    return json.dumps(
        report_document(report), separators=(",", ":"), ensure_ascii=False
    )


def render_human(report: ReadinessReport) -> str:
    lines = [
        f"PyCastle Doctor: {'ready' if report.ready else 'not ready'}",
        f"Repository: {report.configuration.repository}",
        f"Base branch: {report.configuration.base_branch} "
        f"(GitHub default: {report.configuration.github_default_branch or 'unknown'})",
    ]
    for check in report.checks:
        label = check.status.value.replace("_", " ")
        lines.append(f"[{label}] {check.id}: {check.summary}")
        if check.remediation and check.status in {Status.FAIL, Status.BLOCKED}:
            lines.append(f"  Fix: {check.remediation}")
    for item in report.eligible_items:
        lines.append(f"  #{item.number} {item.title}")
    return "\n".join(lines)


class DefaultReadinessAdapter:
    """Read-only production probes with one bounded command invocation each."""

    def __init__(
        self,
        fixture_dir: Path,
        workspace: Path,
        *,
        runner: Callable[..., Any] = run_cmd,
        exists: Callable[[str], bool] = command_exists,
    ) -> None:
        self.fixture_dir = fixture_dir
        self.workspace = workspace
        self.runner = runner
        self.exists = exists

    def _run(self, argv: list[str], *, timeout: float = SHORT_TIMEOUT) -> Any:
        return self.runner(argv, cwd=self.workspace, capture=True, timeout=timeout)

    @staticmethod
    def _ok(result: Any) -> bool:
        return getattr(result, "returncode", 1) == 0

    def probe(self, check_id: str, config: ReadinessConfiguration) -> CheckResult:
        method = getattr(self, f"check_{check_id}")
        return method(config)

    def check_required_commands(self, config: ReadinessConfiguration) -> CheckResult:
        required = ["git", "gh"]
        required.append("docker" if config.sandbox == "docker" else config.runtime)
        required = [name for name in required if name != "stub"]
        missing = [name for name in required if not self.exists(name)]
        return (
            CheckResult(
                Status.FAIL,
                "Required host commands are missing.",
                {"missing": missing},
                "Install the missing commands and retry.",
            )
            if missing
            else CheckResult(
                Status.PASS,
                "Required host commands are available.",
                {"commands": required},
            )
        )

    def check_git_repository(self, _config: ReadinessConfiguration) -> CheckResult:
        result = self._run(["git", "rev-parse", "--show-toplevel"])
        return (
            CheckResult(Status.PASS, "Checkout is a Git repository.")
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Checkout is not a Git repository.",
                remediation="Run Doctor from the intended repository checkout.",
            )
        )

    def check_working_tree(self, _config: ReadinessConfiguration) -> CheckResult:
        branch = self._run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
        if not self._ok(branch):
            return CheckResult(
                Status.FAIL,
                "HEAD is detached.",
                remediation="Check out an attached branch.",
            )
        status = self._run(["git", "status", "--porcelain", "--untracked-files=all"])
        clean = self._ok(status) and not (getattr(status, "stdout", "") or "").strip()
        return (
            CheckResult(Status.PASS, "Working tree is attached and clean.")
            if clean
            else CheckResult(
                Status.FAIL,
                "Working tree has tracked or untracked changes.",
                remediation="Commit, stash, or remove the changes and retry.",
            )
        )

    def check_base_branch(self, config: ReadinessConfiguration) -> CheckResult:
        result = self._run(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{config.base_branch}",
            ]
        )
        facts = {
            "selected": config.base_branch,
            "github_default": config.github_default_branch,
        }
        return (
            CheckResult(Status.PASS, "Selected base branch exists on origin.", facts)
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Selected base branch is not reachable on origin.",
                facts,
                "Push the selected base branch or choose an existing remote branch.",
            )
        )

    def check_fixture_compatibility(
        self, _config: ReadinessConfiguration
    ) -> CheckResult:
        result = check_fixture_compatibility(self.fixture_dir)
        facts = {
            "fixture_version": (
                str(result.fixture_version) if result.fixture_version else None
            ),
            "runner_version": str(result.runner_version),
        }
        return (
            CheckResult(Status.PASS, result.message, facts)
            if result.compatible
            else CheckResult(
                Status.FAIL,
                result.message,
                facts,
                "Run `pycastle upgrade` or install a compatible runner.",
            )
        )

    def check_fixture_structure(self, _config: ReadinessConfiguration) -> CheckResult:
        old = sys.dont_write_bytecode
        try:
            sys.dont_write_bytecode = True
            definition = load_run(self.fixture_dir)
            _validate_run_definition(definition, self.fixture_dir)
        finally:
            sys.dont_write_bytecode = old
        return CheckResult(
            Status.PASS, "Complete Run definition and fixture executables are valid."
        )

    def check_sandbox(self, config: ReadinessConfiguration) -> CheckResult:
        if config.sandbox not in {"host", "docker"}:
            return CheckResult(
                Status.FAIL, "Unknown Sandbox.", remediation="Select host or docker."
            )
        return CheckResult(
            Status.PASS,
            f"{config.sandbox} Sandbox is selected.",
            {"sandbox": config.sandbox},
        )

    def check_agent_image(self, config: ReadinessConfiguration) -> CheckResult:
        if config.sandbox == "host":
            return CheckResult(
                Status.NOT_APPLICABLE, "Host Sandbox uses no Agent image."
            )
        if not config.agent_image:
            return CheckResult(
                Status.FAIL,
                "No Agent image was resolved.",
                remediation="Provide --image or add .pycastle/Dockerfile.",
            )
        dockerfile = self.fixture_dir / "Dockerfile"
        if (
            dockerfile.is_file()
            and config.agent_image
            == sandbox.image_tag_for_dockerfile(dockerfile.read_text())
        ):
            inspect = self._run(["docker", "image", "inspect", config.agent_image])
            if not self._ok(inspect):
                build = self._run(
                    [
                        "docker",
                        "build",
                        "-t",
                        config.agent_image,
                        str(self.fixture_dir),
                    ],
                    timeout=IMAGE_BUILD_TIMEOUT,
                )
                if not self._ok(build):
                    return CheckResult(
                        Status.FAIL,
                        "Agent image build failed.",
                        {"image": config.agent_image},
                        "Fix .pycastle/Dockerfile and retry.",
                    )
        inner = [
            "sh",
            "-c",
            f'test "$(id -un)" = node && test "$HOME" = {sandbox.SANDBOX_HOME} && command -v {config.runtime} >/dev/null && test -w "$HOME" && test -w "$PWD"',
        ]
        argv = sandbox.build_run_command(
            config.runtime,
            inner_argv=inner,
            workspace=self.workspace,
            image=config.agent_image,
        )
        result = self._run(argv)
        return (
            CheckResult(
                Status.PASS,
                "Agent image satisfies the runtime contract.",
                {"image": config.agent_image},
            )
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Agent image does not satisfy the runtime contract.",
                {"image": config.agent_image},
                "Fix the image user, home, Runtime PATH, Auth volume, or workspace permissions.",
            )
        )

    def check_runtime(self, config: ReadinessConfiguration) -> CheckResult:
        if config.runtime == "stub":
            return CheckResult(Status.PASS, "Stub Runtime is available.")
        argv = [config.runtime, "--version"]
        if config.sandbox == "docker":
            argv = sandbox.build_run_command(
                config.runtime,
                inner_argv=argv,
                workspace=self.workspace,
                image=config.agent_image or sandbox.DEFAULT_IMAGE,
            )
        result = self._run(argv)
        return (
            CheckResult(Status.PASS, "Runtime is present and launchable.")
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Runtime is not launchable.",
                remediation="Install or repair the selected Runtime.",
            )
        )

    def check_runtime_authentication(
        self, config: ReadinessConfiguration
    ) -> CheckResult:
        if config.runtime == "stub":
            return CheckResult(
                Status.NOT_APPLICABLE, "Stub Runtime needs no authentication."
            )
        argv = list(sandbox.RUNTIME_CONFIG[config.runtime].status_args)
        if config.sandbox == "docker":
            argv = sandbox.build_status_command(
                config.runtime, image=config.agent_image or sandbox.DEFAULT_IMAGE
            )
        result = self._run(argv)
        return (
            CheckResult(Status.PASS, "Runtime native authentication status passed.")
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Runtime is not authenticated.",
                remediation=f"Authenticate {config.runtime} in the selected Sandbox.",
            )
        )

    def check_gate_toolchain(self, config: ReadinessConfiguration) -> CheckResult:
        gate = self.fixture_dir / "gate"
        if not gate.is_file():
            return CheckResult(Status.NOT_APPLICABLE, "The optional Gate is absent.")
        argv = [str(gate.resolve()), "--check-tools"]
        if config.sandbox == "docker":
            argv = sandbox.build_run_command(
                config.runtime,
                inner_argv=argv,
                workspace=self.workspace,
                image=config.agent_image or sandbox.DEFAULT_IMAGE,
            )
        result = self._run(argv)
        return (
            CheckResult(Status.PASS, "Gate toolchain check passed.")
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Gate toolchain check failed.",
                remediation="Install the project toolchain in the selected Sandbox.",
            )
        )

    def check_github_authentication(
        self, _config: ReadinessConfiguration
    ) -> CheckResult:
        result = self._run(["gh", "auth", "status"])
        return (
            CheckResult(Status.PASS, "GitHub CLI authentication is valid.")
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "GitHub CLI authentication failed.",
                remediation="Run `gh auth login` and retry.",
            )
        )

    def check_github_repository(self, config: ReadinessConfiguration) -> CheckResult:
        result = self._run(
            ["gh", "repo", "view", config.repository, "--json", "nameWithOwner"]
        )
        return (
            CheckResult(
                Status.PASS,
                "GitHub repository identity is reachable.",
                {"repository": config.repository},
            )
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "GitHub repository identity is not reachable.",
                remediation="Verify origin and repository access.",
            )
        )

    def check_github_permissions(self, config: ReadinessConfiguration) -> CheckResult:
        result = self._run(
            ["gh", "api", f"repos/{config.repository}", "--jq", ".permissions.push"]
        )
        allowed = (
            self._ok(result)
            and (getattr(result, "stdout", "") or "").strip().lower() == "true"
        )
        return (
            CheckResult(Status.PASS, "GitHub repository write permission is available.")
            if allowed
            else CheckResult(
                Status.FAIL,
                "GitHub repository write permission is unavailable.",
                remediation="Request write-level repository permission.",
            )
        )

    def check_workflow_labels(self, config: ReadinessConfiguration) -> CheckResult:
        result = self._run(
            [
                "gh",
                "label",
                "list",
                "-R",
                config.repository,
                "--json",
                "name",
                "--limit",
                "100",
            ]
        )
        try:
            labels = {
                row["name"] for row in json.loads(getattr(result, "stdout", "") or "[]")
            }
        except (KeyError, TypeError):
            labels = set()
        missing = sorted({"ready-for-agent", "ready-for-human"} - labels)
        return (
            CheckResult(Status.PASS, "Required workflow labels exist.")
            if self._ok(result) and not missing
            else CheckResult(
                Status.FAIL,
                "Required workflow labels are missing.",
                {"missing": missing},
                "Create the required workflow labels.",
            )
        )

    def eligible_items(self, config: ReadinessConfiguration) -> list[EligibleItem]:
        source = GitHubIssueSource(config.repository, runner=self.runner)
        issues = source.list_ready_metadata(timeout=SHORT_TIMEOUT)
        selected = select_batch(
            issues,
            assignee=config.assignee,
            include_unassigned=config.include_unassigned,
            limit=config.item_limit,
        )
        return [EligibleItem(issue.number, issue.title) for issue in selected]

    def dependencies(self) -> ReadinessDependencies:
        return ReadinessDependencies(self.probe, self.eligible_items)


def _validate_run_definition(definition: RunDefinition, fixture_dir: Path) -> None:
    prompts = (fixture_dir / "prompts").resolve()
    for scope, graph in (
        ("before", definition.before),
        ("item", definition.item),
        ("after", definition.after),
    ):
        if graph is None:
            continue
        if not isinstance(graph, PhaseGraph) or graph.start not in graph.phases:
            raise ValueError(f"Invalid {scope} graph")
        for phase in graph.phases.values():
            for target in (phase.on_success, phase.on_failure):
                if not isinstance(target, Terminal) and target not in graph.phases:
                    raise ValueError(f"Invalid edge in {scope} graph")
            path = (prompts / phase.prompt).resolve()
            if prompts not in path.parents or not path.is_file() or path.is_symlink():
                raise ValueError(f"Invalid prompt path in {scope} graph")
    for name in ("setup", "gate"):
        path = fixture_dir / name
        if path.exists() and (
            not path.is_file() or path.is_symlink() or not path.stat().st_mode & 0o111
        ):
            raise ValueError(f"Invalid {name} executable")
