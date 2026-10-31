"""Unit tests for the `dq.fix_attempts` reader (null plans are normal rows).

A fake engine stands in for Postgres so the null-plan contract is pinned
without Docker: a stored attempt that never produced a plan must be returned as
a `StoredFixAttempt(plan=None)`, while a row that *does* carry an invalid plan
is genuine corruption and still raises.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.helpers.publish import INCIDENT_ID, make_plan, make_verification

from dqs.errors import PublicationError
from dqs.publish.store import StoredFixAttempt, load_latest_fix_attempt


class _Result:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[Any, ...] | None:
        return self._row


class _Connection:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, statement: Any, params: Any = None) -> _Result:
        _ = statement, params
        return _Result(self._row)


class _Engine:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row

    def connect(self) -> _Connection:
        return _Connection(self._row)


def test_a_stored_null_plan_is_returned_as_an_attempt_not_an_error() -> None:
    verification = make_verification(passed=False)

    attempt = load_latest_fix_attempt(
        _Engine((3, None, verification.model_dump(mode="json"))),
        INCIDENT_ID,  # type: ignore[arg-type]
    )

    assert isinstance(attempt, StoredFixAttempt)
    assert attempt.attempt_no == 3
    assert attempt.plan is None
    assert attempt.verification.passed is False
    assert attempt.verification.failed_criteria == ["dbt_build"]


def test_a_latest_plan_is_returned_with_its_verification() -> None:
    plan = make_plan()

    attempt = load_latest_fix_attempt(
        _Engine((1, plan.model_dump(mode="json"), make_verification().model_dump(mode="json"))),  # type: ignore[arg-type]
        INCIDENT_ID,
    )

    assert attempt is not None
    assert attempt.plan == plan
    assert attempt.verification.passed is True


def test_no_attempt_returns_none() -> None:
    assert load_latest_fix_attempt(_Engine(None), INCIDENT_ID) is None  # type: ignore[arg-type]


def test_a_stored_row_without_a_valid_plan_still_raises() -> None:
    verification = make_verification().model_dump(mode="json")

    with pytest.raises(PublicationError, match="does not validate"):
        load_latest_fix_attempt(
            _Engine((1, {"error": "not a plan"}, verification)),
            INCIDENT_ID,  # type: ignore[arg-type]
        )


def test_a_stored_row_without_a_valid_verification_raises() -> None:
    plan = make_plan().model_dump(mode="json")

    with pytest.raises(PublicationError, match="does not validate"):
        load_latest_fix_attempt(
            _Engine((1, plan, {"passed": "maybe"})),
            INCIDENT_ID,  # type: ignore[arg-type]
        )
