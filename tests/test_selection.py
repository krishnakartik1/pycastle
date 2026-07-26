"""Public Item-selection protocol and audit boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pycastle.models import IssueRef
from pycastle.selection import (
    ItemSelectionError,
    SelectionEnd,
    SelectionFailure,
    parse_response,
    render_prompt,
    write_audit_record,
)


def test_render_prompt_keeps_facts_policy_progress_and_contract_ordered() -> None:
    candidate = IssueRef(
        number=42,
        title="Foundation",
        body="Private frozen body.",
        labels=["ready-for-agent"],
        assignees=["agent"],
    )

    prompt = render_prompt(
        [candidate],
        [7],
        "Prefer foundations.",
        remaining_attempt_capacity=1,
        attempted=[7],
        stale=[9],
    )

    assert prompt.index("Private frozen body.") < prompt.index("Prefer foundations.")
    assert prompt.index("Prefer foundations.") < prompt.index("<selection>")
    assert '"completed": [\n    7\n  ]' in prompt
    assert '"stale": [\n    9\n  ]' in prompt


def test_parse_response_returns_typed_stable_lifecycle_values() -> None:
    decision, document = parse_response(
        '<selection>{"item": 42, "reason": "Foundation first."}</selection>',
        {42},
    )

    assert decision.item == 42
    assert document["reason"] == "Foundation first."
    assert SelectionEnd.POLICY_HALT == "project-policy-halted"

    with pytest.raises(ItemSelectionError) as exc_info:
        parse_response('<selection>{"item": 99, "reason": "No."}</selection>', {42})

    assert exc_info.value.code is SelectionFailure.ITEM_OUT_OF_POOL
    assert exc_info.value.code == "selection-item-out-of-pool"


def test_audit_record_retains_private_runtime_stderr(tmp_path: Path) -> None:
    write_audit_record(
        fixture_dir=tmp_path,
        run_id="audit",
        round_number=1,
        candidate_envelope="[]",
        prompt_name="select.md",
        directions="Choose.",
        runtime_transcript="private transcript",
        runtime_stderr="complete private stderr",
        runtime_telemetry=None,
        runtime_error="AgentCrashError: safe",
        parsed_response=None,
        validation_status="failed",
        validation_code=SelectionFailure.RUNTIME_FAILED,
    )

    record = json.loads((tmp_path / "runs/audit/selection-001.json").read_text())

    assert record["runtime_transcript"] == "private transcript"
    assert record["runtime_stderr"] == "complete private stderr"
    assert record["validation"]["code"] == "selection-runtime-failed"
