# ADR-0007: Project execution runs in the sandbox — the gate runs where the phases run

Status: Accepted (2026-06-20)

## Context

A `--sandbox docker` run wraps the **runtime** in `docker run` (the phases execute in the agent image), but the **gate** has always run on the host: `make_fixture_gate_check` invokes the gate as a plain host subprocess, regardless of sandbox. The toolchain requirement was therefore split — the runtime CLI lives in the image, the project's lint/test tools have to be present on the host — and the isolation a user chose Docker for was leaky: the host still needed the project toolchain (#28).

The default gate guards each step with `command -v` and skips a missing tool, exiting 0. So a gate whose tools are absent **passes silently**: it verifies nothing and reports success, and the orchestrator (which sees only the exit code) opens a PR claiming a green gate. This was observed live — a Python project run in a `node:22-slim` agent image produced a vacuous green gate while the run shipped unverified code (#19). The `--verbose` output trace (ADR-less, #52) made the silent skip visible for the first time.

Underneath both is an unstated principle that was never pinned down (#32): *where* does the work of a run actually run? The phases were in the sandbox; the gate was not; nothing said why.

## Decision

**All project execution in the loop runs inside the sandbox; PyCastle's own orchestration runs on the host.**

- *Project execution* — anything that runs the project's code or toolchain against an attempt — is the **agent phases and the gate**. It runs wherever the sandbox is: on the host for `--sandbox host`, inside the agent image for `--sandbox docker`. The gate runs in the **same resolved image** as the phases, wrapped through the same `build_run_command` path (workspace = repo root, workdir = the issue worktree), running the canonical fixture gate (not the worktree's copy, so an attempt cannot weaken its own gate).
- *Orchestration* — `git` (worktree/branch/commit/merge), `gh` (issue source, PR creation), and `docker build` (image resolution) — is PyCastle's machinery, not project execution, and stays on the host.

Two consequences are adopted with the decision:

1. **The image contract grows** to require the project's gate toolchain on PATH, not just the runtime CLI.
2. **A gate that runs zero checks fails** instead of passing. The scaffolded default gate exits non-zero when no configured check could run (rule: fail-if-zero, not fail-if-any-missing — a partially-equipped image still passes on the checks it can run). The orchestrator captures the gate's output and surfaces it (always on failure; under `--verbose` on success) into the per-issue transcript, so even a project-owned gate that pre-dates this change is auditable.

## Rationale

The toolchain lives in exactly one place — the image — instead of being split across host and image. The isolation Docker was chosen for becomes real: the host no longer needs the project's tools. And "the gate runs where the phases run" is a single rule with no special-casing, derived from one principle rather than an accident of which code path got wrapped.

Making a zero-check gate fail closes the gap between "passed" and "couldn't verify," which the host gate silently collapsed into success.

## Alternatives rejected

- **Keep the gate on the host.** Lean image (runtime CLI only), but it is the status quo that split the toolchain and leaked isolation — the host still needs the project tooling, defeating the point of the Docker sandbox.
- **A separate gate image** (toolchain only, distinct from the runtime image). Introduces a second image concept the user must maintain and keep in sync; the toolchain would still be duplicated against anything the phases need. One image for all project execution is simpler and is already what ADR-0006 established for the run/auth image.
- **Fail-if-any-declared-tool-is-missing** (stricter than fail-if-zero). More honest when the image is the single toolchain home, but it turns every partially-equipped image into a hard failure and offers no gentler path; fail-if-zero catches the vacuous case (the real defect) while still letting a run gate on the checks it *can* run.
- **Enforce non-vacuousness in the orchestrator** by parsing gate output. Couples PyCastle to the gate's wording and fights the project-owns-the-gate model; the scaffold-level fail-if-zero plus output surfacing achieves the same without parsing.

## Consequences

- The image contract now requires the gate toolchain; a bring-your-own `--image` must satisfy it too. CONTEXT.md's Image contract and worked Example are updated accordingly.
- A fresh Python scaffold's first `docker` gate **fails loud** (toolchain-less `node:22-slim` → zero checks → fail) until the Dockerfile carries the toolchain. Making that the out-of-the-box path is a separate concern (stack-aware scaffold, #19); this ADR accepts the interim loud failure as the honest default.
- The fail-if-zero hardening lives in the scaffolded gate, so it lands for new `init`s only; existing deployments keep their gate until they re-scaffold, with output surfacing as the backstop that makes a vacuous gate visible regardless.
- Implemented in two steps: the gate-in-container path plus fail-loud and output surfacing first (#28), then the stack-aware scaffold (#19).
- Resolves #28 and #32; informs #19.
