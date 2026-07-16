"""Project-owned PyCastle Run definition.

Gate placement and recovery are ordinary graph topology. Every Gate node invokes
the same frozen `.pycastle/gate`; a Gate-node name is identity, not a hook name.
"""

from pycastle.graph import DONE, build_run, execution_graph, gate_node, runtime_node

run = build_run(
    item=execution_graph(
        start="plan",
        nodes=[
            runtime_node("plan", "plan.md", on_success="implement"),
            runtime_node("implement", "implement.md", on_success="review"),
            runtime_node("review", "review.md", on_success="verify"),
            gate_node("verify", on_success=DONE, on_failure="repair"),
            runtime_node("repair", "repair.md", on_success="verify"),
        ],
    ),
    after=execution_graph(
        start="run-review",
        nodes=[
            runtime_node("run-review", "run-review.md", on_success="run-report"),
            runtime_node("run-report", "run-report.md", on_success="run-verify"),
            gate_node("run-verify", on_success=DONE, on_failure="run-repair"),
            runtime_node("run-repair", "run-repair.md", on_success="run-report"),
        ],
    ),
)
