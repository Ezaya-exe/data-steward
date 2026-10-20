"""Profiling tools: batch profile, profile diff, and evidence rows.

Why: the investigator reasons over *facts* produced by the deterministic
profiler (Phase 2a), not over ad-hoc SQL it might invent. These wrappers reuse
`contracts.profiler.profile_raw_table` and the shared parser (`parse_rate`,
`parses_as`) so a tool answer cannot disagree with the check engine or the
contract generator.

The compact JSON shape here is deliberately smaller than the internal
`TableProfile`: the model needs rates and moments, not the whole baseline
history. Output is wrapped/truncated by `base.data_tool`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, text

from dqs.contracts.parsing import parses_as
from dqs.contracts.profiler import (
    BatchRef,
    BatchTableProfile,
    ColumnBatchStats,
    profile_raw_table,
)
from dqs.contracts.store import load_approved
from dqs.errors import ToolError
from dqs.models.contract import ColumnSpec
from dqs.tools.base import (
    SYSTEM_COLUMNS,
    ToolContext,
    data_tool,
    validate_identifier,
    validate_raw_table,
)

MAX_SAMPLE_LIMIT = 10
MAX_SCAN_ROWS = 200
MAX_SAMPLE_COLUMNS = 20


def batch_ref(engine: Engine, batch_id: str) -> BatchRef:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT batch_id, business_date FROM dq.batches WHERE batch_id = :b"),
            {"b": batch_id},
        ).fetchone()
    if row is None:
        raise ToolError(f"batch {batch_id!r} does not exist in dq.batches")
    return BatchRef(batch_id=str(row[0]), business_date=row[1])


def _batch_profile(engine: Engine, table: str, batch_id: str) -> BatchTableProfile:
    _schema, name = validate_raw_table(table)
    ref = batch_ref(engine, batch_id)
    profile = profile_raw_table(engine, name, [ref])
    if not profile.batches:
        raise ToolError(f"no profile could be built for {table} / {batch_id}")
    return profile.batches[0]


def _null_rate(stats: ColumnBatchStats) -> float:
    total = stats.non_null + stats.null_count
    return round(stats.null_count / total, 6) if total else 0.0


def _compact_column(stats: ColumnBatchStats) -> dict[str, Any]:
    return {
        "null_rate": _null_rate(stats),
        "non_null": stats.non_null,
        "distinct": stats.distinct,
        "unique": stats.unique,
        "min": stats.min_value,
        "max": stats.max_value,
        "mean": stats.mean_value,
        "orphan_rate": stats.orphan_rate,
        "parse_rates": stats.parse_rates,
    }


def summarize_batch_profile(profile: BatchTableProfile) -> dict[str, Any]:
    """The compact, model-facing view of one `BatchTableProfile` (pure)."""
    return {
        "table": profile.table,
        "batch_id": profile.batch_id,
        "business_date": profile.business_date.isoformat() if profile.business_date else None,
        "present": profile.present,
        "row_count": profile.row_count,
        "header": profile.header,
        "columns": {
            name: _compact_column(stats) for name, stats in sorted(profile.column_stats.items())
        },
    }


@data_tool
def get_batch_profile(ctx: ToolContext, table: str, batch_id: str) -> dict[str, Any]:
    """Per-column null/parse/range facts for one raw table in one batch."""
    return summarize_batch_profile(_batch_profile(ctx.engine, table, batch_id))


@data_tool
def compare_profiles(ctx: ToolContext, table: str, batch_a: str, batch_b: str) -> dict[str, Any]:
    """Diff two batches of the same table (clean baseline vs the bad batch)."""
    left = _batch_profile(ctx.engine, table, batch_a)
    right = _batch_profile(ctx.engine, table, batch_b)
    left_columns = set(left.column_stats)
    right_columns = set(right.column_stats)
    columns: dict[str, Any] = {}
    for name in sorted(left_columns | right_columns):
        left_stats = left.column_stats.get(name)
        right_stats = right.column_stats.get(name)
        left_mean = left_stats.mean_value if left_stats else None
        right_mean = right_stats.mean_value if right_stats else None
        columns[name] = {
            "null_rate_a": _null_rate(left_stats) if left_stats else None,
            "null_rate_b": _null_rate(right_stats) if right_stats else None,
            "null_rate_delta": (
                round(_null_rate(right_stats) - _null_rate(left_stats), 6)
                if left_stats and right_stats
                else None
            ),
            "mean_a": left_mean,
            "mean_b": right_mean,
            "mean_delta": (
                round(right_mean - left_mean, 6)
                if left_mean is not None and right_mean is not None
                else None
            ),
            "distinct_a": left_stats.distinct if left_stats else None,
            "distinct_b": right_stats.distinct if right_stats else None,
        }
    return {
        "table": table,
        "batch_a": batch_a,
        "batch_b": batch_b,
        "present_a": left.present,
        "present_b": right.present,
        "row_count_a": left.row_count,
        "row_count_b": right.row_count,
        "row_count_delta": right.row_count - left.row_count,
        "missing_in_a": sorted(right_columns - left_columns),
        "missing_in_b": sorted(left_columns - right_columns),
        "columns": columns,
    }


def batch_header(engine: Engine, schema: str, table: str, batch_id: str) -> list[str] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT columns FROM dq.batch_tables WHERE batch_id = :b AND table_name = :t"),
            {"b": batch_id, "t": table},
        ).fetchone()
    if row is None:
        return None
    observed = row[0]
    return [str(item) for item in observed] if observed is not None else []


def batch_values(
    engine: Engine, table: str, batch_id: str, columns: list[str]
) -> dict[str, list[Any]]:
    """Raw TEXT values per requested column for one batch (deterministic order).

    Used by `find_rename_candidates` for value-overlap evidence. Identifiers
    are validated before interpolation, and the batch id stays a parameter.
    """
    schema, name = validate_raw_table(table)
    safe_columns = [validate_identifier(column, kind="column") for column in columns]
    if not safe_columns:
        return {}
    quoted = ", ".join(f'"{column}"' for column in safe_columns)
    query = text(f'SELECT {quoted} FROM "{schema}"."{name}" WHERE _batch_id = :b ORDER BY ctid')
    with engine.connect() as conn:
        rows = conn.execute(query, {"b": batch_id}).mappings().all()
    return {column: [row[column] for row in rows] for column in safe_columns}


def _row_violations(row: dict[str, Any], specs: dict[str, ColumnSpec]) -> list[str]:
    """Best-effort row-level evidence check (nulls, parse, allowed, range)."""
    bad: list[str] = []
    for name, spec in specs.items():
        if name not in row:
            continue
        value = row[name]
        if value is None:
            if not spec.nullable:
                bad.append(name)
            continue
        text_value = str(value)
        if not parses_as(text_value, spec.logical_type):
            bad.append(name)
            continue
        if spec.allowed_values is not None and text_value not in spec.allowed_values:
            bad.append(name)
            continue
        if spec.range is not None:
            try:
                number = float(text_value)
            except ValueError:
                bad.append(name)
                continue
            if not spec.range.min <= number <= spec.range.max:
                bad.append(name)
    return bad


@data_tool
def sample_rows(
    ctx: ToolContext,
    table: str,
    batch_id: str,
    column: str | None = None,
    only_violating: bool = False,
    limit: int = MAX_SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Return up to `limit` (≤10) evidence rows; optionally only contract breaches.

    `only_violating` is a best-effort filter using the approved contract and the
    shared parser. It never replaces the check engine; it only makes it easier
    for the model to see *which* values look wrong.
    """
    _schema, name = validate_raw_table(table)
    if column is not None:
        validate_identifier(column, kind="column")
    row_limit = max(1, min(int(limit), MAX_SAMPLE_LIMIT))
    header = batch_header(ctx.engine, _schema, name, batch_id)
    qualified = f"{_schema}.{name}"
    if header is None:
        return {
            "table": qualified,
            "batch_id": batch_id,
            "present": False,
            "rows": [],
            "row_count": 0,
        }

    selected = (
        [column] if column is not None else [item for item in header if item not in SYSTEM_COLUMNS]
    )
    if not selected:
        raise ToolError(f"table {qualified} has no business columns to sample")
    selected = selected[:MAX_SAMPLE_COLUMNS]
    quoted = ", ".join(f'"{validate_identifier(item, kind="column")}"' for item in selected)
    scan_limit = MAX_SCAN_ROWS if only_violating else row_limit
    query = text(f'SELECT {quoted} FROM "{_schema}"."{name}" WHERE _batch_id = :b LIMIT :scan')
    with ctx.engine.connect() as conn:
        rows = conn.execute(query, {"b": batch_id, "scan": scan_limit}).mappings().all()

    payload_rows = [{key: row[key] for key in selected} for row in rows]
    if not only_violating:
        return {
            "table": qualified,
            "batch_id": batch_id,
            "present": True,
            "columns": selected,
            "rows": payload_rows[:row_limit],
            "row_count": min(len(payload_rows), row_limit),
        }

    contracts = load_approved(ctx.contracts_dir)
    contract = contracts.get(qualified)
    if contract is None:
        raise ToolError(f"no approved contract for {qualified}; only_violating needs one")
    filtered: list[dict[str, Any]] = []
    for row in payload_rows:
        bad = _row_violations(row, contract.columns)
        if bad:
            enriched = dict(row)
            enriched["_violations"] = bad
            filtered.append(enriched)
    return {
        "table": qualified,
        "batch_id": batch_id,
        "present": True,
        "columns": selected,
        "rows": filtered[:row_limit],
        "row_count": min(len(filtered), row_limit),
        "scanned": len(payload_rows),
        "violating": len(filtered),
    }
