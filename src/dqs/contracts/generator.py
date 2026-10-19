"""Contract builder: derives §7.2 rules + shared decisions into `Contract`s.

Pipeline: `dqs/contracts/profiler.py` produces profile facts per table over
the baseline window; this module turns them into Pydantic contracts with
deterministic discipline (pure functions of the profile). The core rules live
in docs/ARCHITECTURE.md §7.2; the editorial rules ("no bounds on keys/FK/id/
date columns; bounds only on measures", day-1 volume exclusion, products
special case, required_in_batch) are recorded in docs/DECISIONS.md Phase 2a.

Every threshold is a named module constant so tests can reason about margins
instead of guessing numbers:
- RANGE_SPAN_SHARE  range headroom as a fraction of the baseline span (0.25)
- MEAN_STD_MULT     mean-bound spread multiplier over batch-mean σ (4.0)
- MEAN_FALLBACK_SHARE spread when there are too few batch means (0.25)
- NULL_RATE_MULT    max_null_rate headroom multiplier (1.5) + slack (0.01)
- VOLUME_MIN_SHARE / VOLUME_MAX_SHARE volume window shares (0.6 / 1.5)
- PRODUCTS_MAX_SHARE products growth allowance vs the initial load (0.1)
- FK_COVERAGE       FK inference needs ≥99% joins resolved (§7.2)
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Final

from pydantic import BaseModel, ConfigDict

from dqs.contracts.profiler import (
    SYSTEM_COLUMNS,
    ColumnAggregate,
    TableProfile,
    _fk_columns_for_table,
)
from dqs.models.contract import (
    Baseline,
    ColumnReference,
    ColumnSpec,
    Contract,
    Freshness,
    NumericRange,
    Volume,
)
from dqs.models.enums import Layer, LogicalType

RANGE_SPAN_SHARE: Final = 0.25
MEAN_STD_MULT: Final = 4.0
MEAN_FALLBACK_SHARE: Final = 0.25
NULL_RATE_MULT: Final = 1.5
NULL_RATE_SLACK: Final = 0.01
VOLUME_MIN_SHARE: Final = 0.6
VOLUME_MAX_SHARE: Final = 1.5
PRODUCTS_MAX_SHARE: Final = 0.1
FK_COVERAGE: Final = 0.99  # §7.2: infer a reference only at ≥99% resolved joins
FK_ORPHAN_TOLERANCE: Final = 0.01
_STD_EPS: Final = 1e-9  # σ below this is "flat" → mean-bounds fallback applies
ALLOWED_VALUES_COVERAGE: Final = 0.995  # §7.2: ≥99.5% of values covered
PRODUCTS_TABLE: Final = "products"
DIMENSION_TABLES: Final[frozenset[str]] = frozenset({"products", "customers"})
FRESHNESS_MAX_DELAY_MINUTES: Final = 360
# Measure columns (amount/price/quantity/totals, DECISIONS Phase 2a): the only
# columns whose values get range/mean bounds. Keys, FK columns, *_id columns
# and date/timestamp columns get uniqueness/allowed_values/null checks instead.
MEASURE_FRAGMENTS: Final[tuple[str, ...]] = (
    "amount",
    "price",
    "quantity",
    "qty",
    "total",
    "revenue",
    "spend",
    "collected",
)


class Window(BaseModel):
    """Business-date window the profiled baseline covered.

    Staged contracts use these dates in their baseline record: one snapshot
    represents the state after the whole window was built.
    """

    model_config = ConfigDict(extra="forbid")
    first: dt.date
    last: dt.date


def is_measure_name(column: str) -> bool:
    """True for measure columns (amount/price/quantity/totals) only."""
    return any(fragment in column for fragment in MEASURE_FRAGMENTS)


def _key_like(aggregate: ColumnAggregate, references: ColumnReference | None) -> bool:
    """A column with key/temporal semantics never takes measure bounds."""
    return (
        aggregate.name.endswith("_id")
        or aggregate.logical_type in (LogicalType.DATE, LogicalType.TIMESTAMP)
        or references is not None
    )


def _headline_columns(profile: TableProfile) -> list[str]:
    """Contract column order: the most recent observed business header.

    Schema drift enforcement (§7.3) compares what a *future* batch carries to
    what the latest baseline batch carried; older header states are history,
    not the target shape. Loader-owned system columns never enter contracts
    (DECISIONS 23) and staged snapshots' audit aliases stay.
    """
    for batch in reversed(profile.batches):
        if batch.present and batch.header is not None:
            return [name for name in batch.header if name not in SYSTEM_COLUMNS]
    return list(profile.aggregates)


def _nullable(profile: TableProfile, aggregate: ColumnAggregate) -> bool:
    """Any null in the window (raw: any batch; staged: the snapshot) → nullable."""
    if profile.layer is Layer.RAW:
        batch_seen = False
        for batch in profile.batches:
            if not batch.present:
                continue
            batch_seen = True
            stats = batch.column_stats.get(aggregate.name)
            if stats is not None and stats.null_count > 0:
                return True
        # only say nullable: false when at least one batch actually carried rows
        return batch_seen and aggregate.non_null_count < aggregate.total_rows
    return aggregate.non_null_count < aggregate.total_rows


def _max_batch_null_rate(profile: TableProfile, column: str) -> float:
    """Largest per-batch null rate in the window (present, non-empty batches)."""
    largest = 0.0
    for batch in profile.batches:
        if not batch.present or batch.row_count == 0:
            continue
        stats = batch.column_stats.get(column)
        if stats is None:
            continue
        rate = stats.null_count / batch.row_count
        if rate > largest:
            largest = rate
    return largest


def _unique(profile: TableProfile, aggregate: ColumnAggregate, present_batches: int) -> bool:
    """Unique within every batch AND across the whole window (§7.2)."""
    if not aggregate.unique_in_window:
        return False
    for batch in profile.batches:
        if not batch.present or batch.row_count == 0:
            continue
        stats = batch.column_stats.get(aggregate.name)
        if stats is None or not stats.unique:
            return False
    # an all-empty window carries no uniqueness evidence either way
    return present_batches > 0


def _references(profile: TableProfile, column: str) -> ColumnReference | None:
    """The shared FK map's pair for this child column, at ≥99% coverage."""
    if profile.layer is not Layer.RAW:
        return None  # staged contracts carry no references (DECISIONS Phase 2a)
    aggregate = profile.aggregates.get(column)
    if aggregate is None or not aggregate.orphan_rates:
        return None
    if max(aggregate.orphan_rates) > FK_ORPHAN_TOLERANCE:
        return None
    mapping = _fk_columns_for_table(_bare_table(profile.table))
    parent = mapping.get(column)
    if parent is None:  # a column with rates but not in the map never happens
        return None
    return ColumnReference(table=f"raw.{parent[0]}", column=parent[1])


def _bare_table(qualified_or_bare: str) -> str:
    """`raw.orders` / `staging.stg_orders` / `orders` → `orders`-style name."""
    return qualified_or_bare.split(".", maxsplit=1)[-1]


def _range_bounds(aggregate: ColumnAggregate) -> NumericRange | None:
    """[min − 0.25·span, max + 0.25·span], floored at 0 for non-negative data."""
    if aggregate.min_value is None or aggregate.max_value is None:
        return None
    span = aggregate.max_value - aggregate.min_value
    low = aggregate.min_value - RANGE_SPAN_SHARE * span
    high = aggregate.max_value + RANGE_SPAN_SHARE * span
    if aggregate.min_value >= 0:  # amounts/quantities were never negative
        low = max(0.0, low)
    return NumericRange(min=_rounded(low), max=_rounded(high))


def _mean_bounds(aggregate: ColumnAggregate) -> NumericRange | None:
    """Per-batch mean window: μ ± 4·σ, or ±25% when σ≈0 or the window is too small."""
    means = aggregate.batch_means
    if not means:
        return None
    center = sum(means) / len(means)
    variance = sum((value - center) ** 2 for value in means) / len(means)
    sigma = math.sqrt(variance) if len(means) >= 2 else 0.0
    if sigma > _STD_EPS:  # enough spread to trust the batch-mean distribution
        spread = MEAN_STD_MULT * sigma
    else:  # §7.2 fallback: a single observation (or a flat one) hides σ
        spread = MEAN_FALLBACK_SHARE * center
    low = center - spread
    if center >= 0:  # means of amounts/quantities stay non-negative here
        low = max(0.0, low)
    return NumericRange(min=_rounded(low), max=_rounded(center + spread))


def _rounded(value: float) -> float:
    """Deterministic float output for YAML (6 dp is far below SQL noise)."""
    return round(value, 6)


def _allowed_values(aggregate: ColumnAggregate) -> list[str] | None:
    """distinct ≤ 12 AND ≥ 99.5% of non-null values covered (§7.2)."""
    counts = aggregate.value_counts
    if not counts or len(counts) != aggregate.distinct_count:
        return None
    covered = sum(count for _value, count in counts)
    if covered < ALLOWED_VALUES_COVERAGE * aggregate.non_null_count:
        return None
    return [value for value, _count in counts]


def _column_specs(profile: TableProfile, present_batches: int) -> dict[str, ColumnSpec]:
    """Build every contract column from its aggregate facts (§7.2 rules)."""
    specs: dict[str, ColumnSpec] = {}
    for column in _headline_columns(profile):
        aggregate = profile.aggregates.get(column)
        if aggregate is None:  # header column with no profiled facts
            specs[column] = ColumnSpec(logical_type=LogicalType.STRING, nullable=True)
            continue
        nullable = _nullable(profile, aggregate)
        references = _references(profile, column)
        unique = _unique(profile, aggregate, present_batches)
        take_bounds = (
            is_measure_name(column)
            and not _key_like(aggregate, references)
            and aggregate.logical_type in (LogicalType.INTEGER, LogicalType.NUMERIC)
        )
        range_bounds = _range_bounds(aggregate) if take_bounds else None
        mean_bounds = _mean_bounds(aggregate) if take_bounds else None
        max_null_rate = None
        if nullable:
            max_null_rate = _rounded(
                _max_batch_null_rate(profile, column) * NULL_RATE_MULT + NULL_RATE_SLACK
            )
        specs[column] = ColumnSpec(
            logical_type=aggregate.logical_type,
            nullable=nullable,
            max_null_rate=max_null_rate,
            unique=unique,
            references=references,
            allowed_values=_allowed_values(aggregate),
            range=range_bounds,
            mean_bounds=mean_bounds,
        )
    return specs


def _primary_key(profile: TableProfile, specs: dict[str, ColumnSpec]) -> list[str]:
    """First *_id column in declared order unique in-batch and in-window."""
    for column, aggregate in profile.aggregates.items():
        if not column.endswith("_id"):
            continue
        spec = specs.get(column)
        if spec is None or not spec.unique or aggregate.logical_type is not LogicalType.INTEGER:
            continue
        return [column]
    return []


def _raw_volume(profile: TableProfile) -> Volume:
    """Raw-layer volume window (§7.2 + DECISIONS Phase 2a exclusions).

    `initial_load_excluded` is set exactly when the day-1 initial load was left
    out of the histogram, so the check engine can exempt that same batch
    (otherwise a clean baseline fails on its own first day).
    """
    present = [batch for batch in profile.batches if batch.present]
    required = all(batch.present for batch in profile.batches)
    table_name = _bare_table(profile.table)
    if table_name == PRODUCTS_TABLE:
        initial = present[0].row_count if present else 0
        return Volume(
            min_rows=0,
            max_rows=math.ceil(PRODUCTS_MAX_SHARE * initial),
            required_in_batch=required,
            initial_load_excluded=True,
        )
    histogram = [batch.row_count for batch in present]
    table_name_in_dim = table_name in DIMENSION_TABLES and bool(profile.batches)
    if table_name_in_dim:
        # The day-1 initial load must not set dimension bounds: its rows dwarf
        # every later batch (DECISIONS Phase 2a / ARCHITECTURE §7.2).
        initial_batch_id = profile.batches[0].batch_id
        histogram = [batch.row_count for batch in present if batch.batch_id != initial_batch_id]
    excluded: bool | None = True if table_name_in_dim else None
    if not histogram:
        return Volume(
            min_rows=0, max_rows=0, required_in_batch=required, initial_load_excluded=excluded
        )
    return Volume(
        min_rows=max(0, math.floor(VOLUME_MIN_SHARE * min(histogram))),
        max_rows=math.ceil(VOLUME_MAX_SHARE * max(histogram)),
        required_in_batch=required,
        initial_load_excluded=excluded,
    )


def _staged_volume(profile: TableProfile) -> Volume:
    """Staged-layer volume: one snapshot, no batch/header semantics."""
    count = profile.total_rows
    return Volume(
        min_rows=max(0, math.floor(VOLUME_MIN_SHARE * count)),
        max_rows=math.ceil(VOLUME_MAX_SHARE * count),
        required_in_batch=False,
    )


def _volume(profile: TableProfile) -> Volume:
    return _raw_volume(profile) if profile.layer is Layer.RAW else _staged_volume(profile)


def _baseline(profile: TableProfile, window: Window) -> Baseline:
    """Baseline record: batch count, window dates, rows actually profiled."""
    if profile.layer is Layer.RAW:
        batch_count = len(profile.batches)
    else:
        batch_count = 1  # one snapshot observation carries the staged facts
    return Baseline(
        batches=batch_count,
        first=window.first.isoformat(),
        last=window.last.isoformat(),
        rows=profile.total_rows,
    )


def _generated_at(window: Window) -> dt.datetime:
    """Baseline-derived timestamp: byte-stable across runs by construction."""
    return dt.datetime.combine(window.last, dt.time.min, tzinfo=dt.UTC)


def _contract_table(profile: TableProfile) -> str:
    """`raw.orders` for the raw layer; staged profiles are already qualified."""
    if profile.layer is Layer.RAW:
        return f"raw.{profile.table}"
    return profile.table


def generate_contract(profile: TableProfile, window: Window) -> Contract:
    """Pure function of (profile facts, window) → a draft `Contract`."""
    present_batches = sum(1 for batch in profile.batches if batch.present)
    specs = _column_specs(profile, present_batches)
    return Contract(
        status="draft",
        table=_contract_table(profile),
        layer=profile.layer,
        generated_at=_generated_at(window),
        baseline=_baseline(profile, window),
        primary_key=_primary_key(profile, specs),
        columns=specs,
        volume=_volume(profile),
        freshness=Freshness(max_delay_minutes=FRESHNESS_MAX_DELAY_MINUTES),
    )
