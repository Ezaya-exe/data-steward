"""Deterministic scoring tests (`ARCHITECTURE.md` §14, Phase 8a).

Every case is hand-built: the incident, root cause and resolution are literal
values, so a change in scoring semantics fails here instead of hiding inside a
benchmark run. Includes the three cases the phase acceptance names explicitly:
wrong category, right category but wrong column, and a false positive on a
clean control.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from dqs.bench.scenarios import Scenario, get_scenario
from dqs.bench.scoring import (
    Observation,
    ScenarioRun,
    aggregate,
    score_scenario,
    wilson_interval,
)
from dqs.models.enums import (
    IncidentStatus,
    ResolutionType,
    RootCauseCategory,
    Severity,
)
from dqs.models.incident import Incident
from dqs.models.root_cause import EvidenceItem, RootCause
from dqs.models.violation import Violation


def _incident(
    table: str | None, check: str, *, column: str | None = None, batch_id: str = "batch_20260131"
) -> Incident:
    now = dt.datetime(2026, 1, 31, tzinfo=dt.UTC)
    return Incident(
        incident_id=uuid.uuid4(),
        fingerprint=f"fp-{table}-{check}",
        batch_id=batch_id,
        table=table,
        status=IncidentStatus.OPEN,
        severity=Severity.BREAKING,
        violations=[
            Violation(
                check=check,
                severity=Severity.BREAKING,
                table=table,
                column=column,
                batch_id=batch_id,
                message=f"{check} failed",
            )
        ],
        created_at=now,
        updated_at=now,
    )


def _root_cause(
    category: RootCauseCategory,
    columns: list[str],
    *,
    resolution: ResolutionType = ResolutionType.PR,
    table: str = "orders",
) -> RootCause:
    return RootCause(
        category=category,
        summary="hand-built diagnosis",
        affected_table=table,
        affected_columns=columns,
        evidence=[EvidenceItem(tool="run_readonly_sql", finding="saw the drift")],
        confidence=0.9,
        suggested_resolution=resolution,
    )


def _observation(scenario: Scenario, **overrides: Any) -> Observation:
    expected = scenario.expected
    assert expected is not None
    defaults: dict[str, Any] = {
        "incidents": [
            _incident(
                f"raw.{scenario.table}",
                expected.expected_checks[0],
                column=expected.column,
            )
        ],
        "root_cause": _root_cause(
            expected.expected_category,
            list(expected.acceptable_columns) or ([expected.column] if expected.column else []),
            resolution=expected.expected_resolution,
            table=f"raw.{expected.table}",
        ),
        "resolution": expected.expected_resolution,
        "verification_passed": True,
        "semantic_passed": True,
        "fix_attempted": True,
        "fix_attempts": 1,
    }
    defaults.update(overrides)
    return Observation(**defaults)


def test_fully_correct_scenario_scores_true_everywhere() -> None:
    scenario = get_scenario("f4-orders-status-20")
    run = score_scenario(scenario, _observation(scenario))
    assert run.detected is True
    assert run.rc_category_correct is True
    assert run.rc_columns_correct is True
    assert run.resolution_correct is True
    assert run.expected_category == RootCauseCategory.UPSTREAM_NULLS_INTRODUCED.value
    assert run.expected_columns == ["status"]
    assert run.false_positive is None


def test_wrong_category_is_false_even_when_everything_else_matches() -> None:
    scenario = get_scenario("f4-orders-status-20")
    wrong = _root_cause(RootCauseCategory.UNKNOWN, ["status"], resolution=ResolutionType.ISSUE)
    run = score_scenario(scenario, _observation(scenario, root_cause=wrong))
    assert run.rc_category_correct is False
    assert run.rc_columns_correct is True
    assert run.actual_category == RootCauseCategory.UNKNOWN.value


def test_right_category_wrong_column_is_false() -> None:
    scenario = get_scenario("f4-orders-status-20")
    wrong = _root_cause(
        RootCauseCategory.UPSTREAM_NULLS_INTRODUCED,
        ["state"],
        resolution=ResolutionType.ISSUE,
    )
    run = score_scenario(scenario, _observation(scenario, root_cause=wrong))
    assert run.rc_category_correct is True
    assert run.rc_columns_correct is False
    assert run.actual_columns == ["state"]


def test_column_names_are_normalized_but_sets_must_match_exactly() -> None:
    scenario = get_scenario("f1-orders-status")
    matching = _root_cause(
        RootCauseCategory.SOURCE_COLUMN_RENAMED,
        ["raw.status", "staging.order_status"],
    )
    assert score_scenario(scenario, _observation(scenario, root_cause=matching)).rc_columns_correct

    subset = _root_cause(RootCauseCategory.SOURCE_COLUMN_RENAMED, ["status"])
    assert not score_scenario(
        scenario, _observation(scenario, root_cause=subset)
    ).rc_columns_correct


def test_detection_requires_the_expected_check_on_the_right_table() -> None:
    scenario = get_scenario("f4-orders-status-20")
    wrong_check = _incident("raw.orders", "types.column_type")
    run = score_scenario(scenario, _observation(scenario, incidents=[wrong_check]))
    assert run.detected is False

    wrong_table = _incident("raw.customers", "nulls.rate")
    assert (
        score_scenario(scenario, _observation(scenario, incidents=[wrong_table])).detected is False
    )


def test_batch_level_check_is_detected_without_a_table() -> None:
    """`freshness.sla` is emitted with `table=None`; it must still detect (A1)."""
    scenario = get_scenario("f8-customers-30")
    batch_incident = _incident(None, "freshness.sla")
    assert score_scenario(scenario, _observation(scenario, incidents=[batch_incident])).detected

    # The rule is "any incident carrying the batch-level check", not the table.
    other_table = _incident("raw.orders", "freshness.sla")
    assert score_scenario(scenario, _observation(scenario, incidents=[other_table])).detected

    # Still requires an expected check: a different batch-level incident is not it.
    wrong_check = _incident(None, "monitor.missing_batch")
    assert not score_scenario(scenario, _observation(scenario, incidents=[wrong_check])).detected


def test_table_level_detection_still_requires_the_right_table() -> None:
    scenario = get_scenario("f4-orders-status-20")
    wrong_table = _incident("raw.customers", "nulls.rate")
    run = score_scenario(scenario, _observation(scenario, incidents=[wrong_table]))
    assert run.detected is False

    right_table = _incident("raw.orders", "nulls.rate")
    assert score_scenario(scenario, _observation(scenario, incidents=[right_table])).detected


def test_false_positive_on_a_clean_control() -> None:
    control = get_scenario("clean-01")
    assert control.is_control
    clean = score_scenario(control, Observation())
    assert clean.false_positive is False
    assert clean.detected is None
    assert clean.rc_category_correct is None

    with_incident = score_scenario(
        control, Observation(incidents=[_incident("raw.orders", "nulls.rate")])
    )
    assert with_incident.false_positive is True


def test_verifier_pass_with_wrong_values_is_flagged() -> None:
    scenario = get_scenario("f1-orders-status")
    run = score_scenario(
        scenario,
        _observation(scenario, verification_passed=True, semantic_passed=False),
    )
    assert run.verifier_passed is True
    assert run.semantically_correct is False
    assert run.verified_but_wrong is True


def test_columns_are_not_applicable_for_table_scoped_faults() -> None:
    scenario = get_scenario("f6-orders-03")
    run = score_scenario(scenario, _observation(scenario))
    assert run.rc_columns_correct is None
    assert run.expected_columns == []


def test_resolution_mismatch_is_false() -> None:
    scenario = get_scenario("f4-orders-status-20")
    run = score_scenario(scenario, _observation(scenario, resolution=ResolutionType.PR))
    assert run.resolution_correct is False
    assert run.actual_resolution == ResolutionType.PR.value


def _run(
    scenario_id: str,
    *,
    detected: bool,
    category: bool,
    columns: bool | None,
    resolution: bool,
    verifier: bool | None = None,
    semantic: bool | None = None,
    error: str | None = None,
    tokens_in: int = 0,
    cost: float = 0.0,
    wall: float = 0.0,
    fix_attempted: bool | None = None,
) -> ScenarioRun:
    scenario = get_scenario(scenario_id)
    pr_expected = verifier is not None
    return ScenarioRun(
        scenario_id=scenario.id,
        split=scenario.split.value,
        kind=scenario.kind,
        clean=scenario.is_control,
        error=error,
        detected=None if scenario.is_control else detected,
        rc_category_correct=None if scenario.is_control else category,
        rc_columns_correct=None if scenario.is_control else columns,
        resolution_correct=None if scenario.is_control else resolution,
        verifier_passed=verifier,
        semantically_correct=semantic,
        verified_but_wrong=verifier is True and semantic is False,
        fix_attempted=fix_attempted,
        fix_delivered_valid=((verifier is True) if pr_expected else None),
        expected_resolution="PR" if pr_expected else "ISSUE",
        actual_resolution="PR" if pr_expected else "ISSUE",
        tokens_in=tokens_in,
        cost_usd=cost,
        wall_s=wall,
    )


def test_aggregates_use_documented_denominators_and_count_errors_as_misses() -> None:
    runs = [
        _run(
            "f1-orders-status",
            detected=True,
            category=True,
            columns=True,
            resolution=True,
            verifier=True,
            semantic=False,
            fix_attempted=True,
            tokens_in=100,
            cost=0.5,
            wall=10.0,
        ),
        _run(
            "f4-orders-status-20",
            detected=True,
            category=False,
            columns=False,
            resolution=True,
            error="LLMError: boom",
            cost=0.1,
            wall=2.0,
        ),
        ScenarioRun(
            scenario_id="clean-01",
            split="dev",
            kind="clean",
            clean=True,
            false_positive=False,
            tokens_in=20,
            cost_usd=0.0,
            wall_s=1.0,
        ),
        ScenarioRun(
            scenario_id="clean-02",
            split="dev",
            kind="clean",
            clean=True,
            false_positive=True,
        ),
    ]
    aggregates = aggregate(runs)
    overall = aggregates.overall
    assert (overall.scenarios, overall.faults, overall.controls) == (4, 2, 2)
    assert overall.detection_recall == 1.0  # 2 / 2 faults
    assert overall.rc_category_accuracy == 0.5  # 1 / 2
    assert overall.rc_columns_accuracy == 0.5  # 1 / 2 column-scoped
    assert overall.resolution_accuracy == 1.0
    assert overall.fix_delivered_valid_rate == 1.0  # 1 / 1 PR-expected
    assert overall.verifier_pass_given_attempt == 1.0  # 1 / 1 attempted
    assert overall.semantic_given_verified == 0.0  # 0 / 1 verified
    assert (overall.pr_expected, overall.fix_attempted, overall.verified) == (1, 1, 1)
    assert overall.verified_but_wrong == 1
    assert overall.false_positive_rate == 0.5  # 1 / 2 controls
    assert overall.errors == 1
    assert overall.total_cost_usd == 0.6
    assert overall.median_wall_s == 1.5
    assert overall.p90_wall_s == 10.0
    assert set(aggregates.by_kind) == {"clean", "column_rename", "null_spike"}
    assert aggregates.by_kind["column_rename"].scenarios == 1
    assert aggregates.by_kind["null_spike"].errors == 1


def test_aggregate_handles_an_empty_run_without_dividing_by_zero() -> None:
    aggregates = aggregate([])
    assert aggregates.overall.scenarios == 0
    assert aggregates.overall.detection_recall is None
    assert aggregates.overall.false_positive_rate is None
    assert aggregates.by_kind == {}


def test_wilson_interval_matches_hand_computed_values() -> None:
    low, high = wilson_interval(0, 10)
    assert low == pytest.approx(0.0, abs=1e-9)
    assert high == pytest.approx(0.2775, abs=1e-3)

    low, high = wilson_interval(10, 10)
    assert low == pytest.approx(0.7225, abs=1e-3)
    assert high == pytest.approx(1.0, abs=1e-9)

    low, high = wilson_interval(1, 2)
    assert low == pytest.approx(0.0945, abs=1e-3)
    assert high == pytest.approx(0.9055, abs=1e-3)


def test_wilson_interval_is_not_applicable_with_no_samples() -> None:
    assert wilson_interval(0, 0) is None
    assert wilson_interval(3, 0) is None


def test_infra_errors_are_excluded_from_every_denominator() -> None:
    good = _run(
        "f1-orders-status",
        detected=True,
        category=True,
        columns=True,
        resolution=True,
        verifier=True,
        semantic=True,
        tokens_in=100,
    )
    infra = ScenarioRun(
        scenario_id="f4-orders-status-20",
        split="dev",
        kind="column_rename",
        clean=False,
        infra_error="HTTP 429 rate limit",
        tokens_in=50,
    )
    control_infra = ScenarioRun(
        scenario_id="clean-01",
        split="dev",
        kind="clean",
        clean=True,
        infra_error="quota exceeded",
    )
    overall = aggregate([good, infra, control_infra]).overall
    assert (overall.scenarios, overall.faults, overall.controls) == (1, 1, 0)
    assert overall.infra_errors == 2
    assert overall.detection_success == 1
    assert overall.detection_recall == 1.0
    assert overall.errors == 0
    assert overall.false_positive_rate is None


# The six PR-expected scenarios that dev-run-1 forced to UNKNOWN/ISSUE before
# plan_fix. They had zero fix attempts, so they must not be counted as failed
# verifications (docs/review/dev-run-1-analysis.md).
_NOT_ATTEMPTED = [
    "f3-order-items-unit-price",
    "f6-orders-03",
    "f6-order-items-05",
    "f6-payments-15",
    "f6-customers-08",
    "f6-order-items-15",
]


def test_not_attempted_pr_scenarios_are_not_failed_verifications() -> None:
    runs: list[ScenarioRun] = []
    for scenario_id in _NOT_ATTEMPTED:
        scenario = get_scenario(scenario_id)
        run = score_scenario(
            scenario,
            _observation(
                scenario,
                resolution=ResolutionType.ISSUE,
                verification_passed=None,
                semantic_passed=None,
                fix_attempted=False,
                fix_attempts=0,
            ),
        )
        assert run.verifier_passed is None, "None must not be coerced to False"
        assert run.fix_attempted is False
        assert run.fix_delivered_valid is False
        assert run.verified_but_wrong is False
        runs.append(run)

    overall = aggregate(runs).overall
    assert overall.pr_expected == 6
    assert overall.fix_attempted == 0
    assert overall.verified == 0
    assert overall.fix_delivered_valid == 0
    assert overall.verifier_pass_given_attempt is None  # 0/0, not 0%
    assert overall.semantic_given_verified is None
    assert overall.verified_but_wrong == 0


def test_legacy_unknown_attempt_is_excluded_from_the_attempt_denominator() -> None:
    """Old records with `fix_attempted=None` are unknown, not failures."""
    scenario = get_scenario("f6-orders-03")
    run = ScenarioRun(
        scenario_id=scenario.id,
        split=scenario.split.value,
        kind=scenario.kind,
        clean=False,
        expected_resolution="PR",
        actual_resolution="ISSUE",
        verifier_passed=False,
        fix_attempted=None,
        fix_delivered_valid=False,
    )
    overall = aggregate([run]).overall
    assert overall.pr_expected == 1
    assert overall.fix_attempted == 0
    assert overall.verifier_pass_given_attempt is None
    assert overall.fix_delivered_valid_rate == 0.0


def test_metric_block_keeps_the_exact_numerators_for_confidence_intervals() -> None:
    runs = [
        _run(
            "f1-orders-status",
            detected=True,
            category=False,
            columns=True,
            resolution=False,
            verifier=False,
            semantic=None,
            fix_attempted=False,
        ),
        _run(
            "f4-orders-status-20",
            detected=False,
            category=True,
            columns=False,
            resolution=True,
            verifier=True,
            semantic=False,
            fix_attempted=True,
        ),
    ]
    overall = aggregate(runs).overall
    assert overall.detection_success == 1
    assert overall.category_success == 1
    assert overall.columns_success == 1
    assert overall.resolution_success == 1
    assert overall.fix_delivered_valid == 1
    assert overall.fix_attempted == 1
    assert overall.verified == 1
    assert overall.semantic_correct == 0
    assert overall.verified_but_wrong == 1
