"""Pure builders for the Docker agent sandbox argv.

Docker is the isolation boundary (ADR-0003): both the Runtime and the commands
it invokes run inside the container, never on the host. Auth is a Docker volume
holding the agent's subscription login (ADR-0002) rather than an API key or a
host credential bind-mount.

Every function here is a *pure* function of its arguments: it returns the
``docker`` argv as a list of strings and runs nothing. Side effects (actually
invoking ``docker``) live in the CLI and the Runtime, which keeps these
builders trivial to unit-test by asserting the argv.

Encoded decisions, shared by every builder:

* Run as the non-root user ``node`` with home ``/home/node``. The ``claude``
  CLI refuses to run as root, and bind-mounted files written as ``node`` stay
  owned by a real user rather than root.
* The agent image is based on ``node:22`` (see :data:`DEFAULT_IMAGE`); the
  project Dockerfile that builds it is scaffolded by a later slice.
* One auth volume *per Runtime*, shared across every project (see
  :func:`auth_volume`). You log in once per agent, not once per repo.
* Runtime state is pinned with ``CLAUDE_CONFIG_DIR=/home/node/.claude``, the
  mount point of the auth volume, so the CLI reads and writes credentials
  there inside the container.

Credential file contents are never read, printed, or copied by any builder.
Auth is proved only by having the agent answer a prompt (see
:func:`build_status_command`), never by ``cat``-ing the volume.

Headless token fallback
------------------------
:func:`build_login_command` performs the *interactive* browser login, which
needs a TTY. On a headless host (CI, a server with no browser) run the login on
a machine that has a browser, then move the resulting credentials into the same
named volume the host uses, or pass a long-lived token into the container via
the ``CLAUDE_CODE_OAUTH_TOKEN`` environment variable instead of mounting the
volume. The token is read from the host environment at run time and is never
written to the argv these builders return.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

#: The default agent-runtime image, based on ``node:22`` so the bundled
#: ``claude`` CLI is available. The project Dockerfile that publishes this image
#: under this tag is scaffolded by a later slice.
DEFAULT_IMAGE = "pycastle/agent:node22"

#: The non-root user the agent runs as inside the container.
SANDBOX_USER = "node"

#: ``node``'s home inside the container; the auth volume mounts here.
SANDBOX_HOME = "/home/node"

#: Where the per-Runtime auth volume is mounted and where the CLI keeps state.
CLAUDE_CONFIG_DIR = "/home/node/.claude"


def auth_volume(runtime_name: str) -> str:
    """Return the stable, per-Runtime Docker auth volume name.

    The name depends only on ``runtime_name`` — never on a repo or workspace —
    so a single login is shared across every project a Runtime works.
    """
    return f"pycastle-{runtime_name}-auth"


def _auth_mount_args(runtime_name: str) -> list[str]:
    """Mount the per-Runtime auth volume at the CLI's config dir."""
    return ["-v", f"{auth_volume(runtime_name)}:{CLAUDE_CONFIG_DIR}"]


def _config_env_args() -> list[str]:
    """Pin ``CLAUDE_CONFIG_DIR`` to the auth volume's mount point."""
    return ["-e", f"CLAUDE_CONFIG_DIR={CLAUDE_CONFIG_DIR}"]


def build_run_command(
    runtime_name: str,
    *,
    inner_argv: Sequence[str],
    workspace: Path,
    image: str = DEFAULT_IMAGE,
) -> list[str]:
    """Wrap an inner agent argv into a ``docker run`` argv.

    ``inner_argv`` is the command the Runtime would otherwise run on the host
    (for Claude, the ``claude …`` argv from ``ClaudeRuntime.build_command``).
    The container runs as non-root ``node``, mounts the per-Runtime auth volume,
    bind-mounts ``workspace`` at the same path so the agent reads and writes the
    real tree, sets the working directory to it, and pins ``CLAUDE_CONFIG_DIR``.

    ``workspace`` is resolved to an absolute path first: Docker rejects a
    relative bind-mount source, so a relative ``Path`` would otherwise produce a
    silently broken ``docker run`` argv.
    """
    workspace_path = str(Path(workspace).resolve())
    return [
        "docker",
        "run",
        "--rm",
        "-u",
        SANDBOX_USER,
        "-w",
        workspace_path,
        *_auth_mount_args(runtime_name),
        "-v",
        f"{workspace_path}:{workspace_path}",
        *_config_env_args(),
        image,
        *inner_argv,
    ]


def build_login_command(
    runtime_name: str,
    *,
    image: str = DEFAULT_IMAGE,
) -> list[str]:
    """Build the interactive login argv that writes auth into the volume.

    Runs ``claude /login`` with a TTY (``-it``) so the browser-based login can
    complete, writing the subscription credentials into the per-Runtime auth
    volume. On a headless host use the token fallback documented in the module
    docstring instead. No workspace is mounted: login only touches auth state.
    """
    return [
        "docker",
        "run",
        "--rm",
        "-it",
        "-u",
        SANDBOX_USER,
        *_auth_mount_args(runtime_name),
        *_config_env_args(),
        image,
        "claude",
        "/login",
    ]


def build_status_command(
    runtime_name: str,
    *,
    image: str = DEFAULT_IMAGE,
) -> list[str]:
    """Build a fresh-container auth status-check argv.

    Spins up a clean container against the same auth volume and asks the agent
    to answer a one-word prompt. A zero exit proves the volume holds working
    credentials. Auth is never confirmed by reading or printing the credential
    file — only by the agent responding.
    """
    return [
        "docker",
        "run",
        "--rm",
        "-u",
        SANDBOX_USER,
        *_auth_mount_args(runtime_name),
        *_config_env_args(),
        image,
        "claude",
        "-p",
        "respond with the single word: ok",
        "--max-turns",
        "1",
    ]
