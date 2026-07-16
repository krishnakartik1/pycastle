"""Shared, non-destructive readiness evaluation for Doctor and Run."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__, sandbox
from .commands import command_exists, run_cmd
from .compatibility import check_fixture_compatibility
from .graph import (
    ExecutionGraph,
    GateNode,
    RunDefinition,
    RuntimeNode,
    Terminal,
    load_run,
)
from .issues import GitHubIssueSource, select_batch
from .models import IssueRef

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
    "github_authentication",
    "github_repository",
    "github_permissions",
    "workflow_labels",
    "eligible_items",
    "agent_image",
    "frozen_execution_inputs",
    "runtime",
    "runtime_authentication",
)


class AgentImagePreparationError(RuntimeError):
    """The resolved Agent image could not be prepared safely."""


def prepare_agent_image(
    fixture_dir: Path,
    *,
    runner: Callable[..., Any] = run_cmd,
    cwd: Path | None = None,
    capture_build: bool = False,
    inspect_timeout: float | None = None,
    build_timeout: float | None = None,
) -> str:
    """Build the canonical Dockerfile and return its immutable image ID."""
    dockerfile = fixture_dir / "Dockerfile"
    if dockerfile.is_symlink() or not dockerfile.is_file():
        raise AgentImagePreparationError(
            "The canonical Dockerfile is missing or unsafe."
        )
    repository_root = Path(cwd or fixture_dir.parent).resolve()
    image = f"pycastle-readiness:{uuid.uuid4().hex}"
    run_options: dict[str, Any] = {}
    run_options["cwd"] = repository_root
    build_options = dict(run_options)
    if build_timeout is not None:
        build_options["timeout"] = build_timeout
    build = runner(
        [
            "docker",
            "build",
            "--file",
            str(dockerfile.resolve()),
            "--tag",
            image,
            str(repository_root),
        ],
        capture=capture_build,
        **build_options,
    )
    if getattr(build, "returncode", 1) != 0:
        raise AgentImagePreparationError("Agent image build failed.")
    inspect_options = dict(run_options)
    if inspect_timeout is not None:
        inspect_options["timeout"] = inspect_timeout
    inspect = runner(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture=True,
        **inspect_options,
    )
    identity = (getattr(inspect, "stdout", "") or "").strip()
    if getattr(inspect, "returncode", 1) != 0 or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", identity
    ):
        raise AgentImagePreparationError(
            "Docker did not resolve an immutable image ID."
        )
    return identity


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class ReadinessOutcome(StrEnum):
    READY = "ready"
    NO_WORK = "no_work"
    NOT_READY = "not_ready"


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
class FrozenFixtureFile:
    relative_path: str
    content: bytes = field(repr=False)
    mode: int


@dataclass(frozen=True)
class FrozenProjectFixture:
    identity: str
    run_definition: RunDefinition = field(repr=False, compare=False)
    files: tuple[FrozenFixtureFile, ...] = field(repr=False, compare=False)


@dataclass(frozen=True)
class FrozenReadinessInputs:
    base_commit: str
    project_fixture: FrozenProjectFixture
    items: tuple[IssueRef, ...] = field(repr=False)
    sandbox: str
    runtime: str
    agent_image: str | None


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
    outcome: ReadinessOutcome
    runner_version: str
    configuration: ReadinessConfiguration
    checks: tuple[ReadinessCheck, ...]
    eligible_items: tuple[EligibleItem, ...]
    # Run-only data. Renderers deliberately expose only ``eligible_items``.
    selected_items: tuple[IssueRef, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )
    frozen_inputs: FrozenReadinessInputs | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def ready(self) -> bool:
        """Compatibility projection; callers should consume ``outcome``."""
        return self.outcome is ReadinessOutcome.READY


Probe = Callable[[str, ReadinessConfiguration], CheckResult]
ItemLoader = Callable[[ReadinessConfiguration], list[EligibleItem | IssueRef]]
InputFreezer = Callable[
    [ReadinessConfiguration, tuple[IssueRef, ...]], FrozenReadinessInputs
]
Progress = Callable[[str, str, Status | None], None]


@dataclass(frozen=True)
class ReadinessDependencies:
    probe: Probe
    eligible_items: ItemLoader
    freeze_inputs: InputFreezer | None = None


_PREREQUISITES: dict[str, tuple[str, ...]] = {
    "working_tree": ("git_repository",),
    "base_branch": ("git_repository",),
    "fixture_structure": ("fixture_compatibility",),
    "sandbox": ("fixture_compatibility",),
    "github_repository": ("github_authentication", "git_repository"),
    "github_permissions": ("github_authentication", "github_repository"),
    "workflow_labels": ("github_authentication", "github_repository"),
    "eligible_items": ("github_authentication", "github_repository"),
    "agent_image": (
        "fixture_compatibility",
        "fixture_structure",
        "sandbox",
        "eligible_items",
    ),
    "frozen_execution_inputs": (
        "base_branch",
        "fixture_structure",
        "sandbox",
        "eligible_items",
        "agent_image",
    ),
    "runtime": ("sandbox", "agent_image", "frozen_execution_inputs", "eligible_items"),
    "runtime_authentication": ("runtime", "eligible_items"),
}


def evaluate_readiness(
    configuration: ReadinessConfiguration,
    dependencies: ReadinessDependencies,
    *,
    progress: Progress | None = None,
) -> ReadinessReport:
    """Evaluate every independent check and block only true dependants."""
    checks: list[ReadinessCheck] = []
    outcomes: dict[str, Status] = {}
    items: list[EligibleItem] = []
    selected_items: tuple[IssueRef, ...] = ()
    frozen_inputs: FrozenReadinessInputs | None = None
    no_work = False
    for check_id in CHECK_IDS:
        if progress is not None:
            progress("start", check_id, None)
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
        if no_work and check_id in {
            "frozen_execution_inputs",
            "agent_image",
            "runtime",
            "runtime_authentication",
        }:
            result = CheckResult(
                Status.NOT_APPLICABLE,
                "No eligible Items require execution coordination.",
            )
        elif failed:
            result = CheckResult(
                Status.BLOCKED,
                "Blocked by failed prerequisite(s): " + ", ".join(failed),
                {"prerequisites": failed},
                "Resolve the prerequisite checks, then run Doctor again.",
            )
        elif check_id == "frozen_execution_inputs":
            if dependencies.freeze_inputs is None:
                result = CheckResult(Status.PASS, "Execution inputs are frozen.")
            else:
                try:
                    frozen_inputs = dependencies.freeze_inputs(
                        configuration, selected_items
                    )
                    result = CheckResult(
                        Status.PASS,
                        "Exact base, Project fixture, Item batch, and host configuration are frozen.",
                        {
                            "base_commit": frozen_inputs.base_commit,
                            "fixture_identity": frozen_inputs.project_fixture.identity,
                        },
                    )
                except (OSError, TypeError, ValueError):
                    result = CheckResult(
                        Status.FAIL,
                        "Execution inputs could not be frozen safely.",
                        remediation="Resolve the failed identity checks and retry Doctor.",
                    )
        elif check_id == "eligible_items":
            try:
                loaded_items = dependencies.eligible_items(configuration)
                if not isinstance(loaded_items, list):
                    raise TypeError("Eligible Item metadata must be a list")
                safe_items: list[EligibleItem] = []
                full_items: list[IssueRef] = []
                for item in loaded_items:
                    safe_items.append(_safe_eligible_item(item))
                    if isinstance(item, IssueRef):
                        full_items.append(item.model_copy(deep=True))
                order = sorted(
                    range(len(safe_items)), key=lambda index: safe_items[index]
                )
                items = [safe_items[index] for index in order]
                if len(full_items) == len(loaded_items):
                    selected_items = tuple(full_items[index] for index in order)
                result = CheckResult(
                    Status.PASS,
                    f"{len(items)} eligible Item(s) selected.",
                    {"count": len(items)},
                )
                no_work = not items
            except (
                AttributeError,
                KeyError,
                OSError,
                TypeError,
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
                if not isinstance(result, CheckResult):
                    raise TypeError("Probe returned an invalid result")
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
            _bounded_text(result.summary, 500),
            _safe_facts(check_id, result.facts),
            _bounded_text(result.remediation, 500) if result.remediation else None,
        )
        checks.append(check)
        outcomes[check_id] = result.status
        if progress is not None:
            progress("complete", check_id, result.status)
    successful = all(c.status in {Status.PASS, Status.NOT_APPLICABLE} for c in checks)
    if successful and no_work:
        outcome = ReadinessOutcome.NO_WORK
    elif successful:
        outcome = ReadinessOutcome.READY
    else:
        outcome = ReadinessOutcome.NOT_READY
    return ReadinessReport(
        SCHEMA_VERSION,
        outcome,
        __version__,
        configuration,
        tuple(checks),
        tuple(items),
        selected_items,
        frozen_inputs,
    )


_FACT_KEYS: dict[str, dict[str, str]] = {
    "required_commands": {"commands": "list", "missing": "list"},
    "base_branch": {"selected": "text", "github_default": "optional_text"},
    "fixture_compatibility": {
        "fixture_version": "optional_text",
        "runner_version": "text",
    },
    "sandbox": {"sandbox": "text"},
    "agent_image": {"image": "text"},
    "runtime": {"version": "text"},
    "workflow_labels": {"missing": "list"},
    "eligible_items": {"count": "integer"},
    "frozen_execution_inputs": {
        "base_commit": "text",
        "fixture_identity": "text",
    },
}


def _bounded_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if char.isprintable())[:limit]


def _safe_eligible_item(item: object) -> EligibleItem:
    """Validate and bound Issue metadata before it enters a report."""
    if not isinstance(item, EligibleItem | IssueRef):
        raise TypeError("Eligible Item metadata has an invalid shape")
    if (
        not isinstance(item.number, int)
        or isinstance(item.number, bool)
        or item.number < 1
    ):
        raise ValueError("Eligible Item number must be a positive integer")
    if not isinstance(item.title, str):
        raise TypeError("Eligible Item title must be text")
    return EligibleItem(item.number, _bounded_text(item.title, 200))


def _safe_fact_text(value: object, limit: int) -> str:
    bounded = _bounded_text(value, limit)
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._()+/@:-"
    )
    return bounded if bounded and all(char in allowed for char in bounded) else ""


def _safe_facts(check_id: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(facts, Mapping):
        return {}
    if "prerequisites" in facts:
        values = facts["prerequisites"]
        if isinstance(values, list):
            allowed = set(_PREREQUISITES.get(check_id, ()))
            return {
                "prerequisites": [
                    value
                    for value in values
                    if isinstance(value, str) and value in allowed
                ]
            }
        return {}
    safe: dict[str, Any] = {}
    for key, kind in _FACT_KEYS.get(check_id, {}).items():
        value = facts.get(key)
        if kind == "integer" and isinstance(value, int) and not isinstance(value, bool):
            safe[key] = max(0, min(value, 10_000))
        elif kind == "optional_text" and value is None:
            safe[key] = None
        elif kind in {"text", "optional_text"} and isinstance(value, str):
            bounded = _safe_fact_text(value, 200)
            if bounded:
                safe[key] = bounded
        elif kind == "list" and isinstance(value, list | tuple):
            safe[key] = [
                bounded
                for item in value[:20]
                if isinstance(item, str) and (bounded := _safe_fact_text(item, 100))
            ]
    return safe


def report_document(report: ReadinessReport) -> dict[str, Any]:
    configuration = asdict(report.configuration)
    configuration = {
        key: (
            _safe_configuration_value(key, value) if isinstance(value, str) else value
        )
        for key, value in configuration.items()
    }
    return {
        "schema_version": report.schema_version,
        "outcome": report.outcome.value,
        "runner_version": report.runner_version,
        "configuration": configuration,
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


def _safe_configuration_value(key: str, value: str) -> str:
    bounded = _safe_fact_text(value, 200)
    if not bounded:
        return ""
    patterns = {
        "repository": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
        "base_branch": r"[A-Za-z0-9][A-Za-z0-9._/-]*",
        "github_default_branch": r"[A-Za-z0-9][A-Za-z0-9._/-]*",
        "runtime": r"(?:stub|claude|codex)",
        "sandbox": r"(?:host|docker)",
        "assignee": r"(?:@me|[A-Za-z0-9-]+)",
        "agent_image": r"[A-Za-z0-9][A-Za-z0-9._/@:-]*",
    }
    pattern = patterns.get(key)
    return bounded if pattern is not None and re.fullmatch(pattern, bounded) else ""


def render_json(report: ReadinessReport) -> str:
    return json.dumps(
        report_document(report), separators=(",", ":"), ensure_ascii=False
    )


def render_human(report: ReadinessReport) -> str:
    repository = _safe_configuration_value(
        "repository", report.configuration.repository
    )
    base_branch = _safe_configuration_value(
        "base_branch", report.configuration.base_branch
    )
    github_default = (
        _safe_configuration_value(
            "github_default_branch", report.configuration.github_default_branch
        )
        if report.configuration.github_default_branch
        else ""
    )
    lines = [
        f"PyCastle Doctor: {report.outcome.value.replace('_', ' ')}",
        f"Repository: {repository}",
        f"Base branch: {base_branch} "
        f"(GitHub default: {github_default or 'unknown'})",
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
        include_item_content: bool = False,
        stream_image_build: bool = False,
        cleanup_reporter: Callable[[str], None] | None = None,
    ) -> None:
        self.fixture_dir = fixture_dir
        self.workspace = workspace
        self.runner = runner
        self.exists = exists
        self.include_item_content = include_item_content
        self.stream_image_build = stream_image_build
        self.cleanup_reporter = cleanup_reporter
        self._docker_workspace: Path | None = None
        self._base_commit: str | None = None
        self._project_fixture: FrozenProjectFixture | None = None

    def __enter__(self) -> DefaultReadinessAdapter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Remove Doctor's disposable Docker workspace, if one was created."""
        if self._docker_workspace is not None:
            try:
                shutil.rmtree(self._docker_workspace)
            except OSError:
                if self.cleanup_reporter is not None:
                    self.cleanup_reporter("Doctor cleanup could not complete.")
            finally:
                self._docker_workspace = None

    def _readiness_workspace(self) -> Path:
        if self._docker_workspace is None:
            self._docker_workspace = Path(
                tempfile.mkdtemp(prefix="pycastle-doctor-")
            ).resolve()
            # The image-declared user may have a uid different from the host
            # caller that owns this directory. This path holds no repository or
            # credential data, so allow that user to traverse and write the
            # disposable bind mount.
            self._docker_workspace.chmod(0o777)
        return self._docker_workspace

    def _run(self, argv: list[str], *, timeout: float = SHORT_TIMEOUT) -> Any:
        return self.runner(argv, cwd=self.workspace, capture=True, timeout=timeout)

    @staticmethod
    def _ok(result: Any) -> bool:
        return getattr(result, "returncode", 1) == 0

    def _runtime_command(
        self, config: ReadinessConfiguration, inner_argv: list[str]
    ) -> list[str]:
        if config.sandbox == "host":
            return inner_argv
        if not config.agent_image:
            raise AgentImagePreparationError("Agent image has not been pinned")
        return sandbox.build_run_command(
            config.runtime,
            inner_argv=inner_argv,
            workspace=self._readiness_workspace(),
            image=config.agent_image,
        )

    @staticmethod
    def _safe_version(result: Any) -> str | None:
        if not DefaultReadinessAdapter._ok(result):
            return None
        value = getattr(result, "stdout", "")
        if not isinstance(value, str):
            return None
        value = value.strip()
        allowed = frozenset(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._()+/-"
        )
        if not value or len(value) > 100 or any(char not in allowed for char in value):
            return None
        return value

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
        facts = {
            "selected": config.base_branch,
            "github_default": config.github_default_branch,
        }
        if not config.github_default_branch:
            return CheckResult(
                Status.FAIL,
                "GitHub default base branch could not be resolved.",
                facts,
                "Verify GitHub repository access and retry.",
            )
        result = self._run(
            [
                "git",
                "ls-remote",
                "--exit-code",
                "origin",
                f"refs/heads/{config.base_branch}",
            ]
        )
        lines = (getattr(result, "stdout", "") or "").splitlines()
        expected_ref = f"refs/heads/{config.base_branch}"
        parsed = [line.split() for line in lines]
        exact = [
            parts for parts in parsed if len(parts) == 2 and parts[1] == expected_ref
        ]
        commit = exact[0][0] if len(exact) == 1 else ""
        valid_commit = bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", commit))
        if self._ok(result) and valid_commit:
            self._base_commit = commit.lower()
        return (
            CheckResult(
                Status.PASS,
                "Selected remote base resolved to one exact commit.",
                {**facts, "commit": self._base_commit},
            )
            if self._base_commit
            else CheckResult(
                Status.FAIL,
                "Selected base branch did not resolve to one exact remote commit.",
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
            self._project_fixture = _freeze_project_fixture(
                self.fixture_dir, definition
            )
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
        try:
            prepared = prepare_agent_image(
                self.fixture_dir,
                runner=self.runner,
                cwd=self.workspace,
                capture_build=not self.stream_image_build,
                inspect_timeout=SHORT_TIMEOUT,
                build_timeout=IMAGE_BUILD_TIMEOUT,
            )
            object.__setattr__(config, "agent_image", prepared)
        except (AgentImagePreparationError, OSError, subprocess.TimeoutExpired):
            return CheckResult(
                Status.FAIL,
                "Agent image could not be prepared.",
                remediation="Fix .pycastle/Dockerfile and retry.",
            )
        runtime_config = sandbox.RUNTIME_CONFIG.get(config.runtime)
        if runtime_config is None:
            return CheckResult(
                Status.FAIL,
                "The selected Runtime has no Docker Sandbox convention.",
                {"image": config.agent_image},
                "Select Claude or Codex for the Docker Sandbox.",
            )
        auth_sentinel = f".pycastle-doctor-{uuid.uuid4().hex}"
        workspace_sentinel = f".pycastle-doctor-{uuid.uuid4().hex}"
        script = r"""set -eu
cleanup() { rm -f "${auth_file:-}" "${workspace_file:-}"; }
trap cleanup EXIT HUP INT TERM
test "$(id -u)" != 0
test -n "${HOME:-}"
test -w "$HOME"
command -v "$runtime" >/dev/null
eval "config_value=\${$config_env-}"
test "$config_value" = "$config_dir"
auth_file="$config_dir/$auth_name"
workspace_file="$PWD/$workspace_name"
: >"$auth_file"
: >"$workspace_file"
test -w "$auth_file"
test -w "$workspace_file"
"""
        inner = [
            "sh",
            "-c",
            script,
            "pycastle-image-contract",
            # Positional values are assigned by a small prefix so no user value
            # is interpolated into shell syntax.
        ]
        assignments = (
            f"runtime={config.runtime!s}; config_env={runtime_config.config_env!s}; "
            f"config_dir={runtime_config.config_dir!s}; auth_name={auth_sentinel!s}; "
            f"workspace_name={workspace_sentinel!s}; "
        )
        inner[2] = assignments + inner[2]
        argv = sandbox.build_run_command(
            config.runtime,
            inner_argv=inner,
            workspace=self._readiness_workspace(),
            image=prepared,
        )
        result = self._run(argv)
        return (
            CheckResult(
                Status.PASS,
                "Agent image satisfies the runtime contract.",
                {"image": prepared},
            )
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Agent image does not satisfy the runtime contract.",
                {"image": prepared},
                "Fix the image user, home, Runtime PATH, Auth volume, or workspace permissions.",
            )
        )

    def check_runtime(self, config: ReadinessConfiguration) -> CheckResult:
        if config.runtime == "stub":
            return CheckResult(Status.PASS, "Stub Runtime is available.")
        argv = self._runtime_command(config, [config.runtime, "--version"])
        try:
            result = self._run(argv)
        except subprocess.TimeoutExpired:
            return CheckResult(
                Status.FAIL,
                "Runtime launch timed out.",
                remediation="Verify the selected Runtime is responsive and retry Doctor.",
            )
        except OSError:
            return CheckResult(
                Status.FAIL,
                "Runtime is not launchable.",
                remediation="Install or repair the selected Runtime.",
            )
        if not self._ok(result):
            return CheckResult(
                Status.FAIL,
                "Runtime launch failed.",
                remediation="Install or repair the selected Runtime.",
            )
        version = self._safe_version(result)
        facts = {"version": version} if version else {}
        return CheckResult(Status.PASS, "Runtime is present and launchable.", facts)

    def check_runtime_authentication(
        self, config: ReadinessConfiguration
    ) -> CheckResult:
        if config.runtime == "stub":
            return CheckResult(
                Status.NOT_APPLICABLE, "Stub Runtime needs no authentication."
            )
        runtime_config = sandbox.RUNTIME_CONFIG.get(config.runtime)
        if runtime_config is None:
            return CheckResult(
                Status.FAIL,
                "The selected Runtime has no authentication convention.",
                remediation="Select Claude, Codex, or the Stub Runtime.",
            )
        argv = list(runtime_config.status_args)
        if config.sandbox == "docker":
            if not config.agent_image:
                return CheckResult(Status.BLOCKED, "Agent image is unavailable.")
            argv = sandbox.build_status_command(
                config.runtime, image=config.agent_image
            )
        try:
            result = self._run(argv)
        except (OSError, subprocess.TimeoutExpired):
            return CheckResult(
                Status.FAIL,
                "Runtime authentication status could not be checked.",
                remediation=f"Verify {config.runtime} and authenticate it in the selected Sandbox.",
            )
        return (
            CheckResult(Status.PASS, "Runtime native authentication status passed.")
            if self._ok(result)
            else CheckResult(
                Status.FAIL,
                "Runtime is not authenticated.",
                remediation=f"Authenticate {config.runtime} in the selected Sandbox.",
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
        identity = ""
        if self._ok(result):
            document = json.loads(getattr(result, "stdout", "") or "{}")
            if isinstance(document, dict):
                value = document.get("nameWithOwner")
                identity = value if isinstance(value, str) else ""
        matches = (
            bool(config.repository)
            and identity.casefold() == config.repository.casefold()
        )
        return (
            CheckResult(
                Status.PASS,
                "GitHub repository identity is reachable.",
                {"repository": config.repository},
            )
            if matches
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

    def eligible_items(
        self, config: ReadinessConfiguration
    ) -> list[EligibleItem | IssueRef]:
        if not config.assignee:
            raise ValueError("GitHub assignee could not be resolved")
        source = GitHubIssueSource(config.repository, runner=self.runner)
        # Doctor and Run freeze the same complete authoritative Item snapshot;
        # renderers project only number/title from these private copies.
        issues = source.list_ready(timeout=SHORT_TIMEOUT)
        selected = select_batch(
            issues,
            assignee=config.assignee,
            include_unassigned=config.include_unassigned,
            limit=config.item_limit,
        )
        return selected

    def dependencies(self) -> ReadinessDependencies:
        return ReadinessDependencies(
            self.probe, self.eligible_items, self.frozen_inputs
        )

    def frozen_inputs(
        self, config: ReadinessConfiguration, items: tuple[IssueRef, ...]
    ) -> FrozenReadinessInputs:
        if self._base_commit is None or self._project_fixture is None:
            raise ValueError("Readiness identities are incomplete")
        return FrozenReadinessInputs(
            self._base_commit,
            self._project_fixture,
            tuple(item.model_copy(deep=True) for item in items),
            config.sandbox,
            config.runtime,
            config.agent_image,
        )


def _freeze_project_fixture(
    fixture_dir: Path, definition: RunDefinition
) -> FrozenProjectFixture:
    paths = {fixture_dir / "main.py", fixture_dir / "setup", fixture_dir / "gate"}
    for marker in ("version", "sandbox", "Dockerfile"):
        candidate = fixture_dir / marker
        if candidate.is_file() and not candidate.is_symlink():
            paths.add(candidate)
    for graph in (definition.before, definition.item, definition.after):
        if graph is not None:
            paths.update(
                fixture_dir / "prompts" / node.prompt
                for node in graph.nodes.values()
                if isinstance(node, RuntimeNode)
            )
    frozen: list[FrozenFixtureFile] = []
    digest = hashlib.sha256()
    for path in sorted(
        paths, key=lambda value: value.relative_to(fixture_dir).as_posix()
    ):
        relative = path.relative_to(fixture_dir).as_posix()
        content = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        digest.update(relative.encode())
        digest.update(mode.to_bytes(2, "big"))
        digest.update(content)
        frozen.append(FrozenFixtureFile(relative, content, mode))
    return FrozenProjectFixture(
        digest.hexdigest(), copy.deepcopy(definition), tuple(frozen)
    )


def _validate_run_definition(definition: RunDefinition, fixture_dir: Path) -> None:
    prompt_root = fixture_dir / "prompts"
    if prompt_root.is_symlink() or not prompt_root.is_dir():
        raise ValueError("Invalid prompts directory")
    prompts = prompt_root.resolve()
    for scope, graph in (
        ("before", definition.before),
        ("item", definition.item),
        ("after", definition.after),
    ):
        if graph is None:
            continue
        if not isinstance(graph, ExecutionGraph) or graph.start not in graph.nodes:
            raise ValueError(f"Invalid {scope} graph")
        for node in graph.nodes.values():
            if not isinstance(node, RuntimeNode | GateNode):
                raise ValueError(f"Invalid node in {scope} graph")
            for target in (node.on_success, node.on_failure):
                if not isinstance(target, Terminal) and target not in graph.nodes:
                    raise ValueError(f"Invalid edge in {scope} graph")
            if isinstance(node, GateNode):
                continue
            candidate = prompts / node.prompt
            path = candidate.resolve()
            relative_parts = candidate.relative_to(prompts).parts
            has_symlink = any(
                (prompts.joinpath(*relative_parts[:index])).is_symlink()
                for index in range(1, len(relative_parts) + 1)
            )
            if prompts not in path.parents or not path.is_file() or has_symlink:
                raise ValueError(f"Invalid prompt path in {scope} graph")
    for name in ("setup", "gate"):
        path = fixture_dir / name
        if (
            not path.exists()
            or not path.is_file()
            or path.is_symlink()
            or not path.stat().st_mode & 0o111
        ):
            raise ValueError(f"Invalid {name} executable")
        try:
            first_line = path.read_bytes().splitlines()[0].decode("utf-8")
            words = shlex.split(first_line[2:]) if first_line.startswith("#!") else []
        except (IndexError, OSError, UnicodeDecodeError, ValueError):
            words = []
        if not words or not words[0].startswith("/") or len(words) > 2:
            raise ValueError(f"Invalid {name} shebang")
