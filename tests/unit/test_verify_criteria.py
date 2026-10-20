"""Unit tests for pure verification criteria helpers (Phase 5a)."""

from __future__ import annotations

import uuid

from dqs.models.enums import Layer, LogicalType, RootCauseCategory
from dqs.models.fix import Edit, FixPlan
from dqs.verify import criteria


def _plan(category: RootCauseCategory) -> FixPlan:
    return FixPlan(
        incident_id=uuid.uuid4(),
        batch_id="batch_20260131",
        category=category,
        affected_table="raw.orders",
        affected_columns=["order_id"],
        summary="s",
        edits=[Edit(path="models/staging/stg_orders.sql", old="a", new="b")],
        rationale="r",
        expected_effect="e",
        risk="low",
    )


def test_sandbox_schema_uses_the_project_macro_names() -> None:
    assert criteria.sandbox_schema("dqs_sandbox_deadbeef", Layer.STAGING) == (
        "dqs_sandbox_deadbeef_staging"
    )
    assert criteria.sandbox_schema("dqs_sandbox_deadbeef", Layer.MARTS) == (
        "dqs_sandbox_deadbeef_marts"
    )


def test_numeric_contract_accepts_an_integer_column() -> None:
    assert criteria._type_matches(LogicalType.NUMERIC, LogicalType.INTEGER)
    assert criteria._type_matches(LogicalType.INTEGER, LogicalType.INTEGER)
    assert not criteria._type_matches(LogicalType.STRING, LogicalType.INTEGER)
    assert not criteria._type_matches(LogicalType.TIMESTAMP, LogicalType.DATE)


def test_unsupported_category_cannot_pass_verification() -> None:
    result = criteria.targeted_check(
        None,  # type: ignore[arg-type]  # not touched before the category dispatch
        _plan(RootCauseCategory.UNKNOWN),
        "dqs_sandbox_deadbeef_staging",
        "stg_orders",
        None,
        None,
    )
    assert result.passed is False
    assert "no targeted check" in result.detail
