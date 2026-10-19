"""Unit tests for the monitor helpers and approved-contract selection.

The monitor's DB loop is integration-tested; here the pure pieces are pinned:
`--as-of` parsing and the store rule that only approved contracts are enforced.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from dqs.contracts import store
from dqs.errors import CheckError
from dqs.models.contract import (
    Baseline,
    ColumnSpec,
    Contract,
    Freshness,
    Volume,
)
from dqs.models.enums import ContractStatus, Layer, LogicalType
from dqs.monitor import parse_as_of


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-02-03T00:00Z", dt.datetime(2026, 2, 3, tzinfo=dt.UTC)),
        ("2026-02-03T00:00:00+05:00", dt.datetime(2026, 2, 2, 19, 0, tzinfo=dt.UTC)),
        ("2026-02-03T00:00:00", dt.datetime(2026, 2, 3, tzinfo=dt.UTC)),  # naive ⇒ UTC
    ],
)
def test_parse_as_of(text: str, expected: dt.datetime) -> None:
    assert parse_as_of(text) == expected


def test_parse_as_of_rejects_a_non_iso_instant() -> None:
    with pytest.raises(CheckError):
        parse_as_of("not-a-date")


def _contract(status: ContractStatus) -> Contract:
    return Contract(
        table="raw.orders",
        layer=Layer.RAW,
        status=status,
        generated_at=dt.datetime(2026, 1, 31, tzinfo=dt.UTC),
        baseline=Baseline(batches=1, first="2026-01-01", last="2026-01-01", rows=1),
        primary_key=["order_id"],
        columns={
            "order_id": ColumnSpec(logical_type=LogicalType.INTEGER, nullable=False, unique=True)
        },
        volume=Volume(min_rows=0, max_rows=10, required_in_batch=True),
        freshness=Freshness(max_delay_minutes=360),
    )


def test_load_approved_enforces_only_approved_contracts(tmp_path: Path) -> None:
    store.save(_contract(ContractStatus.APPROVED), tmp_path)
    store.save(
        _contract(ContractStatus.DRAFT).model_copy(update={"table": "raw.payments"}), tmp_path
    )
    approved = store.load_approved(tmp_path)
    assert set(approved) == {"raw.orders"}
