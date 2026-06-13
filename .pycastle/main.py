"""PyCastle workflow for this repository.

Hand-written for the conservative default flow: ``plan`` → ``implement`` →
``review``. The plan phase works out an approach, implement does the work
test-first, and review tests edge cases and commits any improvements before the
issue branch is merged. Each phase is driven by its prompt file under
``prompts/``. Edit this file with normal Python to change the workflow — add
phases, reorder them, or (in a later slice) wire up success and failure
transitions.
"""

from pycastle import graph as g

graph = (
    g.build()
    .phase("plan", prompt="plan.md")
    .phase("implement", prompt="implement.md")
    .phase("review", prompt="review.md")
    .build()
)
