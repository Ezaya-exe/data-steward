"""Deterministic pass criteria for a sandboxed fix (docs/ARCHITECTURE.md §11).

Why this is separate from the sandbox runner: a build that exits 0 is not a
correct fix. These functions answer the four questions that make verification
credible, each with SQL against the sandbox only:

1. did dbt build and its tests pass? (`verify.sandbox` owns the build);
2. does every changed staging table still satisfy its **published** contract,
   using only schema/type/null/unique/allowed-values rules? Volume, freshness
   and range rules are excluded because a contract is a one-snapshot profile,
   not a per-batch time series;
3. were rows preserved (≥ 95% of the expected raw rows)? This is what blocks
   "fix by deleting the offending rows";
4. did the category-specific check actually clear (nulls gone, cast/range
   restored, primary key unique)?

All identifiers come from a validated plan, a validated manifest-derived model
name, or a contract, and are re-checked against the identifier regex before
interpolation. Nothing here calls an LLM.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs.checks.base import CheckContext
from dqs.checks.nulls import NullRateCheck
from dqs.checks.schema import MissingColumnCheck, UnexpectedColumnCheck
from dqs.checks.unique import UniquePkCheck
from dqs.contracts import profiler
from dqs.contracts.profiler import IDENTIFIER_RE
from dqs.errors import CheckError, DatabaseError
from dqs.models.contract import Contract
from dqs.models.enums import Layer, LogicalType, RootCauseCategory
from dqs.models.fix import CriterionResult, FixPlan

ROW_PRESERVATION_RATIO: Final = 0.95
IGNORED_COLUMNS: Final = frozenset(profiler.SYSTEM_COLUMNS)
SCHEMA_BY_LAYER: Final[Mapping[Layer, str]] = {
    Layer.STAGING: "staging",
    Layer.MARTS: "marts",
}


def sandbox_schema(base: str, layer: Layer) -> str:
    """The physical schema the project macro creates for a sandbox layer."""
    return f"{base}_{SCHEMA_BY_LAYER[layer]}"


def _identified(name: str) -> str:
    if IDENTIFIER_RE.match(name) is None:
        raise DatabaseError(f"identifier {name!r} is not permitted in verification SQL")
    return name


def _model_ref(schema: str, table: str) -> str:
    return f'"{_identified(schema)}"."{_identified(table)}"'


def _scalar(engine: Engine, sql: str, params: Mapping[str, object] | None = None) -> Any:
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), dict(params or {})).scalar_one()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"sandbox query failed: {exc}") from exc


def _rows(engine: Engine, sql: str, params: Mapping[str, object] | None = None) -> list[Any]:
    try:
        with engine.connect() as conn:
            return list(conn.execute(text(sql), dict(params or {})).fetchall())
    except SQLAlchemyError as exc:
        raise DatabaseError(f"sandbox query failed: {exc}") from exc


def expected_rows(engine: Engine, plan: FixPlan, raw_contract: Contract | None) -> int:
    """Raw rows a correct fix should preserve through staging.

    For duplicates the honest denominator is the number of distinct primary
    keys in raw: a dedupe *should* remove the duplicate copies. Everything else
    must keep every raw row.
    """
    schema, _, table = plan.affected_table.partition(".")
    ref = f'"{_identified(schema)}"."{_identified(table)}"'
    if plan.category is not RootCauseCategory.DUPLICATE_RECORDS:
        return int(_scalar(engine, f"SELECT count(*) FROM {ref}"))

    keys = list(raw_contract.primary_key) if raw_contract is not None else []
    if not keys:
        keys = plan.affected_columns[:1]
    if not keys:
        raise CheckError("duplicate-row verification needs a primary key from the raw contract")
    columns = ", ".join(f'"{_identified(column)}"' for column in keys)
    return int(_scalar(engine, f"SELECT count(*) FROM (SELECT DISTINCT {columns} FROM {ref}) d"))


def _staged_row_count(engine: Engine, schema: str, table: str) -> int:
    return int(_scalar(engine, f"SELECT count(*) FROM {_model_ref(schema, table)}"))


def row_preservation(
    engine: Engine,
    plan: FixPlan,
    schema: str,
    table: str,
    raw_contract: Contract | None,
) -> CriterionResult:
    """`sandbox staging rows ≥ 95% of expected rows` (§11 step 4)."""
    try:
        expected = expected_rows(engine, plan, raw_contract)
        actual = _staged_row_count(engine, schema, table)
    except (CheckError, DatabaseError) as exc:
        return CriterionResult(
            name="row_preservation", passed=False, detail=f"could not count rows: {exc}"
        )

    ratio = (actual / expected) if expected else 1.0
    passed = actual >= ROW_PRESERVATION_RATIO * expected
    detail = (
        f"{actual} staging rows vs {expected} expected ({ratio:.1%}); "
        f"minimum is {ROW_PRESERVATION_RATIO:.0%}"
    )
    return CriterionResult(
        name="row_preservation",
        passed=passed,
        detail=detail,
        observed={"expected_rows": expected, "sandbox_rows": actual, "ratio": round(ratio, 6)},
    )


def _type_matches(expected: LogicalType, actual: LogicalType) -> bool:
    if expected is actual:
        return True
    # A numeric contract is satisfied by an integer Postgres column: the values
    # are a subset of numeric and no information is lost.
    return expected is LogicalType.NUMERIC and actual is LogicalType.INTEGER


def contract_conformance(
    engine: Engine,
    schema: str,
    table: str,
    contract: Contract,
) -> CriterionResult:
    """Published-contract conformance for one changed staging/marts table.

    Only the five allowed rule families are applied. `LogicalTypeCheck` alone
    cannot validate typed columns (it skips a column with no TEXT parse rate),
    so the contract logical type is compared with the Postgres column type
    explicitly.
    """
    try:
        profile = profiler.profile_table_state(
            engine,
            profiler.StagedTableRef(layer=contract.layer, schema_name=schema, table=table),
        )
    except (ValueError, DatabaseError, SQLAlchemyError) as exc:
        return CriterionResult(
            name="contract_conformance",
            passed=False,
            detail=f"{contract.table}: could not profile sandbox table: {exc}",
        )

    snapshot = profile.batches[0]
    ctx = CheckContext(engine=engine, contract=contract, profile=snapshot)
    violations = []
    try:
        for check in (
            MissingColumnCheck(),
            UnexpectedColumnCheck(),
            NullRateCheck(),
            UniquePkCheck(),
        ):
            violations.extend(check.run(ctx))
        problems = [f"{violation.check}: {violation.message}" for violation in violations]

        observed_header = set(snapshot.header or ()) - IGNORED_COLUMNS
        for column in sorted(set(contract.columns) & observed_header):
            spec = contract.columns[column]
            aggregate = profile.aggregates.get(column)
            if aggregate is None or aggregate.pg_type is None:
                problems.append(f"type.logical: {column!r} has no Postgres type")
                continue
            actual = profiler.pg_type_to_logical(aggregate.pg_type)
            if not _type_matches(spec.logical_type, actual):
                problems.append(
                    f"type.logical: {column!r} is {aggregate.pg_type} "
                    f"({actual.value}) but the contract requires {spec.logical_type.value}"
                )
            if spec.allowed_values is not None:
                bad = _disallowed_values(engine, schema, table, column, spec.allowed_values)
                if bad:
                    problems.append(
                        f"allowed_values: {column!r} contains {sorted(bad)[:5]} "
                        f"outside {sorted(spec.allowed_values)}"
                    )
    except (DatabaseError, SQLAlchemyError) as exc:
        return CriterionResult(
            name="contract_conformance",
            passed=False,
            detail=f"{contract.table}: conformance query failed: {exc}",
        )

    return CriterionResult(
        name="contract_conformance",
        passed=not problems,
        detail="; ".join(problems)[:2000] if problems else f"{contract.table} conforms",
        observed={
            "table": contract.table,
            "rows": snapshot.row_count,
            "violations": len(problems),
        },
    )


def _disallowed_values(
    engine: Engine, schema: str, table: str, column: str, allowed: Sequence[str]
) -> list[str]:
    allowed_set = {str(value) for value in allowed}
    sql = (
        f'SELECT DISTINCT "{_identified(column)}"::text '
        f"FROM {_model_ref(schema, table)} "
        f'WHERE "{_identified(column)}" IS NOT NULL '
        f"ORDER BY 1 LIMIT 100"
    )
    return [str(row[0]) for row in _rows(engine, sql) if str(row[0]) not in allowed_set]


def _batch_column(
    engine: Engine, schema: str, table: str, column: str, batch_id: str
) -> tuple[int, int | None, int | None]:
    sql = (
        f'SELECT count(*), min("{_identified(column)}"), max("{_identified(column)}") '
        f"FROM {_model_ref(schema, table)} WHERE _batch_id = :batch"
    )
    row = _rows(engine, sql, {"batch": batch_id})[0]
    return int(row[0]), row[1], row[2]


def _nulls_in_batch(engine: Engine, schema: str, table: str, column: str, batch_id: str) -> int:
    sql = (
        f"SELECT count(*) FROM {_model_ref(schema, table)} "
        f'WHERE _batch_id = :batch AND "{_identified(column)}" IS NULL'
    )
    return int(_scalar(engine, sql, {"batch": batch_id}))


def _column_pg_type(engine: Engine, schema: str, table: str, column: str) -> str | None:
    sql = (
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = :schema AND table_name = :table AND column_name = :column"
    )
    rows = _rows(engine, sql, {"schema": schema, "table": table, "column": _identified(column)})
    return str(rows[0][0]) if rows else None


def _targeted_renamed(engine: Engine, plan: FixPlan, schema: str, table: str) -> CriterionResult:
    column = plan.affected_columns[0] if plan.affected_columns else ""
    if not column:
        return CriterionResult(
            name="targeted_check", passed=False, detail="renamed-column check needs a column"
        )
    if plan.batch_id is None:
        return CriterionResult(
            name="targeted_check", passed=False, detail="renamed-column check needs batch_id"
        )
    rows, _lo, _hi = _batch_column(engine, schema, table, column, plan.batch_id)
    if rows == 0:
        return CriterionResult(
            name="targeted_check",
            passed=False,
            detail=f"the faulted batch {plan.batch_id} has no rows in the sandbox staging table",
            observed={"batch_rows": 0},
        )
    nulls = _nulls_in_batch(engine, schema, table, column, plan.batch_id)
    return CriterionResult(
        name="targeted_check",
        passed=nulls == 0,
        detail=(
            f"{nulls} NULL {column!r} values remain in {plan.batch_id}"
            if nulls
            else f"no NULL {column!r} values in {plan.batch_id}"
        ),
        observed={"batch_rows": rows, "nulls": nulls},
    )


def _targeted_type(
    engine: Engine,
    plan: FixPlan,
    schema: str,
    table: str,
    raw_contract: Contract | None,
) -> CriterionResult:
    column = plan.affected_columns[0] if plan.affected_columns else ""
    if not column:
        return CriterionResult(
            name="targeted_check", passed=False, detail="type-change check needs a column"
        )
    pg_type = _column_pg_type(engine, schema, table, column)
    if pg_type is None:
        return CriterionResult(
            name="targeted_check",
            passed=False,
            detail=f"column {column!r} is missing from the sandbox staging table",
        )
    logical = profiler.pg_type_to_logical(pg_type)
    if logical not in (LogicalType.INTEGER, LogicalType.NUMERIC):
        return CriterionResult(
            name="targeted_check",
            passed=False,
            detail=f"column {column!r} is {pg_type}, not numeric after the fix",
            observed={"pg_type": pg_type},
        )
    if plan.batch_id is None:
        return CriterionResult(
            name="targeted_check", passed=False, detail="type-change check needs batch_id"
        )
    rows, low, high = _batch_column(engine, schema, table, column, plan.batch_id)
    if rows == 0:
        return CriterionResult(
            name="targeted_check",
            passed=False,
            detail=f"the faulted batch {plan.batch_id} has no rows in the sandbox staging table",
        )
    problems: list[str] = []
    spec = raw_contract.columns.get(column) if raw_contract is not None else None
    if spec is not None and spec.range is not None:
        if low is None or high is None:
            problems.append("no non-null values to compare with the raw contract range")
        else:
            if float(low) < spec.range.min or float(high) > spec.range.max:
                problems.append(
                    f"values [{low}, {high}] fall outside the raw contract range "
                    f"[{spec.range.min}, {spec.range.max}]"
                )
    return CriterionResult(
        name="targeted_check",
        passed=not problems,
        detail="; ".join(problems) if problems else f"{column!r} casts to {pg_type} within range",
        observed={"batch_rows": rows, "min": low, "max": high, "pg_type": pg_type},
    )


def _targeted_duplicates(
    engine: Engine,
    plan: FixPlan,
    schema: str,
    table: str,
    staging_contract: Contract | None,
    raw_contract: Contract | None,
) -> CriterionResult:
    keys = list(staging_contract.primary_key) if staging_contract is not None else []
    if not keys and raw_contract is not None:
        keys = list(raw_contract.primary_key)
    if not keys:
        keys = plan.affected_columns[:1]
    if not keys:
        return CriterionResult(
            name="targeted_check", passed=False, detail="duplicate check needs a primary key"
        )

    ref = _model_ref(schema, table)
    if len(keys) == 1:
        key = f'"{_identified(keys[0])}"'
        sql = (
            f"SELECT count(*) - count(DISTINCT {key}), "
            f"count(*) FILTER (WHERE {key} IS NULL) FROM {ref}"
        )
    else:
        columns = ", ".join(f'"{_identified(key)}"' for key in keys)
        null_expr = " OR ".join(f'"{_identified(key)}" IS NULL' for key in keys)
        sql = (
            f"SELECT (SELECT count(*) FROM {ref}) - "
            f"(SELECT count(*) FROM (SELECT DISTINCT {columns} FROM {ref}) d), "
            f"count(*) FILTER (WHERE {null_expr}) FROM {ref}"
        )
    row = _rows(engine, sql)[0]
    duplicates, null_keys = int(row[0]), int(row[1])
    passed = duplicates == 0 and null_keys == 0
    return CriterionResult(
        name="targeted_check",
        passed=passed,
        detail=(
            f"primary key {keys} has {duplicates} duplicate row(s) and {null_keys} NULL key(s)"
        ),
        observed={"duplicates": duplicates, "null_keys": null_keys, "primary_key": keys},
    )


def targeted_check(
    engine: Engine,
    plan: FixPlan,
    schema: str,
    table: str,
    staging_contract: Contract | None,
    raw_contract: Contract | None,
) -> CriterionResult:
    """The category-specific check the original violation demanded (§11 step 4)."""
    try:
        if plan.category is RootCauseCategory.SOURCE_COLUMN_RENAMED:
            return _targeted_renamed(engine, plan, schema, table)
        if plan.category is RootCauseCategory.SOURCE_TYPE_CHANGED:
            return _targeted_type(engine, plan, schema, table, raw_contract)
        if plan.category is RootCauseCategory.DUPLICATE_RECORDS:
            return _targeted_duplicates(engine, plan, schema, table, staging_contract, raw_contract)
    except (CheckError, DatabaseError) as exc:
        return CriterionResult(name="targeted_check", passed=False, detail=str(exc))
    return CriterionResult(
        name="targeted_check",
        passed=False,
        detail=f"no targeted check is defined for {plan.category.value}",
    )


def evaluate(
    engine: Engine,
    plan: FixPlan,
    base_schema: str,
    model_names: Sequence[str],
    contracts: Mapping[str, Contract],
) -> list[CriterionResult]:
    """Run contract, row-preservation and targeted criteria for the edited models."""
    criteria: list[CriterionResult] = []
    staging_schema = sandbox_schema(base_schema, Layer.STAGING)
    staging_contracts: dict[str, Contract | None] = {}
    for model in model_names:
        contract = contracts.get(f"staging.{model}")
        staging_contracts[model] = contract
        if contract is None:
            criteria.append(
                CriterionResult(
                    name="contract_conformance",
                    passed=False,
                    detail=f"no approved staging contract for {model!r}",
                )
            )
        else:
            criteria.append(contract_conformance(engine, staging_schema, model, contract))

        criteria.append(
            row_preservation(
                engine,
                plan,
                staging_schema,
                model,
                contracts.get(plan.affected_table),
            )
        )

    target_model = _target_model(model_names, staging_contracts, plan)
    if target_model is not None:
        criteria.append(
            targeted_check(
                engine,
                plan,
                staging_schema,
                target_model,
                staging_contracts.get(target_model),
                contracts.get(plan.affected_table),
            )
        )
    else:
        criteria.append(
            CriterionResult(
                name="targeted_check",
                passed=False,
                detail="no changed staging model is available for the targeted check",
            )
        )
    return criteria


def _target_model(
    model_names: Sequence[str],
    contracts: Mapping[str, Contract | None],
    plan: FixPlan,
) -> str | None:
    """Prefer the model whose contract declares the affected column."""
    wanted = set(plan.affected_columns)
    for model in model_names:
        contract = contracts.get(model)
        if contract is not None and wanted & set(contract.columns):
            return model
    return model_names[0] if model_names else None
