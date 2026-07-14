# Plan

You are planning how to work a single GitHub issue. Do not write the
implementation yet -- work out the approach so the implement phase can move fast.

1. Read the issue's "What to build" and "Acceptance criteria". If it references
   a parent PRD, read that too.
2. Read the existing code the change touches so you understand the current
   state before proposing anything new. Note the files and public APIs in play.
3. Read `CONTEXT.md` for the project's domain language and use those exact
   terms -- never invent synonyms. Read any relevant `docs/adr/` decisions.
4. Sketch the smallest change that satisfies every acceptance criterion: which
   files to add or edit, the test-first order to build them in, and the edge
   cases the review phase should later probe.
5. Call out anything that looks out of scope for this one issue, and stop there.

Write the plan to `.pycastle/plan.md` -- an ignored scratch path the implement
phase can pick up but that is never committed. Stay within the scope of this one
issue. Do not modify unrelated code, and do not commit in this phase.
