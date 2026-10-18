"""Profile-vs-contract comparison (fit checking for the contracts tests).

Deliberately NOT the check engine (Phase 2b): no `Violation` model, no
severity, no incident fingerprinting. This module answers one test question —
"are a profile's statistics inside the bounds this contract promises?" —
deterministically, for the held-out-fit and fault-sensitivity acceptance
tests. The check engine will reuse the same *thresholds* (the constants
exported here), so generation-time acceptance and check-time detection cannot
drift apart unnoticed.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from dqs.contracts.profiler import BatchTableProfile, TableProfile
from dqs.models.contract import Contract

# Check-engine thresholds (ARCHITECTURE §7.3), defined here so both the fit
# harness and (from Phase 2b) the check engine read one copy.
FK_ORPHAN_MAX: float = 0.005  # fk.integrity fires above 0.5% orphans
TYPE_PARSE_MIN: float = 0.99  # type.logical fires below 99% parse rate
NEAR_BOUND_SHARE: float = 0.10  # "within 10% of a bound", the fit report


class StatFinding(BaseModel):
    """One statistic found outside its contract bound (evidence, not verdict)."""

    model_config = ConfigDict(extra="forbid")
    stat: str  # e.g. "nulls.rate", "schema.missing_column"
    batch_id: str | None = None
    column: str | None = None
    observed: str
    limit: str


class StatMargin(BaseModel):
    """How far a statistic sits from its bound, as a share of the bound's span.

    margin 0.0 = at the bound; 0.1 would mean "10% away"; values near zero are
    the ones the acceptance report must call out.
    """

    model_config = ConfigDict(extra="forbid")
    stat: str
    column: str | None = None
    margin: float


def _null_bound(contract: Contract, column: str) -> float | None:
    spec = contract.columns.get(column)
    if spec is None:
        return None
    return 0.0 if not spec.nullable else (spec.max_null_rate or 0.0)


def _outside(low: float, high: float, value: float) -> bool:
    return value < low or value > high


def _header_findings(profile_batch: BatchTableProfile, contract: Contract) -> list[StatFinding]:
    """Schema drift findings for one observed header (raw layer)."""
    findings: list[StatFinding] = []
    if profile_batch.header is None:
        return findings
    observed = set(profile_batch.header)
    contracted = set(contract.columns)
    for column in sorted(contracted - observed):
        findings.append(
            StatFinding(
                stat="schema.missing_column",
                batch_id=profile_batch.batch_id,
                column=column,
                observed=f"header carries {sorted(observed)}",
                limit=f"contract requires {column}",
            )
        )
    for column in sorted(observed - contracted):
        findings.append(
            StatFinding(
                stat="schema.unexpected_column",
                batch_id=profile_batch.batch_id,
                column=column,
                observed=f"header carries {column}",
                limit="contract has no such column",
            )
        )
    return findings


def _column_findings(batch: BatchTableProfile, contract: Contract) -> list[StatFinding]:
    """Per-column stat findings for one batch slice (nulls/type/unique/bounds)."""
    findings: list[StatFinding] = []
    for column, spec in contract.columns.items():
        stats = batch.column_stats.get(column)
        if stats is None:
            if batch.header is not None:
                findings.append(
                    StatFinding(
                        stat="profile.missing_column_stats",
                        batch_id=batch.batch_id,
                        column=column,
                        observed="no facts for the batch slice",
                        limit="column has a contract row",
                    )
                )
            continue
        null_rate = stats.null_count / batch.row_count if batch.row_count else 0.0
        bound = _null_bound(contract, column)
        if bound is not None and null_rate > bound:
            findings.append(
                StatFinding(
                    stat="nulls.rate",
                    batch_id=batch.batch_id,
                    column=column,
                    observed=f"null rate {null_rate:.4f}",
                    limit=f"≤ {bound:.4f}",
                )
            )
        if spec.unique and not stats.unique:
            findings.append(
                StatFinding(
                    stat="unique.pk",
                    batch_id=batch.batch_id,
                    column=column,
                    observed=f"{stats.distinct} distinct over {stats.non_null} non-null",
                    limit="every value unique",
                )
            )
        rate = stats.parse_rates.get(spec.logical_type.value)
        if rate is not None and rate < TYPE_PARSE_MIN:
            findings.append(
                StatFinding(
                    stat="type.logical",
                    batch_id=batch.batch_id,
                    column=column,
                    observed=f"parse rate {rate:.4f}",
                    limit=f"≥ {TYPE_PARSE_MIN} for {spec.logical_type.value}",
                )
            )
        if spec.mean_bounds is not None and stats.mean_value is not None:
            if _outside(spec.mean_bounds.min, spec.mean_bounds.max, stats.mean_value):
                findings.append(
                    StatFinding(
                        stat="mean_bounds",
                        batch_id=batch.batch_id,
                        column=column,
                        observed=f"batch mean {stats.mean_value:.4f}",
                        limit=f"{spec.mean_bounds.min}..{spec.mean_bounds.max}",
                    )
                )
        if spec.range is not None and (stats.min_value is not None and stats.max_value is not None):
            if _outside(spec.range.min, spec.range.max, stats.min_value) or _outside(
                spec.range.min, spec.range.max, stats.max_value
            ):
                findings.append(
                    StatFinding(
                        stat="range.bounds(value)",
                        batch_id=batch.batch_id,
                        column=column,
                        observed=f"values {stats.min_value}..{stats.max_value}",
                        limit=f"{spec.range.min}..{spec.range.max}",
                    )
                )
        if spec.references is not None and stats.orphan_rate is not None:
            if stats.orphan_rate > FK_ORPHAN_MAX:
                findings.append(
                    StatFinding(
                        stat="fk.integrity",
                        batch_id=batch.batch_id,
                        column=column,
                        observed=f"orphan rate {stats.orphan_rate:.4f}",
                        limit=f"≤ {FK_ORPHAN_MAX}",
                    )
                )
    return findings


def compare(profile: TableProfile, contract: Contract) -> list[StatFinding]:
    """Every statistic of `profile` outside `contract`'s bounds (empty = fits)."""
    findings: list[StatFinding] = []
    for batch in profile.batches:
        if contract.volume.required_in_batch and not batch.present:
            findings.append(
                StatFinding(
                    stat="volume.rows",
                    batch_id=batch.batch_id,
                    column=None,
                    observed="table absent from the batch",
                    limit="required_in_batch=true",
                )
            )
        if batch.present and _outside(
            contract.volume.min_rows, contract.volume.max_rows, batch.row_count
        ):
            findings.append(
                StatFinding(
                    stat="volume.count",
                    batch_id=batch.batch_id,
                    column=None,
                    observed=f"{batch.row_count} rows",
                    limit=f"{contract.volume.min_rows}..{contract.volume.max_rows}",
                )
            )
        findings.extend(_header_findings(batch, contract))
        findings.extend(_column_findings(batch, contract))
    return findings


def _share_distance(value: float, low: float, high: float) -> float:
    """Distance of `value` outside [low, high], as a share of the span.

    Purely for the near-bound report: a small (≤0.2) value means generation
    produced barely enough headroom — worth naming in the phase evidence.
    """
    span = high - low
    if span <= 0:
        return 0.0 if low <= value <= high else abs(value - low)
    floor_distance = (low - value) / span
    ceil_distance = (value - high) / span
    if floor_distance > 0:
        return floor_distance
    if ceil_distance > 0:
        return ceil_distance
    inside = min(value - low, high - value) / span
    return inside


def margins(profile: TableProfile, contract: Contract) -> list[StatMargin]:
    """Near-bound report: per stat/column, distance from the closest bound."""
    report: list[StatMargin] = []
    for batch in profile.batches:
        if batch.present:
            span = contract.volume.max_rows - contract.volume.min_rows
            report.append(
                StatMargin(
                    stat="volume.rows",
                    column=None,
                    margin=(
                        0.0
                        if span <= 0
                        else _share_distance(
                            batch.row_count,
                            contract.volume.min_rows,
                            contract.volume.max_rows,
                        )
                    ),
                )
            )
        for column, spec in contract.columns.items():
            stats = batch.column_stats.get(column)
            if stats is None:
                continue
            if spec.range is not None and stats.max_value is not None:
                report.append(
                    StatMargin(
                        stat="range.bounds",
                        column=column,
                        margin=_share_distance(stats.max_value, spec.range.min, spec.range.max),
                    )
                )
            if spec.mean_bounds is not None and stats.mean_value is not None:
                report.append(
                    StatMargin(
                        stat="mean_bounds",
                        column=column,
                        margin=_share_distance(
                            stats.mean_value,
                            spec.mean_bounds.min,
                            spec.mean_bounds.max,
                        ),
                    )
                )
    return report


def near_bound(stat: StatMargin) -> bool:
    """Inside the reportable 10% band (NEAR_BOUND_SHARE share of the span)."""
    return stat.margin <= NEAR_BOUND_SHARE
