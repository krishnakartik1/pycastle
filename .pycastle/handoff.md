# Handoff — Issue #67 (agent image / harness sets no git author)

## What was attempted

The code fix for issue #67 is **done and committed** at HEAD
(`259e198 fix: pin bot git identity for in-container commits (#67)`). It splices
a stable PyCastle bot identity into `build_run_command` as four `-e GIT_*` env
vars (author + committer). See `git show 259e198` and `src/pycastle/sandbox.py`
(`_git_identity_args`, constants near line 87). The full rationale lives in the
plan at `.pycastle/plan.md` and the issue — not duplicated here.

`git diff HEAD` is **empty**: no further working-tree changes are pending.

## Current state — why the gate is red

One test fails: `tests/test_fixture_in_sync.py::test_committed_fixture_matches_scaffolder`

```
committed .pycastle/ shape has drifted from the scaffolder:
missing [], unexpected ['plan.md']
```

This is **not** a defect in the #67 fix. The guard walks the whole `.pycastle/`
fixture tree on disk (via `rglob`, `_tree()` at test line 57 — it uses the
filesystem, not git tracking) and compares it to a fresh scaffold. The
scaffolder writes `prompts/plan.md` but never a top-level `.pycastle/plan.md`.

The plan phase dropped its plan document at `.pycastle/plan.md` — inside the
committed fixture directory. That stray file is the entire "unexpected" drift.
It is untracked (`git status`), so `.gitignore` won't hide it from the test
(the test globs the filesystem, ignoring only `__pycache__`/`.pyc`).

## Files touched

- `src/pycastle/sandbox.py`, `tests/test_sandbox.py` — the #67 fix, already
  committed at HEAD.
- `.pycastle/plan.md` — untracked stray artifact; the sole cause of the red gate.

## What to try next

Remove the fixture-tree pollution so the shape guard passes; the #67 fix itself
needs no change.

1. Simplest: delete `.pycastle/plan.md` (relocate the plan somewhere outside the
   `.pycastle/` fixture dir if it must be kept), then re-run the gate.
2. Durable: have the plan phase write its plan to a path the fixture guard
   ignores — either outside `.pycastle/` entirely, or under an ignored subdir
   like `.pycastle/runs/` / `.pycastle/logs/` (already in `.pycastle/.gitignore`
   and, more importantly, not scaffolded). Do **not** rely on `.gitignore` alone:
   `_tree()` scans the filesystem, so an ignored-but-present file still fails the
   guard. The file must not exist under `.pycastle/` at gate time.

After removing it, run the gate — all other 350 tests pass and `black` is clean.
