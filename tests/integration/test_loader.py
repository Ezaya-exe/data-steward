"""Integration tests: schema-evolving loader behavior (Phase 1 acceptance).

Uses a dedicated probe table ``raw.loader_probe`` (not one of the 5 business
tables) and cleans everything up, so the seeded baseline stays untouched.
DB-dependent: marker ``integration``; skipped when Postgres is unreachable.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from sqlalchemy import engine as sqla_engine
from sqlalchemy import text

from dqs.errors import ConfigError, DatabaseError
from dqs.sim import loader

pytestmark = pytest.mark.integration

PROBE_TABLE = "loader_probe"
PROBE_DATES = (dt.date(2030, 1, 1), dt.date(2030, 1, 2), dt.date(2030, 1, 3), dt.date(2030, 1, 4))


@pytest.fixture(scope="function")
def probe(rw_engine: sqla_engine.Engine) -> sqla_engine.Engine:
    yield rw_engine
    with rw_engine.begin() as conn:
        conn.exec_driver_sql(f'DROP TABLE IF EXISTS "raw"."{PROBE_TABLE}"')
        conn.execute(
            text(
                "DELETE FROM dq.batch_tables WHERE table_name = :t OR batch_id IN "
                "(SELECT batch_id FROM dq.batches WHERE business_date >= '2030-01-01')"
            ),
            {"t": PROBE_TABLE},
        )
        conn.execute(
            text("DELETE FROM dq.batches WHERE business_date >= '2030-01-01'"),
            {},
        )
        conn.exec_driver_sql("DELETE FROM dq.pipeline_runs WHERE batch_id IS NULL")


def test_first_load_creates_table_and_records_batch(probe: sqla_engine.Engine) -> None:
    frame = pd.DataFrame({"a": ["1"], "b": ["x"]})
    counts = loader.load_batch(probe, PROBE_DATES[0], {PROBE_TABLE: frame})
    assert counts == {PROBE_TABLE: 1}
    with probe.connect() as conn:
        columns = sorted(
            str(c)
            for (c,) in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='raw' AND table_name=:t"
                ),
                {"t": PROBE_TABLE},
            )
        )
        assert columns == ["_batch_id", "_loaded_at", "a", "b"]
        header = conn.execute(
            text("SELECT columns FROM dq.batch_tables WHERE batch_id=:b AND table_name=:t"),
            {"b": loader.batch_id_for_date(PROBE_DATES[0]), "t": PROBE_TABLE},
        ).scalar_one()
        # observed header is the batch's business columns only — not system columns
        assert list(header) == ["a", "b"]


def test_extra_column_is_added_and_missing_rows_read_null(probe: sqla_engine.Engine) -> None:
    loader.load_batch(probe, PROBE_DATES[0], {PROBE_TABLE: pd.DataFrame({"a": ["1"], "b": ["x"]})})
    loader.load_batch(
        probe,
        PROBE_DATES[1],
        {PROBE_TABLE: pd.DataFrame({"a": ["2"], "b": ["y"], "c": ["z"]})},
    )
    with probe.connect() as conn:
        columns = {
            str(c)
            for (c,) in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='raw' AND table_name=:t"
                ),
                {"t": PROBE_TABLE},
            )
        }
        assert "c" in columns
        rows = conn.execute(
            text(f'SELECT a, b, c FROM "raw"."{PROBE_TABLE}" ORDER BY a')
        ).fetchall()
        # row from the FIRST batch (before column c existed) reads as NULL in c
        assert [tuple(r) for r in rows] == [("1", "x", None), ("2", "y", "z")]


def test_missing_column_header_records_without_it(probe: sqla_engine.Engine) -> None:
    loader.load_batch(probe, PROBE_DATES[0], {PROBE_TABLE: pd.DataFrame({"a": ["1"], "b": ["x"]})})
    loader.load_batch(
        probe,
        PROBE_DATES[1],
        {PROBE_TABLE: pd.DataFrame({"a": ["2"], "b": ["y"], "c": ["z"]})},
    )
    # batch 3 contains only column a: header (observed truth) records that, not the table shape
    loader.load_batch(probe, PROBE_DATES[2], {PROBE_TABLE: pd.DataFrame({"a": ["3"]})})
    with probe.connect() as conn:
        header = list(
            conn.execute(
                text("SELECT columns FROM dq.batch_tables WHERE batch_id=:b AND table_name=:t"),
                {"b": loader.batch_id_for_date(PROBE_DATES[2]), "t": PROBE_TABLE},
            ).scalar_one()
        )
        assert header == ["a"]
        rows = conn.execute(
            text(f'SELECT a, b, c FROM "raw"."{PROBE_TABLE}" ORDER BY a')
        ).fetchall()
        assert [tuple(r) for r in rows] == [("1", "x", None), ("2", "y", "z"), ("3", None, None)]


def test_duplicate_batch_is_rejected(probe: sqla_engine.Engine) -> None:
    loader.load_batch(probe, PROBE_DATES[0], {PROBE_TABLE: pd.DataFrame({"a": ["1"]})})
    with pytest.raises(DatabaseError, match="already loaded"):
        loader.load_batch(probe, PROBE_DATES[0], {PROBE_TABLE: pd.DataFrame({"a": ["1"]})})


def test_failing_load_leaves_no_partial_batch(probe: sqla_engine.Engine) -> None:
    with pytest.raises(ConfigError, match="invalid column name"):
        loader.load_batch(
            probe,
            PROBE_DATES[3],
            {PROBE_TABLE: pd.DataFrame({"a": ["1"], "Bad Column!": ["x"]})},
        )
    with probe.connect() as conn:
        # the reject happened pre-transaction; nothing was written at all
        assert (
            conn.execute(
                text("SELECT count(*) FROM dq.batches WHERE business_date = :d"),
                {"d": PROBE_DATES[3]},
            ).scalar_one()
            == 0
        )
