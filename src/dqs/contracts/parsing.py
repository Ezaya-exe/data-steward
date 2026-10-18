"""TEXT value parsing rules shared by the profiler and the check engine.

Why a separate module: raw contracts are profiled from *loaded DB rows* (all
business columns are TEXT, loader-owned), never from the simulator's frames.
Types are therefore inferred from strings, and the *same* rules must decide
"does this value parse as the contract type?" at check time (§7.3
`type.logical`), or detection and generation drift apart silently.

Determinism: pure functions over the value text; no timestamps, no RNG, no
environment. Format expectations are deliberately narrow (strict ISO forms)
so that a real upstream format change is detected instead of silently parsed.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable

from dqs.models.enums import LogicalType

# Inference walks this order and takes the FIRST type >=99% of non-null values
# parse as — most specific first, so "47" is an integer before it is a numeric,
# and "2026-01-15" is a date before it is a string. `STRING` is the fallback
# that everything parses as, so the loop always answers for clean data.
INFERENCE_ORDER: tuple[LogicalType, ...] = (
    LogicalType.INTEGER,
    LogicalType.NUMERIC,
    LogicalType.TIMESTAMP,
    LogicalType.DATE,
    LogicalType.BOOLEAN,
)

_INT_RE: re.Pattern[str] = re.compile(r"^[+-]?\d+$")
_DATE_RE: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# strict lowercase tokens; "t"/"f"/"1"/"0" would make booleans steal integer
# columns if accepted — inference prefers integer for those anyway, but a
# mixed column would then be inferred least-specific for the wrong reason.
_BOOLEAN_TOKENS: frozenset[str] = frozenset({"true", "false"})
INFERENCE_THRESHOLD: float = 0.99


def parses_as(raw: str | None, logical: LogicalType) -> bool:
    """Would `raw` (raw column text) parse as the contract logical type?

    None counts as *not parsing* (nulls are handled by null-rate checks, never
    by type checks). Booleans are matched case-insensitively on the token pair
    the allows only true/false; numeric values must be finite (no NaN/Inf —
    those are exactly what range checks should catch, not swallow).
    """
    if raw is None:
        return False
    text = raw.strip()
    match logical:
        case LogicalType.STRING:
            return True
        case LogicalType.INTEGER:
            return _INT_RE.match(text) is not None
        case LogicalType.NUMERIC:
            try:
                value = float(text)
            except ValueError:
                return False
            return math.isfinite(value)
        case LogicalType.TIMESTAMP:
            if _DATE_RE.match(text):  # pure date, not an instant
                return False
            try:
                dt.datetime.fromisoformat(text)  # 3.11+: accepts trailing 'Z'
            except ValueError:
                return False
            return True
        case LogicalType.DATE:
            if not _DATE_RE.match(text):
                return False
            try:
                dt.date.fromisoformat(text)
            except ValueError:
                return False
            return True
        case LogicalType.BOOLEAN:
            return text.lower() in _BOOLEAN_TOKENS
    return False  # unreachable with the closed enum; mypy completeness


def parse_rate(values: Iterable[str | None], logical: LogicalType) -> tuple[float, int]:
    """(rate, non_null_count) of `values` parsing as `logical`.

    Rate is over NON-NULL values only (nulls are a null-rate concern); 0
    non-null values give rate 1.0 so an all-null column never trips `type.logical`
    (it trips `nulls.rate` instead).
    """
    non_null = 0
    parsed = 0
    for value in values:
        if value is None:
            continue
        non_null += 1
        if parses_as(value, logical):
            parsed += 1
    if non_null == 0:
        return 1.0, 0
    return parsed / non_null, non_null


def infer_logical_type(values: Iterable[str | None]) -> LogicalType:
    """Most specific type >=99% of non-null values parse as (§7.2).

    Walks `INFERENCE_ORDER` (most specific first) and returns the first type
    that clears the threshold; STRING is the fallback that always qualifies.
    """
    snapshot = list(values)
    non_null = sum(1 for value in snapshot if value is not None)
    if non_null == 0:  # nothing to reason about — refuse to invent an integer
        return LogicalType.STRING
    for logical in INFERENCE_ORDER:
        rate, _ = parse_rate(snapshot, logical)
        if rate >= INFERENCE_THRESHOLD:
            return logical
    return LogicalType.STRING
