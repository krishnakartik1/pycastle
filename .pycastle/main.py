"""PyCastle workflow for this repository.

Hand-written for the conservative default flow: ``plan`` -> ``implement`` ->
``review`` -> done. The plan phase works out an approach, implement does the work
test-first (retrying with a handoff while the quality gates stay red), and
review tests edge cases and commits any improvements before the issue branch is
merged. Each phase names its own success and failure destinations as explicit
rows; the executor walks those transitions rather than running a fixed list (see
ADR-0004).

The failure edges all route to ``HUMAN``: implement's bounded retry is kept
internal to the implement phase, so a phase that genuinely cannot pass hands the
issue to a person rather than looping. Edit this file with normal Python to
change the workflow -- add phases, repoint edges, or model handoff as its own
node.
"""

from pycastle.graph import DONE, HUMAN, build, build_run, phase

run = build_run(
    before=None,
    item=build(
        start="plan",
        phases=[
            phase("plan", "plan.md", on_success="implement", on_failure=HUMAN),
            phase("implement", "implement.md", on_success="review", on_failure=HUMAN),
            phase("review", "review.md", on_success=DONE, on_failure=HUMAN),
        ],
    ),
    after=build(
        start="run-review",
        phases=[
            phase("run-review", "run-review.md", on_success="run-repair"),
            phase("run-repair", "run-repair.md", on_success="run-report"),
            phase("run-report", "run-report.md"),
        ],
    ),
)
