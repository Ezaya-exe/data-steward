"""Integration proof that `dqs_ro` blocks writes independently of the SQL guard.

The statements use `WHERE false` so even a misconfigured role cannot change
data; DDL runs inside an explicit transaction that is always rolled back. The
timeout test uses a recursive SELECT (not `pg_sleep`, which the guard rejects)
to prove the 5 s statement cap is real.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs.errors import SqlExecutionError, ToolError
from dqs.tools.base import ToolContext
from dqs.tools.sql_guard import execute_readonly

pytestmark = pytest.mark.integration

WRITE_SQL = [
    "DELETE FROM dq.batches WHERE false",
    "UPDATE dq.batches SET status = 'guard_probe' WHERE false",
    (
        "INSERT INTO dq.batches (batch_id, business_date, arrived_at) "
        "SELECT 'guard_probe', CURRENT_DATE, now() WHERE false"
    ),
    "CREATE TABLE raw.dqs_guard_probe (x integer)",
]


@pytest.mark.parametrize("sql", WRITE_SQL)
def test_ro_role_blocks_writes(ro_engine: Engine, sql: str) -> None:
    connection = ro_engine.connect()
    transaction = connection.begin()
    try:
        with pytest.raises(SQLAlchemyError) as excinfo:
            connection.execute(text(sql))
        message = str(excinfo.value).lower()
        assert "permission denied" in message or "read-only" in message
    finally:
        transaction.rollback()
        connection.close()


def test_guarded_select_runs_on_ro_role_and_is_bounded(ro_engine: Engine) -> None:
    result = execute_readonly(ro_engine, "SELECT batch_id FROM dq.batches")
    if result.row_count == 0:
        pytest.skip("warehouse has no batches to read")
    assert result.row_count <= 200
    assert result.columns == ["batch_id"]
    assert result.truncated is (result.row_count == 200)


def test_statement_timeout_is_enforced(ro_engine: Engine) -> None:
    endless = (
        "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT count(*) FROM t"
    )
    with pytest.raises(SqlExecutionError, match="timeout|canceling"):
        execute_readonly(ro_engine, endless, timeout_ms=200)


def test_tool_context_accepts_the_real_ro_engine(ro_engine: Engine) -> None:
    context = ToolContext(engine=ro_engine)
    assert context.engine is ro_engine


def test_tool_context_rejects_the_real_rw_engine(rw_engine: Engine) -> None:
    """The role check must reject the engine that can write, not just a stub."""
    with pytest.raises(ToolError, match="dqs_ro"):
        ToolContext(engine=rw_engine)
