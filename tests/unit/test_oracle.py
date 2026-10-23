"""Tests for the test-only clean-data oracle (Phase 5b).

The oracle is an answer key, not product code: these tests exercise the pure
comparison and prove that no production module can import it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from dqs.bench.oracle import clean_frame, compare_frames

PROJECT = Path(__file__).resolve().parents[2]
COLUMNS = ["order_id", "customer_id", "order_ts", "status", "total_amount"]


def _expected() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "order_id": [1, 2, 3],
            "customer_id": [10, 11, 12],
            "order_ts": [
                "2026-01-31T10:00:00+00:00",
                "2026-01-31T11:00:00+00:00",
                "2026-01-31T12:00:00+00:00",
            ],
            "status": ["new", "paid", "shipped"],
            "total_amount": [10.5, 20.0, 30.25],
        }
    )


def test_clean_generator_frame_is_deterministic() -> None:
    first = clean_frame("orders", dt.date(2026, 1, 31), 42)
    second = clean_frame("orders", dt.date(2026, 1, 31), 42)
    pd.testing.assert_frame_equal(first, second)
    assert list(first.columns) == COLUMNS


def test_identical_frames_pass_and_extra_batch_columns_are_ignored() -> None:
    actual = _expected().assign(_batch_id="batch_20260131", _loaded_at="ignored")
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert result.passed
    assert result.rows_expected == 3
    assert result.rows_actual == 3
    assert result.detail == "patched rows match the clean generated frame"


def test_decimal_and_float_values_compare_numerically() -> None:
    actual = _expected().copy()
    actual["total_amount"] = [Decimal("10.50"), Decimal("20.00"), Decimal("30.25")]
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert result.passed


def test_missing_key_is_reported() -> None:
    actual = _expected().iloc[:2]
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert not result.passed
    assert result.missing_keys == [3]
    assert "missing=1" in result.detail


def test_unexpected_key_is_reported() -> None:
    actual = _expected().copy()
    actual.loc[3] = [4, 13, "2026-01-31T13:00:00+00:00", "new", 1.0]
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert not result.passed
    assert result.unexpected_keys == [4]


def test_duplicate_key_is_reported_even_when_values_match() -> None:
    actual = pd.concat([_expected(), _expected().iloc[[0]]], ignore_index=True)
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert not result.passed
    assert result.duplicate_keys == [1]
    assert result.rows_actual == 4


def test_wrong_value_is_reported_with_key_and_column() -> None:
    actual = _expected().copy()
    actual.loc[1, "total_amount"] = 999.0
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert not result.passed
    assert result.mismatched_cells == ["order_id=2 column=total_amount"]


def test_missing_output_column_is_reported() -> None:
    actual = _expected().drop(columns=["customer_id"])
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert not result.passed
    assert "missing column(s): customer_id" in result.detail


def test_missing_key_column_is_reported() -> None:
    actual = _expected().drop(columns=["order_id"])
    result = compare_frames(_expected(), actual, key="order_id", columns=COLUMNS)
    assert not result.passed
    assert "key column 'order_id'" in result.detail


def test_bench_package_is_the_only_oracle_location() -> None:
    """The answer key must not live behind production agents or verifiers."""
    project = Path(__file__).resolve().parents[2]
    legacy = project / "tests" / "helpers" / "oracle.py"
    assert not legacy.exists()
    for relative in ("agents", "verify", "workflow", "publish", "tools", "checks"):
        for path in (project / "src" / "dqs" / relative).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "dqs.bench" not in text, path
            assert "oracle" not in text.lower(), path


@pytest.mark.parametrize("table", ["stg_orders; drop table x", "StgOrders", "stg_orders--"])
def test_oracle_rejects_unsafe_identifiers_before_touching_the_db(table: str) -> None:
    from dqs.bench import oracle

    with pytest.raises(ValueError, match="unsafe"):
        oracle.compare_patched_staging(
            settings=None,  # type: ignore[arg-type]
            plan=None,  # type: ignore[arg-type]
            engine=None,  # type: ignore[arg-type]
            fault_date=dt.date(2026, 1, 31),
            seed=42,
            staging_table=table,
        )
