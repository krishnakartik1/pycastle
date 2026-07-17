# ADR-0012: A project-owned Dockerfile defines the Docker Sandbox

Status: Accepted (2026-07-16)

ADR-0014 defines the canonical Setup process. This ADR owns the immutable Docker
bootstrap, persistence mounts, and host-versus-Docker provisioning boundary in
which Setup runs.

## Context

Docker execution currently has three possible image sources, a PyCastle-managed
content-addressed tag, an explicit build command, and a container contract fixed
to the `node` user and `/home/node`. The scaffold also detects a project language
and adds dependencies to the image. Those choices blur ownership: PyCastle must
understand project toolchains, while Setup also claims responsibility for making
the current worktree executable.

Host execution has the opposite problem: it uses whatever bootstrap tools the
machine already has. The two Sandboxes need the same preparation protocol, not
identical provisioning machinery. The project should own both its immutable
Docker substrate and its durable worktree preparation, while PyCastle should own
only a small language-agnostic invocation boundary.

## Decision

The canonical `.pycastle/Dockerfile` is the sole source for an **Agent image**.
Docker execution has no `--image` override and no PyCastle default-image
fallback. PyCastle builds the Dockerfile with the repository root as its build
context and delegates all layer caching to Docker; it does not hash recipes,
manage image caches, or expose a separate `pycastle sandbox build` lifecycle.

Before a Docker Run causes side effects, PyCastle builds the image and pins the
resulting immutable image identity. Build failure prevents the Run from
starting. Every Setup, Runtime-node, and Gate-node process in that Run uses the
same pinned identity; changes to the Dockerfile or build context take effect on
the next Run.

PyCastle never rebuilds or reloads fixture files in response to worktree changes
inside an active Run. A toolchain migration that cannot be prepared or verified
by the pinned image must therefore be staged: first land the Dockerfile, Setup,
or Gate change under the current contract (or complete it manually), then let a
later Run adopt the new frozen fixture and image before landing dependent
manifest changes.

`pycastle init` always creates the same language-neutral Dockerfile, regardless
of the initially selected Sandbox. The scaffold supplies the Runtime CLIs
PyCastle ships, Git, certificate roots, minimal process tools, a neutral
non-root default user, a writable home, and writable `/pycastle/auth`. It marks a
project-owned extension point for interpreters, compilers, package managers, and
OS libraries, but never inspects dependency manifests or adds language-specific
tooling. Once created, the Dockerfile is entirely project-owned.

The Agent image is immutable bootstrap capacity: it contains the selected
Runtime and the tools required to launch project execution. The mandatory Setup
derives worktree-specific dependencies and generated prerequisites from the
current checkout and stores them under the mounted target worktree. Setup cannot
depend on container-local changes, shell activation, exported variables, or a
long-lived container surviving into the following node.

Each Docker process runs in a fresh disposable container as the image's declared
default user; PyCastle does not force a username or home path and does not pass
a runtime `--user` override. On POSIX hosts, every canonical image build supplies
the non-secret decimal effective host identity as `PYCASTLE_HOST_UID` and
`PYCASTLE_HOST_GID` build arguments. The project-owned Dockerfile must consume
that interface and give its declared non-root user compatible numeric IDs,
including when the base image already occupies either ID. A UID-0 host process
is rejected because mapping the declared user to root would violate the Image
contract. Hosts without meaningful effective Unix IDs fail image preparation
with remediation rather than claiming readiness. PyCastle bind-mounts the
repository workspace read-write at the same absolute path and
sets the target Item or Run worktree as the process working directory. The only
other persistent mount is the selected Runtime's Auth volume at
`/pycastle/auth`; Codex and Claude use distinct volumes mounted at that same
neutral path. Project-configured mounts, persistent environment caches, and
extra volumes are not part of the contract.

Docker processes receive the image's environment plus only PyCastle protocol
and Runtime-integration values, including `PYCASTLE_SCOPE` where specified and
the selected Runtime's config path pointing at `/pycastle/auth`. Ambient host
environment variables are not forwarded. General project secret injection is a
separate concern.

The host Sandbox performs no provisioning. The host must already provide the
selected Runtime, its authentication, Git, and the interpreter named by Setup's
shebang. PyCastle invokes the same Setup protocol as Docker, but never installs
host tools, creates a language environment, or interprets a dependency manifest.
Host execution is intentionally less hermetic; Docker is the versioned and
isolated choice.

Runtime authentication is named **Runtime login**, not Sandbox setup. An
explicit Sandbox flag wins, otherwise Runtime login uses the Project fixture's
Sandbox selection. Host login invokes the installed Runtime's normal login flow;
Docker login builds the canonical Agent image and uses the Runtime-specific Auth
volume. Authentication remains explicit and never occurs automatically during
readiness or a Run. The old `pycastle sandbox setup` name has no compatibility
alias.

Release 0.1.2 introduces this Dockerfile interface as a manual compatibility
boundary for every Project fixture. PyCastle validates semantic `ARG`
declarations for both reserved names, but never rewrites the project-owned
Dockerfile. An older fixture remains migration-required; its first Upgrade makes
no writes and directs the owner to edit, review, and commit the Dockerfile. A
second Upgrade from the corrected clean checkout validates adoption and advances
the fixture marker. Doctor, not Upgrade, behaviorally proves that the resulting
image user can create and modify host-owned worktree state.

## Rationale

One version-controlled image source makes Docker ownership visible and removes
resolution precedence, hidden defaults, and PyCastle-managed caching. A neutral
container ABI preserves the Runtime authentication and writable-state guarantees
PyCastle needs without making every project look like a Node project. Separating
immutable bootstrap tools from Setup's durable worktree state gives host and
Docker one preparation model even though only Docker versions its substrate.

Fresh containers and two explicit persistent mounts make durability easy to
reason about: repository state belongs to the project, Auth-volume state belongs
to the Runtime, and everything else is disposable. Refusing ambient environment
forwarding prevents the Docker boundary from silently exposing host secrets.

## Consequences

- ADR-0005 and ADR-0006 are superseded. ADR-0007's rule that all project
  execution uses the selected Sandbox remains current, but its fixed image user
  and image-resident dependency assumptions do not.
- A Docker-capable project must maintain `.pycastle/Dockerfile`; an existing
  image can still be consumed explicitly with a `FROM` instruction.
- Switching Runtime may require editing the project Dockerfile if the selected
  Runtime CLI is absent.
- Project dependency environments must live in each target worktree if they
  need to survive between Docker processes.
- Fixture and toolchain migrations may require two changes because an active Run
  never rebuilds its pinned image or adopts its own fixture edits.
- ADR-0013 defines image-contract diagnostics and makes Doctor perform the same
  canonical build as Run when an eligible batch requires Docker execution.
