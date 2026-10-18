"""Fault-path integration tests on a THROWAWAY Postgres database.

Why not the dev warehouse: these tests load real batches into `raw.*` and
mutate its schema (a renamed column is ALTER'ed in); running that against the
dev warehouse would pollute the exact clean baseline contracts need. Instead
we CREATE DATABASE dqs_test, init roles/schemas/dq tables in it, load the
faulted days through the real `sim_pipeline` + `loader` (run_dbt=False), and
DROP the database afterwards — dev warehouse untouched.

Skipped when Postgres is unreachable; marked ``integration``.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, text

from dqs import db as dbmod
from dqs.config import Settings
from dqs.sim import loader, pipeline
from dqs.sim.faults import parse_fault_specs

pytestmark = pytest.mark.integration

TEST_DB = "dqs_test"
SEED = 42
DATE_RENAME = dt.date(2046, 1, 2)  # far future: cannot collide with the dev baseline
DATE_RENAME_AFTER = dt.date(2046, 1, 3)
DATE_LATE = dt.date(2047, 3, 4)
DATE_MISSING = dt.date(2047, 3, 6)


@pytest.fixture(scope="module")
def throwaway(settings: Settings) -> tuple[Settings, Engine]:
    """Create, init, hand out (and finally drop) the throwaway database."""
    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEST_DB}"')
        test_settings = settings.model_copy(update={"pg_database": TEST_DB})
        dbmod.init_database(test_settings)
        engine = dbmod.make_rw_engine(test_settings)
        try:
            yield test_settings, engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    finally:
        admin.dispose()


def _observed_header(engine: Engine, batch_id: str, table: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT columns FROM dq.batch_tables WHERE batch_id = :b AND table_name = :t"),
                {"b": batch_id, "t": table},
            ).scalar_one()
        )


def _table_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return {
            str(row[0])
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'raw' AND table_name = :t"
                ),
                {"t": table},
            )
        }


def test_column_rename_loads_and_records_the_renamed_header(
    throwaway: tuple[Settings, Engine],
) -> None:
    test_settings, engine = throwaway
    result = pipeline.load_day(
        test_settings,
        DATE_RENAME,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(["column_rename:orders.customer_id:new_name=cust_id"]),
    )
    assert result["rows"]["orders"] > 0

    batch_id = loader.batch_id_for_date(DATE_RENAME)
    # the observed header is the batch's truth: the renamed name, old name gone
    assert _observed_header(engine, batch_id, "orders") == [
        "order_id",
        "cust_id",
        "order_ts",
        "status",
        "total_amount",
    ]
    # fresh table created from the (renamed) header: schema evolution followed it
    assert "cust_id" in _table_columns(engine, "orders")
    with engine.connect() as conn:
        loaded = pd.read_sql(
            text('SELECT "cust_id" FROM "raw"."orders" WHERE "_batch_id" = :b'),
            conn,
            params={"b": batch_id},
        )
    assert loaded["cust_id"].notna().all()  # values live under the renamed column


def test_renamed_header_then_clean_header_adds_second_column(
    throwaway: tuple[Settings, Engine],
) -> None:
    """ALTER-branch of schema evolution: customer_id re-added, old rows NULL there."""
    test_settings, engine = throwaway
    pipeline.load_day(
        test_settings,
        DATE_RENAME_AFTER,
        SEED,
        run_dbt=False,  # clean day after the renamed one
    )
    batch_id = loader.batch_id_for_date(DATE_RENAME_AFTER)
    assert "customer_id" in _observed_header(engine, batch_id, "orders")
    columns = _table_columns(engine, "orders")
    assert {"cust_id", "customer_id"} <= columns  # both exist post-ALTER
    with engine.connect() as conn:
        counts = conn.execute(
            text(
                'SELECT "_batch_id", count(*) FILTER (WHERE "customer_id" IS NULL) '
                'FROM "raw"."orders" GROUP BY "_batch_id" ORDER BY 1'
            )
        ).all()
    by_batch = {str(b): int(n) for b, n in counts}
    assert by_batch[loader.batch_id_for_date(DATE_RENAME)] > 0  # renamed rows NULL there
    assert by_batch[batch_id] == 0  # clean-day rows carry customer_id
    with engine.connect() as conn:
        renamed_ids = conn.execute(
            text(
                'SELECT count(*) FROM "raw"."orders" WHERE "_batch_id" = :b AND "cust_id" IS NULL'
            ),
            {"b": loader.batch_id_for_date(DATE_RENAME)},
        ).scalar_one()
    assert renamed_ids == 0


def test_late_batch_records_the_shifted_arrival_time(
    throwaway: tuple[Settings, Engine],
) -> None:
    test_settings, engine = throwaway
    pipeline.load_day(
        test_settings,
        DATE_LATE,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(["late_batch:orders:hours=6"]),
    )
    batch_id = loader.batch_id_for_date(DATE_LATE)
    expected = loader.arrived_at_for(DATE_LATE) + dt.timedelta(hours=6)
    with engine.connect() as conn:
        arrived = conn.execute(
            text("SELECT arrived_at FROM dq.batches WHERE batch_id = :b"), {"b": batch_id}
        ).scalar_one()
        row_stamps = (
            conn.execute(
                text('SELECT DISTINCT "_loaded_at" FROM "raw"."orders" WHERE "_batch_id" = :b'),
                {"b": batch_id},
            )
            .scalars()
            .all()
        )
    assert arrived == expected  # timestamptz read back as aware UTC
    assert row_stamps == [arrived]  # every row's _loaded_at is the late arrival


def test_missing_table_is_absent_from_dq_batch_tables(
    throwaway: tuple[Settings, Engine],
) -> None:
    test_settings, engine = throwaway
    pipeline.load_day(
        test_settings,
        DATE_MISSING,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(["missing_table:payments"]),
    )
    batch_id = loader.batch_id_for_date(DATE_MISSING)
    with engine.connect() as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                text("SELECT table_name FROM dq.batch_tables WHERE batch_id = :b"),
                {"b": batch_id},
            )
        }
    assert "payments" not in tables  # absent, NOT "0 rows recorded"
    assert tables == {"customers", "products", "orders", "order_items"}
