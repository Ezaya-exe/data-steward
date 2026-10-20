"""Batch profiling for contract generation and later fit checking.

Why here: contracts must be derived from *what actually arrived in the
warehouse*, not from what the simulator intends (docs/DECISIONS.md Phase 2a):
loaded DB rows are the only truth the check engine will later see, so the
profiler reads them back through SQL. Raw tables hold TEXT, so every value
going through pandas is parsed with `dqs.contracts.parsing` (the module the
check engine will share); staging/marts are already typed by dbt, so their
logical types come from the Postgres column types instead.

All SQL is read-only and parameterized; identifiers come from
information_schema or compile-time constants and are re-validated against the
loader's identifier regex before interpolation (defence in depth, AGENTS §6).
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from typing import Any, Final, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text

from dqs.contracts.parsing import INFERENCE_ORDER, LogicalType, infer_logical_type, parse_rate
from dqs.models.enums import Layer
from dqs.sim.faults import FK_MAP

IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_]*$")
RAW_SCHEMA: Final = "raw"
STAGED_SCHEMAS: Final[tuple[tuple[Layer, str], ...]] = (
    (Layer.STAGING, "staging"),
    (Layer.MARTS, "marts"),
)
# Loader-owned metadata columns never enter contracts (DECISIONS 23): schema
# checks ignore them, so profiling must reason over exactly the same set.
SYSTEM_COLUMNS: Final[tuple[str, ...]] = ("_batch_id", "_loaded_at")

# FK_MAP is the single source of truth for which child column may reference
# which parent (docs/DECISIONS.md Phase 2a): the profiler re-indexes it per
# raw table so contract generation and the fault catalog share one map.
# Parent tables are stored bare (schema is always `raw`); contract generation
# re-qualifies them for the `references` field.
FK_BY_TABLE: Final[dict[str, dict[str, tuple[str, str]]]] = {}
for _child, _parent in FK_MAP.items():
    _table, _column = _child.split(".", maxsplit=1)
    _parent_schema, _parent_table, _parent_column = _parent.rsplit(".", maxsplit=2)
    FK_BY_TABLE.setdefault(_table, {})[_column] = (_parent_table, _parent_column)


class BatchRef(BaseModel):
    """One record inside `dq.batches` (the raw profiling window)."""

    model_config = ConfigDict(extra="forbid")
    batch_id: str
    business_date: dt.date


class ColumnBatchStats(BaseModel):
    """One column's facts for one batch (the smallest profiling unit).

    `min_value`/`max_value`/`mean_value` are numbers from the numeric parse of
    the batch slice (raw layer: TEXT parse; staged layer: typed values). The
    generator reads them for *measure* columns exclusively — docs/DECISIONS.md
    forbids bounds on keys, FK columns, ids, and date/timestamp columns.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    non_null: int
    null_count: int
    distinct: int  # distinct non-null values within the batch
    unique: bool  # distinct == non_null within the batch
    min_value: float | None = None
    max_value: float | None = None
    mean_value: float | None = None
    orphan_rate: float | None = None  # FK columns only
    parse_rates: dict[str, float] = {}  # raw layer only: per logical type


class BatchTableProfile(BaseModel):
    """One table inside one batch (raw) or one snapshot (staging/marts).

    `present=False` encodes F9 precisely: the table never appeared in the
    batch (`dq.batch_tables` has no row) — versus the present-but-empty case
    covered by `row_count`/`header`.
    """

    model_config = ConfigDict(extra="forbid")
    table: str
    batch_id: str | None  # None for staged layers: whole-table snapshot
    business_date: dt.date | None
    present: bool
    row_count: int
    header: list[str] | None  # None only when absent from the batch
    column_stats: dict[str, ColumnBatchStats]


class ColumnAggregate(BaseModel):
    """Cross-batch baseline facts per column, consumed by the generator."""

    model_config = ConfigDict(extra="forbid")
    name: str
    logical_type: LogicalType
    distinct_count: int  # distinct non-null values over the profiled window
    non_null_count: int
    total_rows: int
    unique_in_window: bool  # distinct == non_null across the whole window
    pg_type: str | None = None  # staging/marts only: the dbt-declared type
    min_value: float | None = None
    max_value: float | None = None
    batch_means: tuple[float, ...] = ()  # one entry per profiled batch
    orphan_rates: tuple[float, ...] = ()  # one entry per profiled batch
    # populated only when distinct ≤ 12 (allowed_values candidate, §7.2);
    # sorted by value for determinism. Empty for high-cardinality columns.
    value_counts: tuple[tuple[str, int], ...] = ()


class TableProfile(BaseModel):
    """Everything the generator needs about one table's profiling window."""

    model_config = ConfigDict(extra="forbid")
    table: str
    layer: Layer
    batches: list[BatchTableProfile]
    aggregates: dict[str, ColumnAggregate]
    total_rows: int
    first_date: dt.date | None = None
    last_date: dt.date | None = None


class StagedTableRef(BaseModel):
    """A staging/marts table that exists in the warehouse right now."""

    model_config = ConfigDict(extra="forbid")
    layer: Layer
    schema_name: str
    table: str

    @property
    def qualified(self) -> str:
        """Fully-qualified table name, e.g. `staging.stg_orders`."""
        return f"{self.schema_name}.{self.table}"


def list_batches(engine: Engine) -> list[BatchRef]:
    """`dq.batches` in business-date order — the raw profiling window."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT batch_id, business_date FROM dq.batches ORDER BY business_date, batch_id")
        ).fetchall()
    return [BatchRef(batch_id=r[0], business_date=r[1]) for r in rows]


def _pg_columns(engine: Engine, schema: str, table: str) -> list[tuple[str, str]]:
    """(name, pg_type) of a table's columns, ordered as declared."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
            ),
            {"s": schema, "t": table},
        ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def pg_type_to_logical(pg_type: str) -> LogicalType:
    """One Postgres type → one contract logical type (staging/marts path)."""
    base = pg_type.replace(" ", "").strip().lower()
    if base in {"integer", "smallint", "bigint"}:
        return LogicalType.INTEGER
    if base in {"numeric", "decimal", "doubleprecision", "real", "money"}:
        return LogicalType.NUMERIC
    if base.startswith("timestamp"):
        return LogicalType.TIMESTAMP
    if base == "date":
        return LogicalType.DATE
    if base == "boolean":
        return LogicalType.BOOLEAN
    return LogicalType.STRING


def _identified_columns(engine: Engine, schema: str, table: str) -> list[tuple[str, str]]:
    """information_schema columns, each validated against the identifier regex.

    Profiling interpolates identifiers into SQL; even though the names here
    come from the catalog, matching the loader's regex keeps a hostile name
    from ever reaching a query (AGENTS §6).
    """
    columns = _pg_columns(engine, schema, table)
    for name, _pg_type in columns:
        if IDENTIFIER_RE.match(name) is None:
            raise ValueError(f"column name {name!r} is not a safe identifier")
    return columns


def _validated(name: str) -> str:
    """Return `name` only if it matches the identifier regex (SQL safety)."""
    if IDENTIFIER_RE.match(name) is None:
        raise ValueError(f"identifier {name!r} is not permitted in SQL")
    return name


def _batch_table_meta(
    engine: Engine, batch_ids: Sequence[str], table: str
) -> dict[str, tuple[int, list[str]]]:
    """(row_count, observed header) per batch for one raw table.

    Rows are filtered in Python rather than `IN :ids` expansion: one table
    holds at most one metadata row per batch, so the small result set is
    cheaper and safer than tuple binding.
    """
    metadata: dict[str, tuple[int, list[str]]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT batch_id, row_count, columns FROM dq.batch_tables WHERE table_name = :t"),
            {"t": table},
        ).fetchall()
    for row_id, row_count, observed in rows:
        if str(row_id) in set(batch_ids):
            observed_rows = cast(Any, observed)
            values = [str(item) for item in observed_rows] if observed_rows is not None else []
            metadata[str(row_id)] = (int(cast(Any, row_count)), values)
    return metadata


def fk_orphan_rate(
    engine: Engine,
    table: str,
    column: str,
    parent_table: str,
    parent_column: str,
    batch_id: str | None = None,
) -> float:
    """Share of non-null FK values without a parent (SQL anti-join).

    Nulls are never orphans (they are a null-rate concern, DECISIONS 29), so
    the denominator counts non-null values only. `batch_id=None` profiles the
    whole child table (generation coverage check stores per-batch rates when
    profiling a single batch).
    """
    for identifier in (table, column, parent_table, parent_column):
        _validated(identifier)
    child = f"{_validated(RAW_SCHEMA)}.{_validated(table)} c"
    parent = f"{_validated(RAW_SCHEMA)}.{_validated(parent_table)} p"
    batch_filter = "WHERE c._batch_id = :b" if batch_id is not None else ""
    sql = text(
        f"""
        SELECT count(col) FILTER (WHERE col IS NOT NULL)             AS non_null,
               count(col) FILTER (
                   WHERE col IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM {parent} WHERE p.{_validated(parent_column)} = col
                   )
               )                                                     AS orphans
        FROM (SELECT c.{_validated(column)} AS col FROM {child} {batch_filter}) sub
        """
    )
    params = {"b": batch_id} if batch_id is not None else {}
    with engine.connect() as conn:
        row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0.0
    non_null, orphans = int(row[0]), int(row[1])
    if non_null == 0:
        return 0.0
    return float(orphans) / float(non_null)


def _fk_columns_for_table(table: str) -> dict[str, tuple[str, str]]:
    """{child column: (parent table, parent column)} — raw layer only."""
    return dict(FK_BY_TABLE.get(table, {}))


def _load_batch_frame(
    engine: Engine, columns: Sequence[str], table: str, batch_id: str
) -> pd.DataFrame:
    """One batch slice of a raw table (TEXT business columns only)."""
    quoted = ", ".join(f'"{_validated(ca)}"' for ca in columns)
    query = text(f'SELECT {quoted} FROM "{RAW_SCHEMA}"."{_validated(table)}" WHERE _batch_id = :b')
    with engine.connect() as conn:
        return pd.read_sql(query, conn, params={"b": batch_id})


def _numeric_series_for_text(values: pd.Series) -> pd.Series:
    """Finite numeric parse of a TEXT column slice (range/mean facts)."""
    numbers: list[float] = []
    for value in values:
        if value is None or pd.isna(value):
            continue
        try:
            number = float(str(value))
        except ValueError:
            continue
        if math.isfinite(number):
            numbers.append(number)
    return pd.Series(numbers, dtype="float64")


def _numeric_series_for_typed(values: pd.Series) -> pd.Series:
    """Typed column slice as floats for min/max/mean facts."""
    numbers = [float(value) for value in values if value is not None and not pd.isna(value)]
    return pd.Series(numbers, dtype="float64")


def _scalar(series: pd.Series, op: str) -> float | None:
    if series.empty:
        return None
    return float(getattr(series, op)())


def _text_view(values: pd.Series) -> list[str | None]:
    """Values as `str | None` for the shared parser (raw layer only)."""
    return [None if value is None or pd.isna(value) else str(value) for value in values]


ALLOWED_VALUES_MAX_DISTINCT: Final = 12  # §7.2: distinct ≤ 12 → allowed_values candidate


def _low_cardinality_counts(values: pd.Series) -> tuple[tuple[str, int], ...]:
    """Distinct value counts, kept only when they fit the allowed_values rule."""
    counts = values.dropna().value_counts()
    if len(counts) > ALLOWED_VALUES_MAX_DISTINCT:
        return ()
    return tuple((str(name), int(count)) for name, count in sorted(counts.items()))


def _column_batch_stats(
    name: str,
    values: pd.Series,
    *,
    raw_text: bool,
    numeric_facts: bool,
    orphan_rate: float | None,
) -> ColumnBatchStats:
    """Facts over one column's batch slice — pure, in-memory, deterministic.

    `raw_text=False` (staged layer) skips TEXT parse rates: the dbt models
    already declare the Postgres type, which `ColumnAggregate` records.
    `numeric_facts` gates min/max/mean — meaningless for keys, FK columns,
    ids, and date/timestamp columns (editorial rule: measures only).
    """
    non_null = int(values.notna().sum())
    distinct = int(values.nunique(dropna=True))
    numeric: pd.Series = pd.Series(dtype="float64")
    if numeric_facts:
        numeric = (
            _numeric_series_for_text(values) if raw_text else _numeric_series_for_typed(values)
        )
    parse_rates: dict[str, float] = {}
    if raw_text and non_null > 0:
        view = _text_view(values)
        for logical in INFERENCE_ORDER:
            rate, _ = parse_rate(view, logical)
            parse_rates[logical.value] = rate
    return ColumnBatchStats(
        name=name,
        non_null=non_null,
        null_count=len(values) - non_null,
        distinct=distinct,
        unique=distinct == non_null,
        min_value=_scalar(numeric, "min"),
        max_value=_scalar(numeric, "max"),
        mean_value=_scalar(numeric, "mean"),
        orphan_rate=orphan_rate,
        parse_rates=parse_rates,
    )


def profile_raw_table(engine: Engine, table: str, batches: Sequence[BatchRef]) -> TableProfile:
    """Per-batch profile of one raw table over the given `dq.batches` window."""
    table = _validated(table)
    all_columns = _identified_columns(engine, RAW_SCHEMA, table)
    business_columns = [name for name, _pg in all_columns if name not in SYSTEM_COLUMNS]
    metadata = _batch_table_meta(engine, [ref.batch_id for ref in batches], table)
    fk_columns = _fk_columns_for_table(table)

    batch_profiles: list[BatchTableProfile] = []
    column_values: dict[str, list[str | None]] = {name: [] for name in business_columns}
    batch_means: dict[str, list[float]] = {name: [] for name in business_columns}
    batch_orphans: dict[str, list[float]] = {col: [] for col in fk_columns}

    for ref in batches:
        present_meta = metadata.get(ref.batch_id)
        present = present_meta is not None
        frame = (
            _load_batch_frame(engine, business_columns, table, ref.batch_id)
            if present
            else pd.DataFrame({name: pd.Series(dtype=object) for name in business_columns})
        )
        stats: dict[str, ColumnBatchStats] = {}
        row_count = present_meta[0] if present_meta is not None else 0
        observed_header = present_meta[1] if present_meta is not None else None
        for column in business_columns:
            values = frame[column] if present else pd.Series(dtype=object)
            fk_target = fk_columns.get(column)
            orphan_rate: float | None = None
            if present and fk_target is not None:
                orphan_rate = round(
                    fk_orphan_rate(
                        engine, table, column, fk_target[0], fk_target[1], batch_id=ref.batch_id
                    ),
                    6,
                )
                batch_orphans[column].append(orphan_rate)
            column_stats = _column_batch_stats(
                column, values, raw_text=True, numeric_facts=True, orphan_rate=orphan_rate
            )
            stats[column] = column_stats
            if present:
                column_values[column].extend(_text_view(values))
                if column_stats.mean_value is not None:
                    batch_means[column].append(column_stats.mean_value)
        batch_profiles.append(
            BatchTableProfile(
                table=table,
                batch_id=ref.batch_id,
                business_date=ref.business_date,
                present=present,
                row_count=row_count,
                header=observed_header,
                column_stats=stats,
            )
        )

    aggregates: dict[str, ColumnAggregate] = {}
    for column in business_columns:
        window_values = column_values[column]
        window = pd.Series(window_values)
        non_null = sum(1 for value in window_values if value is not None)
        distinct = int(window.nunique())
        numeric = _numeric_series_for_text(window)
        fk_rates = tuple(batch_orphans.get(column, ()))
        aggregates[column] = ColumnAggregate(
            name=column,
            logical_type=infer_logical_type(window_values),
            distinct_count=distinct,
            non_null_count=non_null,
            total_rows=len(window_values),
            unique_in_window=distinct == non_null,
            min_value=_scalar(numeric, "min"),
            max_value=_scalar(numeric, "max"),
            batch_means=tuple(batch_means[column]),
            orphan_rates=fk_rates,
            value_counts=_low_cardinality_counts(window),
        )
    dates = [ref.business_date for ref in batches]
    return TableProfile(
        table=table,
        layer=Layer.RAW,
        batches=batch_profiles,
        aggregates=aggregates,
        total_rows=sum(bp.row_count for bp in batch_profiles),
        first_date=min(dates) if dates else None,
        last_date=max(dates) if dates else None,
    )


def discover_staged_tables(engine: Engine) -> list[StagedTableRef]:
    """Staging/marts base tables that exist right now, deterministic order.

    Contract files materialize one per table (§7.1); a schema may hold views
    (none today); only BASE TABLEs are contract-able. Deterministic ordering
    by schema then name keeps generated catalog summaries stable.
    """
    found: list[StagedTableRef] = []
    for layer, schema in STAGED_SCHEMAS:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_type = 'BASE TABLE' "
                    "ORDER BY table_name"
                ),
                {"s": _validated(schema)},
            ).fetchall()
        for (table,) in rows:
            name = _validated(str(table))
            found.append(StagedTableRef(layer=layer, schema_name=schema, table=name))
    return found


def profile_table_state(engine: Engine, ref: StagedTableRef) -> TableProfile:
    """Whole-snapshot profile of one staging/marts table (staged layer).

    Semantics differ from the raw layer by construction (DECISIONS Phase 2a):
    dbt owns these tables and rebuilds them whole per day, so there is no
    batch to select — the snapshot IS one observation and `logical_type` comes
    straight from the Postgres column types.
    """
    allowed_schemas = {schema for _layer, schema in STAGED_SCHEMAS}
    # Fix verification builds the same dbt models into throwaway sandbox
    # schemas (`dqs_sandbox_<id>_staging` / `_marts`). Those are steward-owned
    # by construction and use the same typed-column semantics, so profiling
    # them is safe; anything else remains rejected.
    is_sandbox = ref.schema_name.startswith("dqs_sandbox_") and bool(
        IDENTIFIER_RE.match(ref.schema_name)
    )
    if ref.schema_name not in allowed_schemas and not is_sandbox:
        raise ValueError(f"schema {ref.schema_name!r} is not a steward-owned staged schema")
    schema = ref.schema_name
    table = _validated(ref.table)
    all_columns = [
        (name, pg_type)
        for name, pg_type in _identified_columns(engine, schema, table)
        # loader-owned system columns never enter contracts (DECISIONS 23)
        if name not in SYSTEM_COLUMNS
    ]
    query = text(f'SELECT * FROM "{schema}"."{table}"')
    with engine.connect() as conn:
        frame = pd.read_sql(query, conn)
    row_count = int(len(frame))

    column_stats: dict[str, ColumnBatchStats] = {}
    aggregates: dict[str, ColumnAggregate] = {}
    for name, pg_type in all_columns:
        logical = pg_type_to_logical(pg_type)
        values = frame[name]
        is_numeric = logical in (LogicalType.INTEGER, LogicalType.NUMERIC)
        numeric = _numeric_series_for_typed(values) if is_numeric else pd.Series(dtype="float64")
        stats = _column_batch_stats(
            name,
            values,
            raw_text=False,
            numeric_facts=is_numeric,
            orphan_rate=None,  # staged layer carries no references (DECISIONS Phase 2a)
        )
        column_stats[name] = stats
        non_null = int(values.notna().sum())
        distinct = int(values.nunique(dropna=True))
        aggregate = ColumnAggregate(
            name=name,
            logical_type=logical,
            distinct_count=distinct,
            non_null_count=non_null,
            total_rows=row_count,
            unique_in_window=distinct == non_null,
            pg_type=pg_type,
            min_value=_scalar(numeric, "min") if is_numeric else None,
            max_value=_scalar(numeric, "max") if is_numeric else None,
            batch_means=(
                (stats.mean_value,) if stats.mean_value is not None and is_numeric else ()
            ),
            orphan_rates=(),
            value_counts=_low_cardinality_counts(values),
        )
        aggregates[name] = aggregate
    snapshot = BatchTableProfile(
        table=ref.qualified,
        batch_id=None,
        business_date=None,
        present=True,
        row_count=row_count,
        header=list(frame.columns),
        column_stats=column_stats,
    )
    return TableProfile(
        table=ref.qualified,
        layer=ref.layer,
        batches=[snapshot],
        aggregates=aggregates,
        total_rows=row_count,
        first_date=None,
        last_date=None,
    )
