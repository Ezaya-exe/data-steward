"""Unit tests for every §7.3 check — pure, no Postgres (docs/PHASES.md Phase 2).

Each check is exercised with the edge cases the phase calls out: an all-null
column, an empty table, a single-row batch, unparseable text values, and a
column missing from the header. The last one proves the no-cascade rule: a
column absent from the header is reported only by `schema.missing_column`.
"""

from __future__ import annotations

import datetime as dt

import pytest

from dqs.checks.base import CheckContext
from dqs.checks.fk import FkIntegrityCheck
from dqs.checks.freshness import FreshnessSlaCheck, _date_range
from dqs.checks.nulls import NullRateCheck
from dqs.checks.ranges import RangeBoundsCheck
from dqs.checks.schema import MissingColumnCheck, UnexpectedColumnCheck
from dqs.checks.types import LogicalTypeCheck
from dqs.checks.unique import UniquePkCheck
from dqs.checks.volume import VolumeRowsCheck
from dqs.contracts.profiler import BatchTableProfile, ColumnBatchStats
from dqs.models.contract import (
    Baseline,
    ColumnReference,
    ColumnSpec,
    Contract,
    Freshness,
    NumericRange,
    Volume,
)
from dqs.models.enums import ContractStatus, Layer, LogicalType, Severity

BATCH_ID = "batch_20260131"
BUSINESS_DATE = dt.date(2026, 1, 31)


def spec(**kwargs: object) -> Contract:
    """A small approved raw contract; keyword-driven for per-test tailoring."""
    columns = kwargs.pop("columns", None)
    if columns is None:
        columns = {
            "order_id": ColumnSpec(logical_type=LogicalType.INTEGER, nullable=False, unique=True),
            "amount": ColumnSpec(
                logical_type=LogicalType.NUMERIC,
                nullable=False,
                range=NumericRange(min=0.0, max=1000.0),
                mean_bounds=NumericRange(min=400.0, max=600.0),
            ),
        }
    fields: dict[str, object] = {
        "table": "raw.orders",
        "layer": Layer.RAW,
        "status": ContractStatus.APPROVED,
        "generated_at": dt.datetime(2026, 1, 31, tzinfo=dt.UTC),
        "baseline": Baseline(batches=30, first="2026-01-01", last="2026-01-30", rows=1000),
        "primary_key": ["order_id"],
        "columns": columns,
        "volume": Volume(min_rows=1, max_rows=100, required_in_batch=True),
        "freshness": Freshness(max_delay_minutes=360),
    }
    fields.update(kwargs)
    return Contract.model_validate(fields)


def stats(name: str, **kwargs: object) -> ColumnBatchStats:
    fields: dict[str, object] = {
        "name": name,
        "non_null": 10,
        "null_count": 0,
        "distinct": 10,
        "unique": True,
    }
    fields.update(kwargs)
    return ColumnBatchStats.model_validate(fields)


def profile(
    column_stats: dict[str, ColumnBatchStats],
    *,
    header: list[str] | None = None,
    row_count: int = 10,
    present: bool = True,
) -> BatchTableProfile:
    return BatchTableProfile(
        table="orders",
        batch_id=BATCH_ID,
        business_date=BUSINESS_DATE,
        present=present,
        row_count=row_count,
        header=header if header is not None else sorted(column_stats),
        column_stats=column_stats,
    )


def ctx(
    contract: Contract, batch_profile: BatchTableProfile | None, **kwargs: object
) -> CheckContext:
    fields: dict[str, object] = {
        "engine": None,
        "contract": contract,
        "profile": batch_profile,
        "batch_id": BATCH_ID,
    }
    fields.update(kwargs)
    return CheckContext(**fields)  # type: ignore[arg-type]


# --- schema --------------------------------------------------------------------------


def test_missing_column_is_breaking_and_lists_the_observed_header() -> None:
    contract = spec()
    observed = profile({"order_id": stats("order_id")}, header=["order_id"])
    violations = MissingColumnCheck().run(ctx(contract, observed))
    assert [v.column for v in violations] == ["amount"]
    assert violations[0].severity is Severity.BREAKING
    assert violations[0].observed["header"] == ["order_id"]


def test_unexpected_column_is_a_warning_and_ignores_system_columns() -> None:
    contract = spec()
    observed = profile(
        {"order_id": stats("order_id"), "amount": stats("amount"), "coupon": stats("coupon")},
        header=["order_id", "amount", "coupon", "_batch_id", "_loaded_at"],
    )
    violations = UnexpectedColumnCheck().run(ctx(contract, observed))
    assert [v.column for v in violations] == ["coupon"]
    assert violations[0].severity is Severity.WARNING


def test_schema_checks_are_silent_when_the_table_is_absent() -> None:
    contract = spec()
    absent = profile({}, header=None, present=False, row_count=0)
    assert MissingColumnCheck().run(ctx(contract, absent)) == []
    assert UnexpectedColumnCheck().run(ctx(contract, absent)) == []


# --- type ----------------------------------------------------------------------------


def test_type_logical_fires_below_threshold_and_skips_unparseable_missing_values() -> None:
    contract = spec()
    observed = profile(
        {
            "order_id": stats("order_id", parse_rates={"integer": 1.0}),
            "amount": stats("amount", parse_rates={"numeric": 0.5}),
        }
    )
    violations = LogicalTypeCheck().run(ctx(contract, observed))
    assert [v.column for v in violations] == ["amount"]
    assert violations[0].severity is Severity.BREAKING


def test_type_logical_is_silent_on_an_all_null_column() -> None:
    contract = spec()
    observed = profile(
        {
            "order_id": stats("order_id", parse_rates={"integer": 1.0}),
            "amount": stats("amount", non_null=0, null_count=10, parse_rates={}),
        }
    )
    assert LogicalTypeCheck().run(ctx(contract, observed)) == []


# --- nulls ---------------------------------------------------------------------------


def test_nulls_rate_breaking_for_non_nullable_and_warning_for_nullable() -> None:
    contract = spec(
        columns={
            "order_id": ColumnSpec(logical_type=LogicalType.INTEGER, nullable=False),
            "note": ColumnSpec(logical_type=LogicalType.STRING, nullable=True, max_null_rate=0.2),
        }
    )
    observed = profile(
        {
            "order_id": stats("order_id", non_null=8, null_count=2),
            "note": stats("note", non_null=7, null_count=3),
        }
    )
    violations = {v.column: v for v in NullRateCheck().run(ctx(contract, observed))}
    assert violations["order_id"].severity is Severity.BREAKING
    assert violations["note"].severity is Severity.WARNING


def test_nulls_rate_is_silent_on_an_empty_table() -> None:
    contract = spec()
    observed = profile({"order_id": stats("order_id")}, row_count=0)
    assert NullRateCheck().run(ctx(contract, observed)) == []


def test_value_checks_do_not_cascade_for_a_column_missing_from_the_header() -> None:
    """`amount` is absent from the header: only schema.missing_column may speak."""
    contract = spec()
    observed = profile(
        {"order_id": stats("order_id")},
        header=["order_id"],
        row_count=10,
    )
    ctx_ = ctx(contract, observed)
    assert MissingColumnCheck().run(ctx_) != []
    assert NullRateCheck().run(ctx_) == []
    assert LogicalTypeCheck().run(ctx_) == []
    assert RangeBoundsCheck().run(ctx_) == []


# --- range ---------------------------------------------------------------------------


def test_range_bounds_breaking_when_the_mean_is_out_but_warning_when_isolated() -> None:
    contract = spec()
    mean_out = profile(
        {
            "order_id": stats("order_id"),
            "amount": stats("amount", min_value=1.0, max_value=999.0, mean_value=5000.0),
        }
    )
    violations = RangeBoundsCheck().run(ctx(contract, mean_out))
    assert len(violations) == 1
    assert violations[0].severity is Severity.BREAKING

    isolated = profile(
        {
            "order_id": stats("order_id"),
            "amount": stats("amount", min_value=1.0, max_value=99999.0, mean_value=500.0),
        }
    )
    violations = RangeBoundsCheck().run(ctx(contract, isolated))
    assert len(violations) == 1
    assert violations[0].severity is Severity.WARNING


def test_range_bounds_skips_unparseable_values_and_single_row_batches() -> None:
    contract = spec()
    unparseable = profile(
        {
            "order_id": stats("order_id"),
            "amount": stats("amount", min_value=None, max_value=None, mean_value=None),
        }
    )
    assert RangeBoundsCheck().run(ctx(contract, unparseable)) == []

    single = profile(
        {
            "order_id": stats("order_id", non_null=1, distinct=1),
            "amount": stats(
                "amount", non_null=1, distinct=1, min_value=5.0, max_value=5.0, mean_value=5.0
            ),
        },
        row_count=1,
    )
    assert RangeBoundsCheck().run(ctx(contract, single)) == []


# --- volume --------------------------------------------------------------------------


def test_volume_requires_a_table_only_when_the_contract_says_so() -> None:
    required = spec()
    optional = spec(volume=Volume(min_rows=0, max_rows=100, required_in_batch=False))
    absent = profile({}, header=None, present=False, row_count=0)
    assert len(VolumeRowsCheck().run(ctx(required, absent))) == 1
    assert VolumeRowsCheck().run(ctx(optional, absent)) == []


def test_volume_flags_a_present_but_out_of_window_table() -> None:
    contract = spec(volume=Volume(min_rows=10, max_rows=20, required_in_batch=True))
    empty = profile({"order_id": stats("order_id")}, row_count=0)
    violations = VolumeRowsCheck().run(ctx(contract, empty))
    assert len(violations) == 1
    assert violations[0].observed == {"row_count": 0}


# --- unique --------------------------------------------------------------------------


def test_unique_pk_fires_within_the_batch_without_a_database() -> None:
    contract = spec()
    duplicated = profile({"order_id": stats("order_id", non_null=10, distinct=9, unique=False)})
    violations = UniquePkCheck().run(ctx(contract, duplicated))
    assert [v.column for v in violations] == ["order_id"]
    assert violations[0].severity is Severity.BREAKING
    assert violations[0].sample == []  # no engine: still fires, no sample


def test_unique_pk_is_silent_on_a_clean_unique_column() -> None:
    contract = spec()
    clean = profile({"order_id": stats("order_id", non_null=10, distinct=10, unique=True)})
    assert UniquePkCheck().run(ctx(contract, clean)) == []


# --- fk ------------------------------------------------------------------------------


def test_fk_integrity_fires_above_the_orphan_tolerance() -> None:
    contract = spec(
        columns={
            "order_id": ColumnSpec(logical_type=LogicalType.INTEGER, nullable=False),
            "customer_id": ColumnSpec(
                logical_type=LogicalType.INTEGER,
                nullable=False,
                references=ColumnReference(table="raw.customers", column="customer_id"),
            ),
        }
    )
    observed = profile(
        {
            "order_id": stats("order_id"),
            "customer_id": stats("customer_id", orphan_rate=0.5),
        }
    )
    violations = FkIntegrityCheck().run(ctx(contract, observed))
    assert [v.column for v in violations] == ["customer_id"]
    assert violations[0].severity is Severity.BREAKING


def test_fk_integrity_is_silent_below_tolerance_and_when_column_absent() -> None:
    contract = spec(
        columns={
            "order_id": ColumnSpec(logical_type=LogicalType.INTEGER, nullable=False),
            "customer_id": ColumnSpec(
                logical_type=LogicalType.INTEGER,
                nullable=False,
                references=ColumnReference(table="raw.customers", column="customer_id"),
            ),
        }
    )
    below = profile(
        {"order_id": stats("order_id"), "customer_id": stats("customer_id", orphan_rate=0.001)}
    )
    assert FkIntegrityCheck().run(ctx(contract, below)) == []

    header_missing = profile({"order_id": stats("order_id")}, header=["order_id"])
    assert FkIntegrityCheck().run(ctx(contract, header_missing)) == []


# --- freshness / missing batch -------------------------------------------------------


def test_freshness_fires_only_after_the_sla_deadline() -> None:
    contract = spec()
    on_time = ctx(
        contract,
        None,
        business_date=BUSINESS_DATE,
        arrived_at=dt.datetime(2026, 2, 1, 2, 0, tzinfo=dt.UTC),
        batch_id=BATCH_ID,
    )
    assert FreshnessSlaCheck().run(on_time) == []

    late = ctx(
        contract,
        None,
        business_date=BUSINESS_DATE,
        arrived_at=dt.datetime(2026, 2, 2, 2, 0, tzinfo=dt.UTC),
        batch_id=BATCH_ID,
    )
    violations = FreshnessSlaCheck().run(late)
    assert [v.check for v in violations] == ["freshness.sla"]
    assert violations[0].severity is Severity.BREAKING


@pytest.mark.parametrize(
    ("first", "last", "count"),
    [
        (dt.date(2026, 1, 1), dt.date(2026, 1, 1), 1),
        (dt.date(2026, 1, 1), dt.date(2026, 1, 3), 3),
        (dt.date(2026, 1, 5), dt.date(2026, 1, 1), 0),
    ],
)
def test_date_range_helper(first: dt.date, last: dt.date, count: int) -> None:
    assert len(_date_range(first, last)) == count
