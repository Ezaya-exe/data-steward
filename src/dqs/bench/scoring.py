"""Deterministic benchmark scoring (Phase 8a, `ARCHITECTURE.md` §14).

There is no LLM judge here: every metric compares a typed artifact (the
incident, `RootCause`, final resolution, verifier verdict) with the scenario's
answer key. A scenario that errored is counted as a miss in the aggregate
denominators — dropping it would inflate the score.

Denominators are explicit (documented in `docs/phase-notes/phase-8a.md`):
detection/category/columns/resolution over fault scenarios; fix-validity and
semantic correctness over PR-expected scenarios; false-positive over controls.
"""

from __future__ import annotations

import math
import statistics
from typing import Final

from pydantic import BaseModel, ConfigDict

from dqs.bench.scenarios import Scenario
from dqs.models.enums import ResolutionType
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause

# RootCause category/column/report values are compared case-insensitively;
# a model naming `raw.orders.total_amount` or `total_amount` means the same.
_TABLE_PREFIXES: Final = ("raw.", "staging.", "marts.")


class Observation(BaseModel):
    """Everything the runner measured for one scenario before scoring.

    Kept separate from `ScenarioRun` so the scoring function is pure and a test
    can hand-build the exact incident/root-cause/resolution it wants.
    """

    model_config = ConfigDict(extra="forbid")

    incidents: list[Incident] = []
    root_cause: RootCause | None = None
    resolution: ResolutionType | None = None
    verification_passed: bool | None = None
    semantic_passed: bool | None = None
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0
    error: str | None = None
    # A rate-limit/quota failure is the provider's problem, not a wrong answer.
    # It is persisted for the resume path but excluded from every denominator.
    infra_error: str | None = None
    repeat: int = 0


class ScenarioRun(BaseModel):
    """One persisted per-scenario result (the JSON record in a results file)."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    split: str
    kind: str
    clean: bool
    repeat: int = 0
    error: str | None = None
    infra_error: str | None = None
    detected: bool | None = None
    rc_category_correct: bool | None = None
    rc_columns_correct: bool | None = None
    resolution_correct: bool | None = None
    verifier_passed: bool | None = None
    semantically_correct: bool | None = None
    false_positive: bool | None = None
    verified_but_wrong: bool = False
    expected_category: str | None = None
    actual_category: str | None = None
    expected_columns: list[str] = []
    actual_columns: list[str] = []
    expected_resolution: str | None = None
    actual_resolution: str | None = None
    expected_checks: list[str] = []
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    wall_s: float = 0.0


class MetricBlock(BaseModel):
    """One aggregate row (overall, or one fault kind)."""

    model_config = ConfigDict(extra="forbid")

    scenarios: int
    faults: int
    controls: int
    # Denominator sizes for the proportions that are not simply `faults` /
    # `controls`; the report uses them to show n and a Wilson interval.
    column_scoped: int = 0
    pr_expected: int = 0
    # Numerators are stored, not recomputed from a rounded rate, so the report
    # and the confidence interval always use the exact count.
    detection_success: int = 0
    category_success: int = 0
    columns_success: int = 0
    resolution_success: int = 0
    fix_success: int = 0
    semantic_success: int = 0
    false_positive_success: int = 0
    detection_recall: float | None = None
    rc_category_accuracy: float | None = None
    rc_columns_accuracy: float | None = None
    resolution_accuracy: float | None = None
    fix_validity_rate: float | None = None
    semantic_rate: float | None = None
    false_positive_rate: float | None = None
    verified_but_wrong: int = 0
    infra_errors: int = 0
    errors: int = 0
    total_cost_usd: float = 0.0
    median_cost_usd: float = 0.0
    p90_cost_usd: float = 0.0
    median_wall_s: float = 0.0
    p90_wall_s: float = 0.0


class Aggregates(BaseModel):
    overall: MetricBlock
    by_kind: dict[str, MetricBlock]


def _normalized_column(value: str) -> str:
    text = value.strip()
    for prefix in _TABLE_PREFIXES:
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.strip().lower()


def _table_name(value: str | None) -> str | None:
    if value is None:
        return None
    return value.split(".")[-1].strip().lower()


def _detected(scenario: Scenario, incidents: list[Incident]) -> bool:
    expected = scenario.expected
    if expected is None or scenario.table is None:
        return False
    wanted = set(expected.expected_checks)
    for incident in incidents:
        if _table_name(incident.table) != scenario.table.lower():
            continue
        if any(violation.check in wanted for violation in incident.violations):
            return True
    return False


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def score_scenario(scenario: Scenario, observation: Observation) -> ScenarioRun:
    """Score one scenario from typed artifacts; never guesses or calls an LLM."""
    expected = scenario.expected
    root_cause = observation.root_cause
    run = ScenarioRun(
        scenario_id=scenario.id,
        split=scenario.split.value,
        kind=scenario.kind,
        clean=scenario.is_control,
        repeat=observation.repeat,
        error=observation.error,
        infra_error=observation.infra_error,
        tool_calls=observation.tool_calls,
        tokens_in=observation.tokens_in,
        tokens_out=observation.tokens_out,
        cost_usd=round(observation.cost_usd, 6),
        wall_s=round(observation.wall_s, 3),
    )

    if scenario.is_control:
        if observation.infra_error is None:
            run.false_positive = bool(observation.incidents)
        return run

    assert expected is not None  # catalogue validation guarantees this
    run.expected_checks = list(expected.expected_checks)
    run.expected_category = expected.expected_category.value
    run.expected_columns = sorted(expected.acceptable_columns)
    run.expected_resolution = expected.expected_resolution.value

    # Infrastructure failures are recorded but never scored: the agent did not
    # get a fair chance, so counting the attempt as a miss would distort the
    # benchmark. `--resume` re-runs these records.
    if observation.infra_error is not None:
        return run

    run.detected = _detected(scenario, observation.incidents)
    run.actual_category = None if root_cause is None else root_cause.category.value
    run.rc_category_correct = (
        root_cause is not None and root_cause.category is expected.expected_category
    )

    if expected.acceptable_columns:
        actual_columns = [] if root_cause is None else root_cause.affected_columns
        run.actual_columns = sorted(actual_columns)
        run.rc_columns_correct = {_normalized_column(c) for c in actual_columns} == {
            _normalized_column(c) for c in expected.acceptable_columns
        }
    else:
        run.actual_columns = [] if root_cause is None else sorted(root_cause.affected_columns)
        run.rc_columns_correct = None  # not column-scoped

    run.actual_resolution = None if observation.resolution is None else observation.resolution.value
    run.resolution_correct = observation.resolution is expected.expected_resolution

    if expected.expected_resolution is ResolutionType.PR:
        run.verifier_passed = observation.verification_passed is True
        run.semantically_correct = observation.semantic_passed is True
        run.verified_but_wrong = (
            observation.verification_passed is True and observation.semantic_passed is False
        )
    return run


def wilson_interval(successes: int, n: int, *, z: float = 1.96) -> tuple[float, float] | None:
    """Two-sided 95% Wilson score interval for a proportion, or None when n=0.

    Wilson is used instead of the normal approximation because it behaves at the
    boundaries: 0/10 and 10/10 still get a sensible interval, and n=0 is
    explicitly "not applicable" rather than a division by zero.
    """
    if n <= 0:
        return None
    p = min(1.0, max(0.0, successes / n))
    denominator = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denominator
    margin = z * math.sqrt((p * (1.0 - p) / n) + (z * z) / (4.0 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 6) if values else 0.0


def _p90(values: list[float]) -> float:
    """Nearest-rank 90th percentile (deterministic, no interpolation)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(0.9 * len(ordered) + 0.999999) - 1))
    return round(ordered[index], 6)


def aggregate(runs: list[ScenarioRun]) -> Aggregates:
    """Overall and per-kind metrics; errors are misses in every denominator."""
    kinds = sorted({run.kind for run in runs})
    return Aggregates(
        overall=_block(runs),
        by_kind={kind: _block([run for run in runs if run.kind == kind]) for kind in kinds},
    )


def _block(runs: list[ScenarioRun]) -> MetricBlock:
    """Aggregate one scope; infrastructure errors are counted, never scored."""
    infra_errors = sum(1 for run in runs if run.infra_error is not None)
    scored = [run for run in runs if run.infra_error is None]
    faults = [run for run in scored if not run.clean]
    controls = [run for run in scored if run.clean]
    pr_expected = [run for run in faults if run.expected_resolution == ResolutionType.PR.value]
    column_scoped = [run for run in faults if run.rc_columns_correct is not None]
    detection_success = sum(1 for run in faults if run.detected is True)
    category_success = sum(1 for run in faults if run.rc_category_correct is True)
    columns_success = sum(1 for run in column_scoped if run.rc_columns_correct is True)
    resolution_success = sum(1 for run in faults if run.resolution_correct is True)
    fix_success = sum(1 for run in pr_expected if run.verifier_passed is True)
    semantic_success = sum(1 for run in pr_expected if run.semantically_correct is True)
    false_positive_success = sum(1 for run in controls if run.false_positive is True)

    return MetricBlock(
        scenarios=len(scored),
        faults=len(faults),
        controls=len(controls),
        column_scoped=len(column_scoped),
        pr_expected=len(pr_expected),
        detection_success=detection_success,
        category_success=category_success,
        columns_success=columns_success,
        resolution_success=resolution_success,
        fix_success=fix_success,
        semantic_success=semantic_success,
        false_positive_success=false_positive_success,
        detection_recall=_ratio(detection_success, len(faults)),
        rc_category_accuracy=_ratio(category_success, len(faults)),
        rc_columns_accuracy=_ratio(columns_success, len(column_scoped)),
        resolution_accuracy=_ratio(resolution_success, len(faults)),
        fix_validity_rate=_ratio(fix_success, len(pr_expected)),
        semantic_rate=_ratio(semantic_success, len(pr_expected)),
        false_positive_rate=_ratio(false_positive_success, len(controls)),
        verified_but_wrong=sum(1 for run in pr_expected if run.verified_but_wrong),
        infra_errors=infra_errors,
        errors=sum(1 for run in scored if run.error is not None),
        total_cost_usd=round(sum(run.cost_usd for run in runs), 6),
        median_cost_usd=_median([run.cost_usd for run in runs]),
        p90_cost_usd=_p90([run.cost_usd for run in runs]),
        median_wall_s=_median([run.wall_s for run in runs]),
        p90_wall_s=_p90([run.wall_s for run in runs]),
    )


__all__ = [
    "Aggregates",
    "MetricBlock",
    "Observation",
    "ScenarioRun",
    "aggregate",
    "score_scenario",
    "wilson_interval",
]
