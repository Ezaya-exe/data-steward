"""Schema-evolving batch loader into ``raw.*`` (Fivetran-style, §4.2).

Why here: upserts/audit for batch loads must be one atomic operation and must
never let a partial batch into the warehouse. Rules implemented:
- create the table on first sight (all business columns TEXT + system columns);
- add unknown columns via ``ALTER TABLE ADD COLUMN`` (older rows read as NULL);
- columns missing from the batch are simply not inserted (NULL for those rows);
- one transaction covers tables + ``dq.batches`` + ``dq.batch_tables``;
- duplicate batch ids are rejected (re-runs require an explicit ``dqs db reset``),
  because silently appending would double rows and break determinism promises.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Final

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from dqs.errors import ConfigError, DatabaseError

logger = logging.getLogger(__name__)

IDENTIFIER_RE: Final = re.compile(r"^[a-z_][a-z0-9_]*$")
RAW_SCHEMA: Final = "raw"
SYSTEM_COLUMNS: Final = ("_batch_id", "_loaded_at")


def batch_id_for_date(business_date: dt.date) -> str:
    """One batch id per business date across tables (``batch_YYYYMMDD``)."""
    return f"batch_{business_date:%Y%m%d}"


def arrived_at_for(business_date: dt.date) -> dt.datetime:
    """Batch arrival = business_date + 1 day at 02:00Z (simulated SLA clock)."""
    return dt.datetime.combine(business_date + dt.timedelta(days=1), dt.time(2, 0), tzinfo=dt.UTC)


def load_batch(
    engine: Engine,
    business_date: dt.date,
    tables: Mapping[str, pd.DataFrame],
    arrived_at: dt.datetime | None = None,
) -> dict[str, int]:
    """Load one batch (all given tables) atomically; return rows loaded per table.

    ``tables`` maps a raw table name to its DataFrame (business columns only).
    ``arrived_at`` overrides the simulated arrival time (fault F8 late_batch):
    it stamps `dq.batches.arrived_at` and every row's `_loaded_at` so freshness
    detection can see the delay; the business date stays untouched.
    """

    batch_id = batch_id_for_date(business_date)
    arrived = arrived_at if arrived_at is not None else arrived_at_for(business_date)

    for name, frame in tables.items():
        _validate_table_name(name)
        _validate_headers(frame, name)

    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM dq.batches WHERE batch_id = :b"), {"b": batch_id}
        ).first()
        if exists is not None:  # fail before any write: whole batch stays atomic
            raise DatabaseError(
                f"batch {batch_id} already loaded; run 'dqs db reset' before re-seeding"
            )
        conn.execute(
            text(
                "INSERT INTO dq.batches (batch_id, business_date, arrived_at, status) "
                "VALUES (:b, :d, :a, 'loaded')"
            ),
            {"b": batch_id, "d": business_date, "a": arrived},
        )

        summary: dict[str, int] = {}
        for name, frame in tables.items():
            headers = list(frame.columns)
            row_count = _load_table(conn, name, headers, frame, batch_id, arrived)
            conn.execute(
                text(
                    "INSERT INTO dq.batch_tables (batch_id, table_name, row_count, columns) "
                    "VALUES (:b, :t, :n, :c)"
                ),
                {"b": batch_id, "t": name, "n": row_count, "c": headers},
            )
            summary[name] = row_count
    logger.info("loaded batch %s tables=%s", batch_id, summary)
    return summary


def _validate_table_name(name: str) -> None:
    if not IDENTIFIER_RE.match(name):
        raise ConfigError(f"invalid table name {name!r}: must match {IDENTIFIER_RE.pattern}")


def _validate_headers(frame: pd.DataFrame, table: str) -> None:
    for column in frame.columns:
        if not IDENTIFIER_RE.match(str(column)):
            raise ConfigError(
                f"invalid column name {column!r} in table {table!r}: "
                f"must match {IDENTIFIER_RE.pattern}"
            )
        if column in SYSTEM_COLUMNS:
            raise ConfigError(
                f"column {column!r} in table {table!r} collides with loader system column"
            )


def _qualified(table: str) -> str:
    return f'"{RAW_SCHEMA}"."{table}"'


def _existing_columns(conn: Any, table: str) -> list[str] | None:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"
        ),
        {"s": RAW_SCHEMA, "t": table},
    ).fetchall()
    return None if not rows else [str(r) for (r,) in rows]


def _load_table(
    conn: Any,
    table: str,
    headers: Sequence[str],
    frame: pd.DataFrame,
    batch_id: str,
    arrived: dt.datetime,
) -> int:
    existing = _existing_columns(conn, table)
    if existing is None:
        conn.exec_driver_sql(_create_table_sql(table, headers))
    else:
        for column in headers:
            if column not in existing:
                conn.exec_driver_sql(f'ALTER TABLE {_qualified(table)} ADD COLUMN "{column}" text')
    # only columns present in this batch are inserted; absent ones stay NULL
    columns = [*headers, *SYSTEM_COLUMNS]
    if len(frame):
        # COPY is the bulk path (Phase 8a): one round trip per table instead of
        # one parameterised INSERT per row. It writes the exact same text
        # values; `_row_values` keeps the conversion rules identical.
        _copy_rows(conn, table, columns, _row_values(frame, batch_id, arrived))
    return len(frame)


def _create_table_sql(table: str, headers: Sequence[str]) -> str:
    parts = [f'"{c}" text' for c in headers]
    parts.append('"_batch_id" text NOT NULL')
    parts.append('"_loaded_at" timestamptz NOT NULL')
    return f"CREATE TABLE {_qualified(table)} ({', '.join(parts)})"


def _row_values(
    frame: pd.DataFrame,
    batch_id: str,
    arrived: dt.datetime,
) -> Iterator[tuple[Any, ...]]:
    """Yield COPY tuples in the batch header's column order.

    Every business value is stored as TEXT, exactly as the previous
    parameterised INSERT did; NULL/NaN map to SQL NULL. The two loader-owned
    system columns are appended last.
    """
    for record in frame.itertuples(index=False, name=None):
        values: list[Any] = []
        for value in record:
            values.append(None if _is_null(value) else str(value))
        values.append(batch_id)
        values.append(arrived)
        yield tuple(values)


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, float) and np.isnan(value)


def _copy_rows(
    conn: Any,
    table: str,
    columns: Sequence[str],
    rows: Iterator[tuple[Any, ...]],
) -> None:
    """COPY FROM STDIN on the same connection/transaction as the batch row.

    psycopg3 exposes the live driver connection through SQLAlchemy; using it
    directly keeps the atomic per-batch transaction intact (no second commit).
    """
    raw = conn.connection.driver_connection
    columns_sql = ", ".join(f'"{column}"' for column in columns)
    statement = f"COPY {_qualified(table)} ({columns_sql}) FROM STDIN"
    with raw.cursor() as cursor, cursor.copy(statement) as copy:
        for row in rows:
            copy.write_row(row)
