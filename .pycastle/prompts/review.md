# Review

You are reviewing and hardening the implementation of a single GitHub issue
before its branch is merged. This is a hardening pass, not an approve/reject
gate: fix what you find, do not hand work back.

1. Re-read the issue's "Acceptance criteria" and the diff produced so far.
2. Run the project's quality gates and fix anything they flag before going on.
3. Stress-test the edge cases the implement phase may have missed: empty inputs,
   zero, `None`, missing optional fields, boundary and off-by-one conditions,
   and invalid inputs that should raise specific errors. Write tests that probe
   these paths. If you can break the implementation, fix it.
4. Tidy code quality: unclear names, needless nesting, missing type hints or
   docstrings, redundant code, and any domain terms that drift from
   `CONTEXT.md`.
5. Run the quality gates once more and confirm they pass.
6. Commit your improvements with a conventional commit message that references
   the issue number. If you made no changes, there is nothing to commit.

Commit any review improvements in this phase so they are part of the issue
branch before it is merged. Stay within the scope of this one issue.
