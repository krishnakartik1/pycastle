# ADR-0006: One image-resolution path for setup/build/run — the project Dockerfile is the source of truth

Status: Accepted (2026-06-19)

## Context

ADR-0005 made `pycastle run` and `pycastle sandbox build` resolve the agent image from `.pycastle/Dockerfile` into a content-addressed tag (`pycastle/agent:<hash>`), built on demand. But `pycastle sandbox setup` (auth onboarding) was left pointing at the static `sandbox.DEFAULT_IMAGE` (`pycastle/agent:node22`), and nothing in the post-0005 flow ever builds that tag. On a clean host, `sandbox setup` therefore tries to `docker run pycastle/agent:node22`, fails to find it, attempts a registry pull, and errors — auth cannot be onboarded even though `run` would happily auto-build and work (#37).

The deeper issue is a split mental model: the run image is now a *project* concern (per-project Dockerfile, content-addressed), while auth had drifted onto a separate static tag. The desired user experience is that **the project `.pycastle/Dockerfile` is the single source of truth for everything PyCastle runs**, and a user never has to reason about image tags.

## Decision

`setup`, `build`, and `run` all resolve the agent image through **one** path:

1. `--image X` given → use `X` verbatim (bring-your-own-image escape hatch).
2. else `.pycastle/Dockerfile` exists → build it into its content-addressed tag, building only if absent.
3. else → error with guidance (`pycastle init` first).

`sandbox setup` builds/uses the *same* image as `run`, then runs the login against that tag. The per-runtime auth **volume** stays shared across projects (ADR-0002) and is independent of which image onboarded it — so auth is still onboarded once, and a later run against the project's image reuses those credentials.

## Rationale

The Dockerfile becomes the single source of truth for auth and run alike — one image concept, built on demand, never a tag the user must name. This matches the already-shipped `run`/`build` behaviour and removes the only command (`setup`) that diverged from it. `--image` remains a quiet escape hatch, never needed on the happy path.

## Alternatives rejected

- **Separate runtime-owned auth image** (a minimal CLI-only image, project-independent). Conceptually cleaner — auth *is* a per-runtime concern — but it introduces a **second** image concept the user must hold (an auth image *and* a run image), which is exactly the confusion we are removing. Its one advantage (onboarding auth with no project present) is not a real workflow: auth is shared across projects and onboarded once, from within a project.
- **Keep `node22` as a static default that actually gets built.** Collapses into this decision (if built from the project Dockerfile) or into the rejected separate-image option (if built from a pycastle-internal Dockerfile); not a distinct design, just a worse tag name than the content hash.

## Consequences

- Onboarding auth now requires an initialized project (`.pycastle/Dockerfile` present). Acceptable: auth is shared and onboarded once, always from within a project.
- `sandbox setup` gains an on-demand build step (cached after the first build, like `run`).
- Supersedes the assumption that auth runs against `DEFAULT_IMAGE`; `DEFAULT_IMAGE` becomes only the fallback when resolving with no Dockerfile and no `--image`.
- Resolves #37.
