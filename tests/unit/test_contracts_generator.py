"""Unit tests for contract generation rules on synthetic profile facts.

Why synthetic: every §7.2 + DECISIONS rule must be provable from named
profile facts (no DB needed), with negative cases: a non-measure column never
gets range/mean bounds, a FK column outside the shared map never gets a
reference, absent batches lower volume but never `required_in_batch`.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import pytest

from dqs.contracts.generator import (
    FK_ORPHAN_TOLERANCE,
    Window,
    generate_contract,
    is_measure_name,
)
from dqs.contracts.profiler import (
    BatchTableProfile,
    ColumnAggregate,
    ColumnBatchStats,
    TableProfile,
)
from dqs.models.enums import Layer, LogicalType

WINDOW: Final = Window(first=dt.date(2026, 1, 1), last=dt.date(2026, 1, 30))


def _agg(
    name: str,
    logical: LogicalType,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    distinct: int = 10,
    non_null: int = 10,
    total: int = 10,
    unique: bool | None = None,
    means: tuple[float, ...] = (),
    orphans: tuple[float, ...] = (),
    value_counts: tuple[tuple[str, int], ...] = (),
) -> ColumnAggregate:
    return ColumnAggregate(
        name=name,
        logical_type=logical,
        distinct_count=distinct,
        non_null_count=non_null,
        total_rows=total,
        unique_in_window=(distinct == non_null) if unique is None else unique,
        min_value=min_value,
        max_value=max_value,
        batch_means=means,
        orphan_rates=orphans,
        value_counts=value_counts,
    )


def _batch(
    batch_id: str,
    *,
    row_count: int,
    header: list[str],
    stats: dict[str, ColumnBatchStats] | None = None,
) -> BatchTableProfile:
    column_stats = stats or {
        name: ColumnBatchStats(
            name=name, non_null=row_count, null_count=0, distinct=1, unique=False
        )
        for name in header
    }
    return BatchTableProfile(
        table="orders",
        batch_id=batch_id,
        business_date=dt.date(2026, 1, 1),
        present=True,
        row_count=row_count,
        header=header,
        column_stats=column_stats,
    )


def _profile(
    table: str,
    layer: Layer,
    batches: list[BatchTableProfile],
    aggregates: dict[str, ColumnAggregate],
) -> TableProfile:
    return TableProfile(
        table=table,
        layer=layer,
        batches=batches,
        aggregates=aggregates,
        total_rows=sum(b.row_count for b in batches if b.present),
        first_date=WINDOW.first,
        last_date=WINDOW.last,
    )


def _single(table: str, layer: Layer, aggregate: ColumnAggregate) -> TableProfile:
    batch = _batch("b0", row_count=aggregate.total_rows, header=[aggregate.name])
    return _profile(table, layer, [batch], {aggregate.name: aggregate})


def _raw_nullable(
    column: str, nulls_per_batch: list[int], rows_per_batch: list[int]
) -> TableProfile:
    """A raw profile where per-batch null rates vary predictably."""
    stats: dict[str, ColumnBatchStats] = {}
    batches: list[BatchTableProfile] = []
    for batch_no, (nulls, rows) in enumerate(zip(nulls_per_batch, rows_per_batch, strict=False)):
        stats[f"{column}"] = ColumnBatchStats(
            name=column,
            non_null=rows - nulls,
            null_count=nulls,
            distinct=1,
            unique=False,
        )
        batches.append(
            _batch(
                f"b{batch_no}", row_count=rows, header=[column], stats={column: stats[f"{column}"]}
            )
        )
    aggregate = ColumnAggregate(
        name=column,
        logical_type=LogicalType.STRING,
        distinct_count=rows_per_batch[0] - nulls_per_batch[0],
        non_null_count=sum(r - n for r, n in zip(rows_per_batch, nulls_per_batch, strict=False)),
        total_rows=sum(rows_per_batch),
        unique_in_window=False,
    )
    return _profile("payments", Layer.RAW, batches, {column: aggregate})


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        ("total_amount", True),
        ("unit_price", True),
        ("price", True),
        ("amount", True),
        ("quantity", True),
        ("item_qty", True),
        ("payment_total", True),
        ("total_spend", True),
        ("revenue", True),
        ("collected", True),
        ("customer_id", False),
        ("order_item_id", False),
        ("status", False),
        ("order_ts", False),
        ("signup_date", False),
        ("email", False),
    ],
)
def test_is_measure_name_matches_phase2_decision_list(column: str, expected: bool) -> None:
    assert is_measure_name(column) is expected


def test_measure_column_gets_range_and_mean_bounds() -> None:
    aggregate = _agg(
        "total_amount",
        LogicalType.NUMERIC,
        min_value=100.0,
        max_value=200.0,
        means=(600.0, 640.0, 620.0),
    )
    contract = generate_contract(_single("orders", Layer.RAW, aggregate), WINDOW)
    spec = contract.columns["total_amount"]
    # range = [min - 0.25*span, max + 0.25*span] over the window (span 100)
    assert spec.range is not None
    assert spec.range.min == 75.0
    assert spec.range.max == 225.0
    # mean over batch means = 620, σ(population) = 16.33 → ±4σ
    assert spec.mean_bounds is not None
    assert spec.mean_bounds.min == pytest.approx(554.68, abs=0.01)
    assert spec.mean_bounds.max == pytest.approx(685.32, abs=0.01)


def test_key_and_fk_columns_never_get_measure_bounds() -> None:
    key = _agg("customer_id", LogicalType.INTEGER, min_value=1, max_value=900)
    date_col = _agg("signup_date", LogicalType.DATE)
    contract = generate_contract(_single("orders", Layer.RAW, key), WINDOW)
    assert contract.columns["customer_id"].range is None
    contract = generate_contract(_single("customers", Layer.RAW, date_col), WINDOW)
    assert contract.columns["signup_date"].range is None
    assert contract.columns["signup_date"].mean_bounds is None


def test_range_lower_bound_clamped_for_never_negative_measures() -> None:
    aggregate = _agg("amount", LogicalType.NUMERIC, min_value=10.0, max_value=190.0)
    contract = generate_contract(_single("payments", Layer.RAW, aggregate), WINDOW)
    assert contract.columns["amount"].range is not None
    assert contract.columns["amount"].range.min == 0.0  # 10 - 45 → clamped


def test_mean_bounds_fallback_when_std_is_zero() -> None:
    aggregate = _agg("price", LogicalType.NUMERIC, means=(250.0, 250.0, 250.0))
    contract = generate_contract(_single("products", Layer.RAW, aggregate), WINDOW)
    spec = contract.columns["price"]
    assert spec.mean_bounds is not None
    assert spec.mean_bounds.min == pytest.approx(187.5, abs=0.01)  # -25%
    assert spec.mean_bounds.max == pytest.approx(312.5, abs=0.01)  # +25%


def test_nullable_rules_and_max_null_rate_formula() -> None:
    clean = _agg("status", LogicalType.STRING, value_counts=(("paid", 10),))
    contract = generate_contract(_single("orders", Layer.RAW, clean), WINDOW)
    assert contract.columns["status"].nullable is False
    assert contract.columns["status"].max_null_rate is None

    # 10% nulls in the worst batch → max_null_rate = 0.10 * 1.5 + 0.01 = 0.16
    contract = generate_contract(
        _raw_nullable("method", nulls_per_batch=[1], rows_per_batch=[10]),
        WINDOW,
    )
    spec = contract.columns["method"]
    assert spec.nullable is True
    assert spec.max_null_rate == pytest.approx(0.16, abs=0.001)


def test_unique_candidate_must_hold_in_every_batch() -> None:
    # window-unique duplication inside ONE batch breaks uniqueness
    aggregate = _agg("email", LogicalType.STRING, distinct=30, non_null=30)
    batch = _batch(
        "b0",
        row_count=10,
        header=["email"],
        stats={
            "email": ColumnBatchStats(
                name="email", non_null=10, null_count=0, distinct=9, unique=False
            )
        },
    )
    profile = _profile("customers", Layer.RAW, [batch], {"email": aggregate})
    contract = generate_contract(profile, WINDOW)
    assert contract.columns["email"].unique is False
    assert contract.primary_key == []


def test_primary_key_first_unique_id_column_in_declared_order() -> None:
    first = _agg("batch_number", LogicalType.INTEGER, distinct=10, non_null=10)
    second = _agg("order_item_id", LogicalType.INTEGER, distinct=10, non_null=10)
    unique_stats = {
        "batch_number": ColumnBatchStats(
            name="batch_number", non_null=10, null_count=0, distinct=10, unique=True
        ),
        "order_item_id": ColumnBatchStats(
            name="order_item_id", non_null=10, null_count=0, distinct=10, unique=True
        ),
    }
    profile = _profile(
        "order_items",
        Layer.RAW,
        [_batch("b0", row_count=10, header=["batch_number", "order_item_id"], stats=unique_stats)],
        {"batch_number": first, "order_item_id": second},
    )
    contract = generate_contract(profile, WINDOW)
    assert contract.primary_key == ["order_item_id"]


def test_fk_reference_inference_follows_shared_fk_map_and_coverage() -> None:
    from dqs.contracts.profiler import _fk_columns_for_table

    mapping = _fk_columns_for_table("orders")
    assert mapping["customer_id"] == ("customers", "customer_id")
    covered = _agg("customer_id", LogicalType.INTEGER, orphans=(0.0, 0.0, 0.0))
    contract = generate_contract(_single("orders", Layer.RAW, covered), WINDOW)
    assert contract.columns["customer_id"].references is not None
    reference = contract.columns["customer_id"].references
    assert reference is not None
    assert reference.table == "raw.customers"
    assert reference.column == "customer_id"

    orphaned = _agg("customer_id", LogicalType.INTEGER, orphans=(0.0, 0.02, 0.0))
    contract = generate_contract(_single("orders", Layer.RAW, orphaned), WINDOW)
    assert contract.columns["customer_id"].references is None
    assert FK_ORPHAN_TOLERANCE == 0.01


def test_missing_table_lowerers_volume_but_breaks_required_in_batch() -> None:

    aggregates = {"order_id": _agg("order_id", LogicalType.INTEGER, distinct=9, non_null=9)}
    present_batch = _batch("b1", row_count=100, header=["order_id"])
    absent = BatchTableProfile(
        table="orders",
        batch_id="b2",
        business_date=dt.date(2026, 1, 2),
        present=False,
        row_count=0,
        header=None,
        column_stats={},
    )
    contract = generate_contract(
        _profile("orders", Layer.RAW, [present_batch, absent], aggregates), WINDOW
    )
    assert contract.volume.min_rows == 60
    assert contract.volume.max_rows == 150
    assert contract.volume.required_in_batch is False


def test_dimension_volume_excludes_day_one_initial_load() -> None:
    from dqs.contracts.profiler import ColumnBatchStats

    stats_day1 = ColumnBatchStats(
        name="customer_id", non_null=2000, null_count=0, distinct=2000, unique=True
    )
    day1 = _batch("b0", row_count=2000, header=["customer_id"], stats={"customer_id": stats_day1})
    later = [
        _batch("b1", row_count=40, header=["customer_id"]),
        _batch("b2", row_count=50, header=["customer_id"]),
    ]
    aggregate = _agg("customer_id", LogicalType.INTEGER, distinct=2090, non_null=2090)
    contract = generate_contract(
        _profile("customers", Layer.RAW, [day1, *later], {"customer_id": aggregate}), WINDOW
    )
    assert contract.volume.min_rows == 24  # floor(0.6 * 40), day-1 excluded
    assert contract.volume.max_rows == 75  # ceil(1.5 * 50)


def test_products_volume_special_case() -> None:
    identity = _agg("product_id", LogicalType.INTEGER, distinct=200, non_null=200)
    profile = _profile(
        "products",
        Layer.RAW,
        [
            _batch("b0", row_count=200, header=["product_id"]),
            _batch("b1", row_count=0, header=["product_id"]),
        ],
        {"product_id": identity},
    )
    contract = generate_contract(profile, WINDOW)
    assert contract.volume.min_rows == 0
    assert contract.volume.max_rows == 20  # ceil(0.1 * initial 200)
    assert contract.volume.required_in_batch is True


def test_staged_layer_volume_and_no_references() -> None:
    aggregate = _agg(
        "order_id",
        LogicalType.INTEGER,
        distinct=100,
        non_null=100,
    )
    contract = generate_contract(_single("staging.stg_orders", Layer.STAGING, aggregate), WINDOW)
    assert contract.volume.min_rows == 6  # floor(0.6 * 10 snapshot rows)
    assert contract.volume.max_rows == 15  # ceil(1.5 * 10)
    assert contract.volume.required_in_batch is False
    assert contract.columns["order_id"].references is None  # staged FK fields never


def test_contract_is_deterministic_metadata() -> None:
    aggregate = _agg("order_id", LogicalType.INTEGER, distinct=10, non_null=10)
    c1 = generate_contract(_single("orders", Layer.RAW, aggregate), WINDOW)
    c2 = generate_contract(_single("orders", Layer.RAW, aggregate), WINDOW)
    assert c1 == c2
    assert c1.generated_at.isoformat() == "2026-01-30T00:00:00+00:00"
    assert c1.status.value == "draft"
    assert c1.freshness.max_delay_minutes == 360
