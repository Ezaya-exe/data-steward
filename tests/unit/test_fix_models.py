"""Unit tests for the fix-plan/verification models (Phase 5a)."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from dqs.models.enums import RootCauseCategory
from dqs.models.fix import (
    MAX_EDITS,
    MAX_NEW_BYTES,
    CriterionResult,
    Edit,
    FixPlan,
    VerificationResult,
)


def _plan(**overrides: object) -> FixPlan:
    data: dict[str, object] = {
        "incident_id": uuid.uuid4(),
        "batch_id": "batch_20260131",
        "category": RootCauseCategory.SOURCE_COLUMN_RENAMED,
        "affected_table": "raw.orders",
        "affected_columns": ["customer_id"],
        "summary": "restore customer_id",
        "edits": [Edit(path="models/staging/stg_orders.sql", old="customer_id", new="cust_id")],
        "rationale": "the source renamed the column",
        "expected_effect": "customer_id is populated again",
        "risk": "low: one expression",
    }
    data.update(overrides)
    return FixPlan.model_validate(data)


def test_valid_plan_round_trips_through_json() -> None:
    plan = _plan()
    restored = FixPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert restored.edits[0].path == "models/staging/stg_orders.sql"


def test_at_most_three_edits() -> None:
    edit = Edit(path="models/staging/stg_orders.sql", old="a", new="b")
    assert _plan(edits=[edit] * MAX_EDITS).edits
    with pytest.raises(ValidationError, match="at most 3 edits"):
        _plan(edits=[edit] * (MAX_EDITS + 1))


def test_replacement_text_is_bounded_in_bytes() -> None:
    # Multi-byte characters count as bytes, not characters: the limit is about
    # how much text a reviewer has to read, and UTF-8 is what gets written.
    text = "é" * (MAX_NEW_BYTES // 2)
    _plan(edits=[Edit(path="models/staging/stg_orders.sql", old="a", new=text)])
    with pytest.raises(ValidationError, match="plan limit"):
        _plan(edits=[Edit(path="models/staging/stg_orders.sql", old="a", new=text + "é")])


def test_no_op_edit_is_rejected() -> None:
    with pytest.raises(ValidationError, match="no-op"):
        Edit(path="models/staging/stg_orders.sql", old="a", new="a")


def test_edit_path_must_not_contain_nul_or_padding() -> None:
    with pytest.raises(ValidationError, match="NUL"):
        Edit(path="models/staging/a.sql\x00", old="a", new="b")
    with pytest.raises(ValidationError, match="whitespace"):
        Edit(path=" models/staging/a.sql", old="a", new="b")


def test_affected_table_must_be_a_raw_table() -> None:
    with pytest.raises(ValidationError, match="raw table"):
        _plan(affected_table="staging.stg_orders")
    with pytest.raises(ValidationError, match="raw table"):
        _plan(affected_table="raw.orders; drop table x")


def test_batch_id_shape_is_enforced() -> None:
    with pytest.raises(ValidationError, match="batch_YYYYMMDD"):
        _plan(batch_id="20260131")
    with pytest.raises(ValidationError, match="batch_YYYYMMDD"):
        _plan(batch_id="batch_20261332x")


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(extra_field=True)


def test_verification_result_derives_failures_from_criteria() -> None:
    result = VerificationResult.from_criteria(
        [
            CriterionResult(name="dbt_build", passed=True),
            CriterionResult(name="row_preservation", passed=False, detail="0 rows"),
            CriterionResult(name="row_preservation", passed=False, detail="0 rows"),
        ],
        duration_s=1.5,
    )
    assert result.passed is False
    assert result.failed_criteria == ["row_preservation"]


def test_verification_result_consistency_rules() -> None:
    with pytest.raises(ValidationError, match="passed verification"):
        VerificationResult(passed=True, failed_criteria=["dbt_build"])
    with pytest.raises(ValidationError, match="must name"):
        VerificationResult(passed=False, failed_criteria=[])


def test_verification_excerpt_is_bounded() -> None:
    result = VerificationResult.from_criteria(
        [CriterionResult(name="dbt_build", passed=True)],
        dbt_log_excerpt="x" * 5000,
    )
    assert len(result.dbt_log_excerpt) == 4096
