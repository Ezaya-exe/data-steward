"""Benchmark answer-key oracle: compare a patched sandbox with clean data.

Why this exists (Phase 5b, moved here in Phase 8a): the verifier can pass a fix
that clears the check and preserves rows but still writes wrong values (for
example casting every amount to the same number). The benchmark's ground truth
is the generator, which is a pure function of ``(seed, date)``; this module
rebuilds the patched staging model with the real dbt project and compares its
fault-batch rows with that clean frame.

This is deliberately **not** importable by the agent/verification/publishing
code: the fixer and verifier must not be able to see the answer key (AGENTS.md
§7.9: no special-casing fault names). `tests/unit/test_bench_isolation.py`
asserts that nothing under ``agents/``, ``verify/``, ``workflow/``, ``publish/``,
``tools/`` or ``checks/`` imports this package.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Final

import pandas as pd
from sqlalchemy import Engine, text

from dqs.config import Settings
from dqs.lineage import manifest as manifest_module
from dqs.lineage.graph import LineageGraph
from dqs.models.enums import Layer
from dqs.models.fix import FixPlan
from dqs.sim import generator
from dqs.verify import criteria as criteria_module
from dqs.verify import patch, sandbox

PROJECT_DIR: Final = patch.PROJECT_DIR
MAX_REPORTED: Final = 20
IDENTIFIER_RE: Final = r"^[a-z_][a-z0-9_]*$"


@dataclass(frozen=True)
class OracleResult:
    """The oracle's verdict, with the evidence behind a failure."""

    passed: bool
    table: str
    build_passed: bool = False
    rows_expected: int = 0
    rows_actual: int = 0
    missing_keys: list[int] = field(default_factory=list)
    unexpected_keys: list[int] = field(default_factory=list)
    duplicate_keys: list[int] = field(default_factory=list)
    mismatched_cells: list[str] = field(default_factory=list)
    detail: str = ""


def clean_frame(raw_table: str, fault_date: date, seed: int) -> pd.DataFrame:
    """The generator's clean frame for the fault date (the answer key)."""
    tables = generator.tables_for_date(fault_date, seed)
    return tables[raw_table].copy()


def compare_frames(
    expected: pd.DataFrame,
    actual: pd.DataFrame,
    *,
    key: str,
    columns: list[str],
) -> OracleResult:
    """Compare two frames row-for-row by business key, normalizing DB types."""
    if key not in expected.columns or key not in actual.columns:
        return OracleResult(
            passed=False,
            table="",
            rows_expected=len(expected),
            rows_actual=len(actual),
            detail=f"key column {key!r} is missing from one frame",
        )
    missing_columns = [column for column in columns if column not in actual.columns]
    if missing_columns:
        return OracleResult(
            passed=False,
            table="",
            rows_expected=len(expected),
            rows_actual=len(actual),
            detail=f"patched output is missing column(s): {', '.join(missing_columns)}",
        )

    expected_clean = _normalize(expected, columns, key)
    actual_clean = _normalize(actual, columns, key)
    duplicate_keys = sorted(
        int(value) for value in actual_clean.loc[actual_clean.duplicated(subset=[key]), key]
    )
    # A duplicate key means the fix did not deduplicate; compare distinct rows
    # anyway so the report can also show value errors.
    actual_unique = actual_clean.drop_duplicates(subset=[key], keep="first")
    expected_unique = expected_clean.drop_duplicates(subset=[key], keep="first")

    expected_keys = set(expected_unique[key].tolist())
    actual_keys = set(actual_unique[key].tolist())
    missing_keys = sorted(expected_keys - actual_keys)
    unexpected_keys = sorted(actual_keys - expected_keys)

    common = expected_unique.merge(
        actual_unique, on=key, how="inner", suffixes=("_expected", "_actual")
    )
    mismatched_cells: list[str] = []
    for column in columns:
        if column == key:
            continue
        left = common[f"{column}_expected"]
        right = common[f"{column}_actual"]
        different = ~_values_equal(left, right)
        for row_key in common.loc[different, key].head(MAX_REPORTED):
            mismatched_cells.append(f"{key}={int(row_key)} column={column}")

    passed = (
        not missing_keys and not unexpected_keys and not duplicate_keys and not mismatched_cells
    )
    detail = "patched rows match the clean generated frame"
    if not passed:
        detail = (
            f"missing={len(missing_keys)} unexpected={len(unexpected_keys)} "
            f"duplicates={len(duplicate_keys)} mismatched_cells={len(mismatched_cells)}"
        )
    return OracleResult(
        passed=passed,
        table="",
        build_passed=True,
        rows_expected=len(expected_clean),
        rows_actual=len(actual_clean),
        missing_keys=missing_keys[:MAX_REPORTED],
        unexpected_keys=unexpected_keys[:MAX_REPORTED],
        duplicate_keys=duplicate_keys[:MAX_REPORTED],
        mismatched_cells=mismatched_cells[:MAX_REPORTED],
        detail=detail,
    )


def compare_patched_staging(
    settings: Settings,
    plan: FixPlan,
    *,
    engine: Engine,
    fault_date: date,
    seed: int,
    raw_table: str = "orders",
    staging_table: str = "stg_orders",
    key: str = "order_id",
    timeout_s: int | None = None,
    project_dir: Path = PROJECT_DIR,
) -> OracleResult:
    """Build the patched staging model in a sandbox and compare it with clean data.

    Uses exactly the production allowlist/apply/build helpers, so the compared
    table is the one verification would have produced. The sandbox schemas are
    dropped in ``finally``; a failing build is reported, never skipped.
    """
    if not _is_identifier(staging_table):
        raise ValueError(f"unsafe staging table name {staging_table!r}")
    if not _is_identifier(raw_table):
        raise ValueError(f"unsafe raw table name {raw_table!r}")

    base_schema = patch.sandbox_schema_name(plan)
    if not patch.is_sandbox_schema(base_schema):
        raise ValueError(f"invalid sandbox schema name {base_schema!r}")
    timeout = timeout_s or settings.fix_verify_timeout_seconds
    columns = generator.columns_of_table(raw_table)
    expected = clean_frame(raw_table, fault_date, seed)
    batch_id = f"batch_{fault_date:%Y%m%d}"
    build_passed = False

    try:
        canonical = patch.validate_edit_paths(plan.edits, project_dir)
        loaded = manifest_module.load_manifest(project_dir, regenerate_when_stale=False)
        graph = LineageGraph.from_manifest(loaded, project_dir=project_dir)
        resolved = patch.validate_relevance(canonical, graph, plan.affected_table)
        models = patch.model_names(list(resolved.values()))
        sandbox.drop_sandbox_schemas(engine, base_schema)

        with tempfile.TemporaryDirectory(prefix="dqs_oracle_") as temporary:
            work = Path(temporary) / "dbt_project"
            patch.copy_project(project_dir, work)
            patch.apply_edits(plan.edits, work)
            outcome = sandbox.run_dbt_build(
                work,
                models,
                base_schema,
                settings,
                timeout_s=timeout,
            )
            build_passed = outcome.passed
            if not outcome.passed:
                return OracleResult(
                    passed=False,
                    table=staging_table,
                    build_passed=False,
                    rows_expected=len(expected),
                    detail=f"patched sandbox build failed: {outcome.detail}",
                )

        staging_schema = criteria_module.sandbox_schema(base_schema, Layer.STAGING)
        actual = _read_staging_batch(engine, staging_schema, staging_table, batch_id, columns)
        result = compare_frames(expected, actual, key=key, columns=columns)
        return OracleResult(
            passed=result.passed,
            table=staging_table,
            build_passed=build_passed,
            rows_expected=result.rows_expected,
            rows_actual=result.rows_actual,
            missing_keys=result.missing_keys,
            unexpected_keys=result.unexpected_keys,
            duplicate_keys=result.duplicate_keys,
            mismatched_cells=result.mismatched_cells,
            detail=result.detail,
        )
    finally:
        sandbox.drop_sandbox_schemas(engine, base_schema, raise_on_error=False)


def _read_staging_batch(
    engine: Engine,
    schema: str,
    table: str,
    batch_id: str,
    columns: list[str],
) -> pd.DataFrame:
    if not patch.is_sandbox_schema(schema):
        raise ValueError(f"refusing to read a non-sandbox schema {schema!r}")
    if not _is_identifier(table) or any(not _is_identifier(column) for column in columns):
        raise ValueError("unsafe staging identifier")
    selected = ", ".join(f'"{column}"' for column in columns)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f'SELECT {selected} FROM "{schema}"."{table}" WHERE _batch_id = :batch ORDER BY 1'
            ),
            {"batch": batch_id},
        ).mappings()
        return pd.DataFrame([dict(row) for row in rows], columns=columns)


def _normalize(frame: pd.DataFrame, columns: list[str], key: str) -> pd.DataFrame:
    normalized = frame.loc[:, columns].copy()
    for column in columns:
        series = normalized[column]
        if column == key:
            normalized[column] = pd.to_numeric(series, errors="coerce").astype("Int64")
        elif column.endswith("_ts") or column.endswith("_at") or column == "signup_date":
            normalized[column] = pd.to_datetime(series, utc=True, errors="coerce")
        else:
            # DB drivers return Decimal/None for numeric columns, while the
            # generator returns floats; try numeric first, fall back to text.
            numeric = pd.to_numeric(series, errors="coerce")
            non_null = series.notna()
            if non_null.any() and numeric[non_null].notna().all():
                normalized[column] = numeric.astype(float).round(6)
            else:
                normalized[column] = series.astype("string")
    return normalized


def _values_equal(left: pd.Series, right: pd.Series) -> pd.Series:
    both_null = left.isna() & right.isna()
    equal = left.eq(right)
    return both_null | equal


def _is_identifier(value: str) -> bool:
    import re

    return re.fullmatch(IDENTIFIER_RE, value) is not None


__all__ = [
    "OracleResult",
    "clean_frame",
    "compare_frames",
    "compare_patched_staging",
]
