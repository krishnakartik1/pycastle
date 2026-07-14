# Plan — Issue #67: agent image / harness sets no git author

## Problem

In a `--sandbox docker` run the runtime executes inside the agent image as the
non-root `node` user (`sandbox.build_run_command`). The `implement` and `review`
prompts both tell the runtime to **commit its work** (`implement.md` step 4,
`review.md` step 6). But nothing gives the container a git author identity:

- The **agent image** (`.pycastle/Dockerfile`) never runs `git config` for the
  `node` user.
- The **harness** (`sandbox.build_run_command`) passes `-e` for the runtime's
  config-dir env var only (`_config_env_args`), never any `GIT_*` identity.

So inside the container `git commit` has no `user.name`/`user.email`. Git either
aborts (`Author identity unknown … Please tell me who you are`) — the agent
cannot finish the commit step — or synthesizes a junk `node@<container-id>.(none)`
author that then rides into the merged branch and the run PR. Both are wrong.

The host sandbox path is unaffected in practice (it uses the host user's git
config) and is **out of scope** for this issue (see below).

## Fix (smallest change that satisfies the criteria)

Set a stable PyCastle bot git identity in the **harness**, not the Dockerfile, so
it is image-agnostic and also covers a bring-your-own `--image` (which the
Dockerfile approach would miss). This mirrors the existing `_config_env_args`
pattern of splicing `-e KEY=VALUE` into the `docker run` argv.

Git honours the `GIT_AUTHOR_*` / `GIT_COMMITTER_*` environment variables above
any config, so setting them makes commits deterministic regardless of what the
image does or doesn't configure. **All four must be set** — author *and*
committer — because git auto-detects (and can fail on) the committer identity
separately from the author.

### Files to edit

1. `src/pycastle/sandbox.py`
   - Add module constants for the identity, e.g.:
     ```python
     GIT_AUTHOR_NAME = "PyCastle"
     GIT_AUTHOR_EMAIL = "pycastle@users.noreply.github.com"
     ```
     (Committer name/email reuse the same two values — one bot identity.)
   - Add a small pure helper `_git_identity_args() -> list[str]` returning the
     four `-e GIT_AUTHOR_NAME=… -e GIT_AUTHOR_EMAIL=… -e GIT_COMMITTER_NAME=…
     -e GIT_COMMITTER_EMAIL=…` pairs.
   - Splice it into `build_run_command` **only** — right after
     `*_config_env_args(runtime_name)` and before `image`. Do **not** add it to
     `build_login_command` / `build_status_command`: those never commit, and
     keeping them lean preserves their exact-argv contract.
   - Update the `build_run_command` docstring to note it pins a bot git identity
     so in-container commits have a deterministic author.

2. `tests/test_sandbox.py` (test-first — write these first, watch them fail)
   - **New** `test_build_run_command_sets_git_author_and_committer_identity`:
     assert all four `GIT_AUTHOR_NAME/EMAIL`, `GIT_COMMITTER_NAME/EMAIL` env
     values are present as `-e NAME=VALUE` (author and committer both, using the
     module constants).
   - **New** codex variant (or parametrize) asserting the identity is
     runtime-agnostic — present for `"codex"` too.
   - **New** `test_login_and_status_carry_no_git_identity`: assert
     `GIT_AUTHOR_NAME` does **not** appear in `build_login_command` /
     `build_status_command` argv (they don't commit — lock the boundary).
   - **Update** the two full-argv equality tests to include the new `-e` entries
     in the expected list, placed right after the config-dir `-e` and before the
     image:
     - `test_build_run_command_wraps_inner_argv` (claude)
     - `test_build_run_command_codex_pins_codex_home_and_volume` (codex)

### Build order

1. Add the new/updated assertions in `tests/test_sandbox.py` (red).
2. Add the constants + `_git_identity_args()` + splice into `build_run_command`
   in `sandbox.py` (green).
3. Run the gate (`ruff`, `black`, `pytest`).

## Edge cases for the review phase to probe

- **Committer as well as author** is set — a fix that only sets `GIT_AUTHOR_*`
  still lets git fail auto-detecting the committer.
- **Runtime-agnostic**: identity present for both `claude` and `codex`.
- **Bring-your-own `--image`**: because the fix is in the harness, a custom
  `--image` (which never ran the scaffolded Dockerfile) still gets an identity.
- **Gate wrapper inherits it harmlessly**: `make_fixture_gate_check(sandbox=
  "docker")` reuses `build_run_command`, so the gate container also carries the
  identity — fine (the gate doesn't commit; no special-casing needed).
- **Login/status stay lean**: no git identity leaks into the auth-only argvs.
- **No credential leak**: the new `-e` values are a name/email, so
  `test_build_run_command_does_not_leak_credentials` still holds.
- **Deterministic values** (module constants) keep the argv tests plain equality.

## Out of scope (call-outs, not to be done here)

- The **host** sandbox commit path (orchestrator's own `git commit` on the host
  and host-run phases) relies on the host user's git config; hardening that for a
  config-less host is a separate concern.
- Touching the **Dockerfile** to add `git config` — deliberately not done; the
  harness env-var approach supersedes it and covers custom images. (The Dockerfile
  is a project-owned, byte-exempt fixture anyway.)
- Making the identity **configurable** (CLI flag / per-project override) — not
  asked for; a single stable bot identity satisfies the issue.
</content>
</invoke>
