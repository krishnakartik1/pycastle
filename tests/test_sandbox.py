"""Pure Docker argv builders for the agent sandbox.

Every test here asserts the exact ``docker`` argv a builder produces. No real
Docker runs: the builders are pure functions of their inputs, so the tests are
plain equality checks. These lock in the encoded decisions from ADR-0002
(subscription-backed auth volume) and ADR-0003 (Docker is the isolation
boundary): non-root ``node``, a ``node:22``-based image, one auth volume per
Runtime, and ``CLAUDE_CONFIG_DIR`` pinning runtime state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pycastle import sandbox


def test_default_image_is_node_22_based() -> None:
    # The agent image is based on node:22 so the bundled claude CLI runs. The
    # tag names that base ("node22") so the lineage is legible from the argv.
    assert "node22" in sandbox.DEFAULT_IMAGE
    assert sandbox.DEFAULT_IMAGE == "pycastle/agent:node22"


def test_auth_volume_is_stable_per_runtime() -> None:
    # One volume per runtime, shared across projects: the name depends only on
    # the runtime, never on a repo or workspace path.
    assert sandbox.auth_volume("claude") == "pycastle-claude-auth"
    assert sandbox.auth_volume("codex") == "pycastle-codex-auth"
    assert sandbox.auth_volume("claude") == sandbox.auth_volume("claude")


def test_build_run_command_wraps_inner_argv() -> None:
    workspace = Path("/home/krishna/work/repo")
    inner = ["claude", "-p", "do the work", "--output-format", "stream-json"]

    argv = sandbox.build_run_command("claude", inner_argv=inner, workspace=workspace)

    assert argv == [
        "docker",
        "run",
        "--rm",
        "-u",
        "node",
        "-w",
        "/home/krishna/work/repo",
        "-v",
        "pycastle-claude-auth:/home/node/.claude",
        "-v",
        "/home/krishna/work/repo:/home/krishna/work/repo",
        "-e",
        "CLAUDE_CONFIG_DIR=/home/node/.claude",
        sandbox.DEFAULT_IMAGE,
        "claude",
        "-p",
        "do the work",
        "--output-format",
        "stream-json",
    ]


def test_build_run_command_resolves_relative_workspace_to_absolute() -> None:
    # Docker rejects a relative bind-mount source, so a relative workspace must
    # be resolved to an absolute path before it reaches the argv. Otherwise the
    # mount source and -w would be relative and the container would fail to start.
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("work/repo")
    )
    workdir = argv[argv.index("-w") + 1]
    assert Path(workdir).is_absolute()
    # The bind-mount source and target stay symmetric on the resolved path.
    assert f"{workdir}:{workdir}" in argv
    assert workdir.endswith("/work/repo")


def test_build_run_command_keeps_absolute_workspace_unchanged() -> None:
    # An already-absolute workspace is passed through verbatim (resolve is a
    # no-op for it), so the exact mount path the caller chose is preserved.
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("/home/krishna/proj")
    )
    assert argv[argv.index("-w") + 1] == "/home/krishna/proj"
    assert "/home/krishna/proj:/home/krishna/proj" in argv


def test_build_run_command_preserves_spaces_as_single_argv_elements() -> None:
    # A workspace path with spaces stays one argv element on both the mount and
    # -w (no shell-joining), so the container sees the real directory name.
    workspace = Path("/home/krishna/my repo")
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=workspace
    )
    assert argv[argv.index("-w") + 1] == "/home/krishna/my repo"
    assert "/home/krishna/my repo:/home/krishna/my repo" in argv


def test_build_run_command_passes_inner_argv_as_distinct_elements() -> None:
    # The inner claude argv is spliced in element-by-element, never shell-joined
    # into one string, so a prompt with spaces survives as a single argument.
    inner = ["claude", "-p", "fix the bug now", "--output-format", "stream-json"]
    argv = sandbox.build_run_command("claude", inner_argv=inner, workspace=Path("/w"))
    image_idx = argv.index(sandbox.DEFAULT_IMAGE)
    assert argv[image_idx + 1 :] == inner
    # The multi-word prompt is one element, not split across the argv.
    assert "fix the bug now" in argv


def test_build_run_command_runs_non_root_node() -> None:
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("/w")
    )
    assert "-u" in argv
    assert argv[argv.index("-u") + 1] == "node"


def test_build_run_command_pins_config_dir_under_node_home() -> None:
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("/w")
    )
    assert "-e" in argv
    assert "CLAUDE_CONFIG_DIR=/home/node/.claude" in argv


def test_build_run_command_mounts_auth_volume_at_node_home() -> None:
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("/w")
    )
    # The per-runtime auth volume is mounted under node's home, where the claude
    # CLI looks for its credentials.
    assert "pycastle-claude-auth:/home/node/.claude" in argv


def test_build_run_command_bind_mounts_workspace_for_readwrite() -> None:
    workspace = Path("/home/krishna/proj")
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=workspace
    )
    # The workspace is bind-mounted at the same path so the agent reads and
    # writes the real tree, and the working dir is set to it.
    assert f"{workspace}:{workspace}" in argv
    assert argv[argv.index("-w") + 1] == str(workspace)


def test_build_run_command_uses_rm() -> None:
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("/w")
    )
    assert argv[:3] == ["docker", "run", "--rm"]


def test_build_run_command_accepts_custom_image() -> None:
    argv = sandbox.build_run_command(
        "claude",
        inner_argv=["claude"],
        workspace=Path("/w"),
        image="my/agent:dev",
    )
    # The image sits just before the inner argv.
    assert argv[argv.index("my/agent:dev") + 1] == "claude"


def test_build_run_command_does_not_leak_credentials() -> None:
    argv = sandbox.build_run_command(
        "claude", inner_argv=["claude"], workspace=Path("/w")
    )
    joined = " ".join(argv)
    # Never cat/echo or otherwise read credential file contents.
    for forbidden in ("cat", "echo", ".credentials.json", "/home/node/.claude/"):
        assert forbidden not in joined


def test_build_login_command_is_interactive_into_volume() -> None:
    argv = sandbox.build_login_command("claude")

    assert argv == [
        "docker",
        "run",
        "--rm",
        "-it",
        "-u",
        "node",
        "-v",
        "pycastle-claude-auth:/home/node/.claude",
        "-e",
        "CLAUDE_CONFIG_DIR=/home/node/.claude",
        sandbox.DEFAULT_IMAGE,
        "claude",
        "auth",
        "login",
        "--claudeai",
    ]


def test_build_login_command_accepts_custom_image() -> None:
    argv = sandbox.build_login_command("claude", image="my/agent:dev")
    assert argv[argv.index("my/agent:dev") + 1 :] == [
        "claude",
        "auth",
        "login",
        "--claudeai",
    ]


def test_build_status_command_checks_from_fresh_container() -> None:
    argv = sandbox.build_status_command("claude")

    assert argv == [
        "docker",
        "run",
        "--rm",
        "-u",
        "node",
        "-v",
        "pycastle-claude-auth:/home/node/.claude",
        "-e",
        "CLAUDE_CONFIG_DIR=/home/node/.claude",
        sandbox.DEFAULT_IMAGE,
        "claude",
        "auth",
        "status",
    ]


def test_status_command_does_not_print_credentials() -> None:
    # The status check proves auth works by making the agent answer, never by
    # reading or printing the credential file. This test fails the moment anyone
    # appends a `cat .../.credentials.json` (or any read of the volume) to the
    # status argv, since those strings would appear in the joined command.
    argv = sandbox.build_status_command("claude")
    joined = " ".join(argv)
    for forbidden in (
        "cat",
        "echo",
        "cp",
        ".credentials.json",
        "/home/node/.claude/",
    ):
        assert forbidden not in joined


def test_status_command_proves_auth_with_the_real_status_subcommand() -> None:
    # Positive lock on the mechanism: auth is confirmed by the runtime's own
    # `auth status` subcommand, which the real claude CLI provides. No file is
    # read; the only thing after the image is the claude status invocation.
    argv = sandbox.build_status_command("claude")
    image_idx = argv.index(sandbox.DEFAULT_IMAGE)
    assert argv[image_idx + 1 :] == ["claude", "auth", "status"]


def test_login_and_status_share_one_volume_across_projects() -> None:
    # Login and status both target the same per-runtime volume; nothing here is
    # keyed on a project path, so a single login serves every repo.
    login = sandbox.build_login_command("claude")
    status = sandbox.build_status_command("claude")
    vol = "pycastle-claude-auth:/home/node/.claude"
    assert vol in login
    assert vol in status


# --- Codex sandbox argv -----------------------------------------------------
#
# Codex gets its own config dir (CODEX_HOME), its own env var, its own auth
# volume, and a device-authorization login. The Claude argv above must stay
# byte-for-byte unchanged — these tests lock in the parametrization without
# regressing Claude.


def test_build_run_command_codex_pins_codex_home_and_volume() -> None:
    workspace = Path("/home/krishna/work/repo")
    inner = [
        "codex",
        "-C",
        "/home/krishna/work/repo",
        "--dangerously-bypass-approvals-and-sandbox",
        "exec",
        "--json",
        "do the work",
    ]

    argv = sandbox.build_run_command("codex", inner_argv=inner, workspace=workspace)

    assert argv == [
        "docker",
        "run",
        "--rm",
        "-u",
        "node",
        "-w",
        "/home/krishna/work/repo",
        "-v",
        "pycastle-codex-auth:/home/node/.codex",
        "-v",
        "/home/krishna/work/repo:/home/krishna/work/repo",
        "-e",
        "CODEX_HOME=/home/node/.codex",
        sandbox.DEFAULT_IMAGE,
        *inner,
    ]


def test_build_run_command_wrapper_is_runtime_agnostic_for_codex() -> None:
    # The wrapper adds no codex-specific flags: the bypass flag is owned by the
    # runtime's inner argv. The wrapper only mounts/pins and splices inner_argv.
    inner = ["codex", "exec", "--json", "hi"]
    argv = sandbox.build_run_command("codex", inner_argv=inner, workspace=Path("/w"))
    # No bypass flag anywhere the wrapper added (the inner argv here has none).
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    image_idx = argv.index(sandbox.DEFAULT_IMAGE)
    assert argv[image_idx + 1 :] == inner


def test_build_login_command_codex_uses_device_auth_no_tty() -> None:
    argv = sandbox.build_login_command("codex")

    assert argv == [
        "docker",
        "run",
        "--rm",
        "-u",
        "node",
        "-v",
        "pycastle-codex-auth:/home/node/.codex",
        "-e",
        "CODEX_HOME=/home/node/.codex",
        sandbox.DEFAULT_IMAGE,
        "codex",
        "login",
        "--device-auth",
    ]
    # The device-authorization flow needs no TTY: -it is never passed.
    assert "-it" not in argv


def test_build_status_command_codex_uses_login_status_subcommand() -> None:
    # Codex has no `auth` subcommand: its status check is `codex login status`
    # (verified against Codex 0.139.0), run against the Codex auth volume and
    # CODEX_HOME — never a hardcoded `claude` and never a bare `codex status`,
    # which is not a subcommand and would launch the interactive TUI instead.
    argv = sandbox.build_status_command("codex")

    assert argv == [
        "docker",
        "run",
        "--rm",
        "-u",
        "node",
        "-v",
        "pycastle-codex-auth:/home/node/.codex",
        "-e",
        "CODEX_HOME=/home/node/.codex",
        sandbox.DEFAULT_IMAGE,
        "codex",
        "login",
        "status",
    ]
    # No cross-runtime leak: the Codex status check never shells out to claude.
    assert "claude" not in argv


def test_build_login_command_claude_still_interactive_with_tty() -> None:
    # Parametrizing per runtime must not regress Claude: it keeps its TTY and
    # its browser-based auth login flow.
    argv = sandbox.build_login_command("claude")
    assert "-it" in argv
    assert argv[-4:] == ["claude", "auth", "login", "--claudeai"]


def test_codex_auth_volume_distinct_from_claude() -> None:
    assert sandbox.auth_volume("codex") == "pycastle-codex-auth"
    assert sandbox.auth_volume("codex") != sandbox.auth_volume("claude")


def test_codex_run_command_does_not_leak_credentials() -> None:
    argv = sandbox.build_run_command(
        "codex", inner_argv=["codex"], workspace=Path("/w")
    )
    joined = " ".join(argv)
    for forbidden in ("cat", "echo", "auth.json", "/home/node/.codex/"):
        assert forbidden not in joined


def test_unknown_runtime_sandbox_config_raises() -> None:
    with pytest.raises(ValueError):
        sandbox.build_run_command("nope", inner_argv=["x"], workspace=Path("/w"))


# --- Content-addressed agent-image tag (ADR-0005) ---------------------------
#
# The agent image is built on demand from the project's Dockerfile and cached by
# a content hash of the recipe text. These tests lock the tag shape and the
# hash's determinism without ever invoking docker (the helper is pure).


def test_image_tag_for_dockerfile_is_pycastle_agent_with_12_hex() -> None:
    # The tag is `pycastle/agent:<sha256(text)[:12]>`: the pycastle/agent repo
    # plus a 12-char lowercase-hex slice of the recipe's sha256.
    tag = sandbox.image_tag_for_dockerfile("FROM node:22-slim\n")
    repo, _, digest = tag.partition(":")
    assert repo == "pycastle/agent"
    assert len(digest) == 12
    assert all(c in "0123456789abcdef" for c in digest)


def test_image_tag_for_dockerfile_matches_known_sha256() -> None:
    # The hash is over the exact recipe bytes (utf-8 sha256), not a file's mtime
    # or size, so it is deterministic across runs and platforms.
    import hashlib

    text = "FROM node:22-slim\nUSER node\n"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    assert sandbox.image_tag_for_dockerfile(text) == f"pycastle/agent:{expected}"


def test_image_tag_for_dockerfile_is_stable_for_identical_text() -> None:
    # Identical recipe text -> identical tag, which is what lets an unchanged
    # Dockerfile skip the build entirely.
    text = "FROM node:22-slim\nRUN npm i -g x\n"
    assert sandbox.image_tag_for_dockerfile(text) == sandbox.image_tag_for_dockerfile(
        text
    )


def test_image_tag_for_dockerfile_changes_on_one_byte_edit() -> None:
    # A one-byte edit changes the hash, so an edited Dockerfile resolves to a new
    # tag and triggers exactly one rebuild.
    a = sandbox.image_tag_for_dockerfile("FROM node:22-slim\n")
    b = sandbox.image_tag_for_dockerfile("FROM node:22-slim \n")
    assert a != b
