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
) -> dict[str, int]:
    """Load one batch (all given tables) atomically; return rows loaded per table.

    ``tables`` maps a raw table name to its DataFrame (business columns only).
    """

    batch_id = batch_id_for_date(business_date)
    arrived = arrived_at_for(business_date)

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
    rows = _row_dicts(frame, batch_id, arrived)
    # only columns present in this batch are inserted; absent ones stay NULL
    columns = [*headers, *SYSTEM_COLUMNS]
    if rows:
        sql = _insert_sql(table, columns)
        for chunk in _chunks(rows, 1000):
            conn.execute(text(sql), chunk)
    return len(rows)


def _create_table_sql(table: str, headers: Sequence[str]) -> str:
    parts = [f'"{c}" text' for c in headers]
    parts.append('"_batch_id" text NOT NULL')
    parts.append('"_loaded_at" timestamptz NOT NULL')
    return f"CREATE TABLE {_qualified(table)} ({', '.join(parts)})"


def _insert_sql(table: str, columns: Sequence[str]) -> str:
    columns_sql = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    return f"INSERT INTO {_qualified(table)} ({columns_sql}) VALUES ({placeholders})"


def _row_dicts(
    frame: pd.DataFrame,
    batch_id: str,
    arrived: dt.datetime,
) -> list[dict[str, Any]]:
    columns = list(frame.columns)
    rows: list[dict[str, Any]] = []
    for record in frame.itertuples(index=False, name=None):
        row: dict[str, Any] = {
            column: (
                None
                if value is None or (isinstance(value, float) and np.isnan(value))
                else str(value)
            )
            for column, value in zip(columns, record, strict=True)
        }
        row["_batch_id"] = batch_id
        row["_loaded_at"] = arrived
        rows.append(row)
    return rows


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]
