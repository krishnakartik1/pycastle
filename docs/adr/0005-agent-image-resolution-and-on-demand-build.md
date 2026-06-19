# ADR-0005: Agent image resolution — `--image`, else build the project Dockerfile (content-addressed), else default tag

Status: Accepted (2026-06-18)

## Context

`pycastle run --sandbox docker` runs the agent inside a container, but today it only ever issues a `docker run <image>` (see `sandbox.build_run_command`) — it **never builds**. The image is `--image`'s value or `DEFAULT_IMAGE` (`pycastle/agent:node22`), and the scaffolded `.pycastle/Dockerfile` is connected to that tag only by a hand-run `docker build -t pycastle/agent:node22 .pycastle` and a matching default. A user editing the Dockerfile gets no rebuild; a user on a non-Node stack gets an image with no toolchain. We want the project's Dockerfile to be the source of truth for the agent image without forcing a manual build before every run, and without rebuilding when nothing changed.

## Decision

`pycastle run` resolves the agent image by this precedence, and builds on demand:

1. **`--image X` given** → run `X`. Never build, never read the Dockerfile. This is pure bring-your-own-image.
2. **No `--image`, `.pycastle/Dockerfile` exists** → derive a content-addressed tag `pycastle/agent:<sha256(Dockerfile)[:12]>`, `docker build -t <tag> .pycastle` **only if that tag is not already present** (`docker image inspect`), then run it. The Dockerfile takes precedence and is built on demand, cached by content.
3. **No `--image`, no Dockerfile** → fall back to `DEFAULT_IMAGE`, erroring with guidance if it is absent.

A `pycastle sandbox build` convenience builds the Dockerfile into its content-addressed tag explicitly (same path step 2 takes implicitly).

## Rationale

The tag is a hash of the Dockerfile, so an unchanged recipe means the tag already exists and the build is skipped — instant run. An edited Dockerfile changes the hash, so exactly one rebuild happens, then it is cached again. This is what makes "Dockerfile takes precedence, built on demand" not mean "rebuilt every run." `--image` staying authoritative keeps a genuine bring-your-own-image escape hatch (an existing CI image) that bypasses building entirely.

## Alternatives rejected

- **Auto-build every run, relying on Docker's layer cache.** An unchanged rebuild is fast but still invokes the daemon each run and is not instant; the hash-tag + `inspect`-skip is strictly better for the "no change → no build" goal.
- **No implicit build (status quo) — require a manual `docker build` first.** Loses the "edit the Dockerfile and just run" flow and silently runs a stale image when the built tag drifts from the default.
- **Agent installs its toolchain at runtime inside the container.** The container runs as non-root `node` (the Claude CLI refuses root), so it cannot `apt-get`; it needs network egress, reinstalls every run with no layer cache, is non-reproducible, and is an arbitrary-execution surface. It pays a one-time cost on every run.

## Consequences

- `run` gains an `--image` flag and an image-resolution step; `--sandbox docker` may now trigger a build, so `docker` is the only host prerequisite (unchanged) but a first run on a new Dockerfile pays one build.
- **The hash covers the recipe, not the upstream base.** `FROM node:22-slim` is a moving tag: identical Dockerfile text → identical hash → no rebuild, so an updated upstream base is *not* picked up. This is the intended default (reproducible relative to the recipe); catching base drift is a deliberate `--pull`/rebuild, out of scope here.
- Content-addressed images accumulate as the Dockerfile is edited; pruning old tags is left to `docker image prune` or a later `pycastle sandbox prune`.
- **Does not by itself enable arbitrary base images.** The sandbox still hardcodes the `node` user and `/home/node` (`sandbox.SANDBOX_USER`/`SANDBOX_HOME`), so a base without a `node` user must still be conformed by the Dockerfile (an adapter `FROM <base>` + agent CLI + `node` user). Parameterizing user/home for true bring-your-own-image is a separate decision.
