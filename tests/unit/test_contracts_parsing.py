"""Unit tests for the shared TEXT-parsing rules (deterministic, no DB).

Why these matter: the check engine (2b) reuses `parses_as`, so a wrong rule
here silently changes what `type.logical` catches — these fixture values are
the round-trip CATALOG the engine will see.
"""

from __future__ import annotations

from typing import Final, cast

import pytest

from dqs.contracts.parsing import (
    INFERENCE_THRESHOLD,
    LogicalType,
    infer_logical_type,
    parse_rate,
    parses_as,
)

INT: Final = LogicalType.INTEGER
NUM: Final = LogicalType.NUMERIC
TS: Final = LogicalType.TIMESTAMP
DATE: Final = LogicalType.DATE
BOOL: Final = LogicalType.BOOLEAN
STR: Final = LogicalType.STRING


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", True),
        ("+7", True),
        ("-3002", True),
        ("007", True),
        ("7.5", False),
        ("1e3", False),
        (" 7", True),  # trim is deliberate: stray whitespace is costume noise,
        ("7 ", True),  # not a type drift (the same trim applies at check time)
        ("", False),
    ],
)
def test_integer_parsing(value: str, expected: bool) -> None:
    assert parses_as(value, INT) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7.5", True),
        ("7", True),
        ("-0.5", True),
        ("5699.99", True),
        ("nan", False),  # floats must be finite to count as parsed
        ("inf", False),
        ("2026-01-15", False),
        ("", False),
    ],
)
def test_numeric_parsing(value: str, expected: bool) -> None:
    assert parses_as(value, NUM) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-15T10:00:00", True),
        ("2026-01-15T10:00:00+05:30", True),
        ("2026-01-15T10:00:00Z", True),
        ("2026-01-15 10:00:00", True),  # loader writes isoformat with space
        ("2026-01-15", False),  # a pure date is not an instant
        ("15-01-2026", False),
        ("2026-01-15T99:00:00", False),
    ],
)
def test_timestamp_parsing(value: str, expected: bool) -> None:
    assert parses_as(value, TS) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-01-15", True),
        ("2026-02-29", False),  # calendar validity, not just shape
        ("2026-13-01", False),
        ("2026-01-15T10:00:00", False),
        ("15/01/2026", False),
    ],
)
def test_date_parsing(value: str, expected: bool) -> None:
    assert parses_as(value, DATE) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", True), ("TRUE", True), ("1", False), ("yes", False)],
)
def test_boolean_parsing(value: str, expected: bool) -> None:
    assert parses_as(value, BOOL) is expected


def test_string_parsing_always_true_and_none_never_parses() -> None:
    assert parses_as("any text", STR)
    for logical in (INT, NUM, TS, DATE, BOOL, STR):
        assert parses_as(None, logical) is False


def test_parse_rate_counts_only_non_null_values() -> None:
    values = ["7", None, "8", "bad"]
    assert parse_rate(cast("list[str | None]", values), INT) == (2 / 3, 3)
    assert parse_rate([], INT) == (1.0, 0)  # all-empty column never trips type check


def test_infer_logical_type_prefers_most_specific() -> None:
    assert infer_logical_type(["1", "2", "3"]) is INT
    assert infer_logical_type(["1.5", "2.5"]) is NUM
    assert infer_logical_type(["2026-01-15T10:00:00Z"]) is TS
    assert infer_logical_type(["2026-01-15"]) is DATE
    assert infer_logical_type(["state", "other"]) is STR
    assert infer_logical_type([None, None]) is STR  # nothing parseable


def test_infer_logical_type_threshold_used_for_noise() -> None:
    # 99% of 100 values are integers, 1 value garbage → still INTEGER
    mostly = ["42"] * 99 + ["not-a-number"]
    assert infer_logical_type(cast("list[str | None]", mostly)) is INT
    assert INFERENCE_THRESHOLD == 0.99
    # 90% integers → nothing clears the threshold → STRING fallback
    noisy = ["42"] * 90 + ["garbage"] * 10
    assert infer_logical_type(cast("list[str | None]", noisy)) is STR
