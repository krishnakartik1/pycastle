from __future__ import annotations

import json

from pycastle.readiness import (
    CHECK_IDS,
    CheckResult,
    EligibleItem,
    ReadinessConfiguration,
    ReadinessDependencies,
    Status,
    evaluate_readiness,
    render_json,
)


def configuration() -> ReadinessConfiguration:
    return ReadinessConfiguration(
        repository="owner/repo",
        base_branch="main",
        github_default_branch="main",
        runtime="stub",
        sandbox="host",
        agent_image=None,
        assignee="octocat",
        include_unassigned=False,
        item_limit=2,
    )


def test_report_has_stable_order_schema_and_number_title_only_items() -> None:
    calls: list[str] = []

    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        calls.append(check_id)
        if check_id == "runtime_authentication":
            return CheckResult(Status.NOT_APPLICABLE, "Stub needs no authentication")
        return CheckResult(Status.PASS, "Ready", {"safe": "fact"})

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=probe,
            eligible_items=lambda _configuration: [
                EligibleItem(7, "Seven"),
                EligibleItem(2, "Two"),
            ],
        ),
    )

    assert calls == list(CHECK_IDS[:-1])
    assert [check.id for check in report.checks] == list(CHECK_IDS)
    assert report.ready is True
    document = json.loads(render_json(report))
    assert list(document) == [
        "schema_version",
        "ready",
        "runner_version",
        "configuration",
        "checks",
        "eligible_items",
    ]
    assert document["schema_version"] == 1
    assert document["eligible_items"] == [
        {"number": 2, "title": "Two"},
        {"number": 7, "title": "Seven"},
    ]


def test_failed_prerequisite_blocks_dependents_and_independent_checks_continue() -> (
    None
):
    called: list[str] = []

    def probe(check_id: str, _configuration: ReadinessConfiguration) -> CheckResult:
        called.append(check_id)
        if check_id == "fixture_compatibility":
            return CheckResult(
                Status.FAIL, "Fixture is incompatible", remediation="upgrade"
            )
        return CheckResult(Status.PASS, "Ready")

    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(probe=probe, eligible_items=lambda _configuration: []),
    )
    by_id = {check.id: check for check in report.checks}

    assert by_id["fixture_structure"].status is Status.BLOCKED
    assert by_id["sandbox"].status is Status.BLOCKED
    assert by_id["github_authentication"].status is Status.PASS
    assert "github_authentication" in called
    assert report.ready is False


def test_zero_items_is_a_failure_with_actionable_remediation() -> None:
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(Status.PASS, "Ready"),
            eligible_items=lambda _configuration: [],
        ),
    )
    check = report.checks[-1]
    assert check.id == "eligible_items"
    assert check.status is Status.FAIL
    assert check.remediation
    assert report.ready is False


def test_child_diagnostics_are_not_retained_in_report() -> None:
    secret = "ghp_super_secret raw gate output"
    report = evaluate_readiness(
        configuration(),
        ReadinessDependencies(
            probe=lambda _id, _configuration: CheckResult(
                Status.FAIL, "Probe failed", unsafe_detail=secret
            ),
            eligible_items=lambda _configuration: [],
        ),
    )
    assert secret not in render_json(report)
