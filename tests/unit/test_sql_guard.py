"""Negative/positive unit tests for the deny-by-default SQL guard.

Every rule in AGENTS.md §7.2 has at least one rejection test here; the
DB-level role/read-only proof lives in `tests/integration/test_sql_guard_db.py`.
"""

from __future__ import annotations

import pytest

from dqs.errors import SqlGuardError
from dqs.tools.sql_guard import validate_sql

INVALID_SQL = [
    # DML / DDL / COPY
    "DROP TABLE raw.orders",
    "DELETE FROM raw.orders",
    "UPDATE raw.orders SET status = 'paid'",
    "INSERT INTO raw.orders (order_id) VALUES ('1')",
    "CREATE TABLE raw.evil (x int)",
    "ALTER TABLE raw.orders ADD COLUMN evil text",
    "TRUNCATE raw.orders",
    "COPY raw.orders FROM '/tmp/orders.csv'",
    # stacked statements and comment tricks
    "SELECT 1; DROP TABLE raw.orders",
    "SELECT 1; -- ; DROP TABLE raw.orders",
    "SELECT * FROM raw.orders; -- ; DELETE FROM raw.orders",
    "SELECT 1;;",
    # locking / SELECT INTO
    "SELECT * FROM raw.orders FOR UPDATE",
    "SELECT * FROM raw.orders FOR SHARE",
    "SELECT * FROM raw.orders FOR NO KEY UPDATE",
    "SELECT * INTO copy_orders FROM raw.orders",
    # relations outside the allowlist / unqualified / catalog
    "SELECT * FROM orders",
    "SELECT * FROM public.orders",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM pg_catalog.pg_class",
    "SELECT * FROM warehouse.raw.orders",
    "SELECT * FROM raw.orders, pg_catalog.pg_class",
    # dangerous functions
    "SELECT pg_sleep(100)",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_ls_dir('/')",
    "SELECT pg_terminate_backend(123)",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/x')",
    "SELECT dblink('host=evil dbname=x')",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT current_setting('data_directory')",
    "SELECT query_to_xml('SELECT * FROM raw.orders', true, true, '')",
    # SQL embedded in a CTE (not just the top-level statement)
    "WITH x AS (DELETE FROM raw.orders RETURNING *) SELECT * FROM x",
    "WITH x AS (INSERT INTO raw.orders (order_id) VALUES ('1') RETURNING *) SELECT * FROM x",
    "WITH x AS (SELECT * FROM raw.orders) SELECT * FROM x UNION SELECT * FROM pg_catalog.pg_class",
]

VALID_SQL = [
    "SELECT 1",
    "SELECT * FROM raw.orders",
    "SELECT * FROM raw.orders;",
    "SELECT * FROM raw.orders LIMIT 50",
    "SELECT * FROM raw.orders ORDER BY order_id OFFSET 5",
    "SELECT ';' AS marker FROM raw.orders",
    "SELECT * FROM raw.orders /* ; DROP TABLE raw.orders */",
    "WITH x AS (SELECT * FROM raw.orders) SELECT * FROM x",
    "SELECT * FROM raw.orders JOIN staging.stg_orders USING (order_id)",
    "SELECT * FROM raw.orders WHERE order_id IN (SELECT order_id FROM raw.order_items)",
]


@pytest.mark.parametrize("sql", INVALID_SQL)
def test_guard_rejects(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        validate_sql(sql)


@pytest.mark.parametrize("sql", VALID_SQL)
def test_guard_accepts_and_returns_executable_sql(sql: str) -> None:
    capped = validate_sql(sql)
    assert capped
    assert "SELECT" in capped.upper()


def test_guard_caps_missing_limit() -> None:
    assert validate_sql("SELECT * FROM raw.orders").endswith("LIMIT 200")


def test_guard_caps_oversized_limit() -> None:
    assert validate_sql("SELECT * FROM raw.orders LIMIT 5000").endswith("LIMIT 200")


def test_guard_keeps_smaller_limit() -> None:
    assert validate_sql("SELECT * FROM raw.orders LIMIT 50").endswith("LIMIT 50")


def test_guard_caps_limit_all() -> None:
    assert validate_sql("SELECT * FROM raw.orders LIMIT ALL").endswith("LIMIT 200")


def test_guard_rejects_empty_sql() -> None:
    with pytest.raises(SqlGuardError, match="empty"):
        validate_sql("   ")
