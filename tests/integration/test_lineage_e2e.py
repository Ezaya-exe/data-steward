"""Impact/lineage acceptance on a real faulted day (Phase 3, integration).

The module seeds its own throwaway database (``dqs_lineage_e2e``) with the
30-day baseline — WITHOUT dbt builds — then loads one F1 faulted day with a
real ``dbt build``, exactly as ``dqs sim day`` would. Assertions:

1. the migration is idempotent (``dqs db init`` twice on an existing DB);
2. an upgrade of a pre-002 database applies 002 and stays idempotent;
3. after the faulted day, ``dq.pipeline_runs`` holds the parsed failing
   nodes (DECISIONS 55), not just a log excerpt;
4. the monitor creates the incident and ``dqs impact`` prints affected
   models, stored failing tests, exposures with owners and the score, and
   persists the report into ``dq.incidents.impact``.

The dev warehouse is never touched: everything happens in the module DB (dbt
is pointed at it via the process env dbt itself reads).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import text

from dqs import db as dbmod
from dqs import dbt_run
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.config import Settings
from dqs.contracts import store
from dqs.sim import pipeline
from dqs.sim.faults import parse_fault_specs

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
MODULE_DB = "dqs_lineage_e2e"
LEGACY_DB = "dqs_lineage_pre002"
SEED = 42
BASELINE_DAYS = 30
FAULT_DATE = dt.date(2026, 2, 5)
CLEAN_DATES = [dt.date(2026, 1, 31) + dt.timedelta(days=i) for i in range(5)]
CONTRACTS_DIR = ROOT / "contracts"
F1_SPEC = "column_rename:orders.customer_id:new_name=cust_id"


@pytest.fixture(scope="module")
def module_db(settings: Settings) -> Iterator[Settings]:
    """Create + init + seed the 30-day baseline once (no dbt builds)."""
    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        try:
            with admin.connect() as conn:
                conn.execute(text("SELECT 1"))
        except sa.exc.SQLAlchemyError as exc:
            pytest.skip(f"Postgres not reachable: {exc}")
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{MODULE_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{MODULE_DB}"')
        module_settings = settings.model_copy(update={"pg_database": MODULE_DB})
        dbmod.init_database(module_settings)
        pipeline.seed_baseline(module_settings, BASELINE_DAYS, SEED, run_dbt=False)
        # contiguous clean held-out days before the fault day: customer ids are
        # date-slotted (DECISION 18) and the monitor's missing-batch walk would
        # otherwise turn the gap into a second incident
        for day in CLEAN_DATES:
            pipeline.load_day(module_settings, day, SEED, run_dbt=False)
        yield module_settings
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{MODULE_DB}" WITH (FORCE)')
        admin.dispose()


def _pipeline_run_columns(engine: sa.Engine) -> set[str]:
    return {
        row[0]
        for row in engine.connect().execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'dq' AND table_name = 'pipeline_runs'"
            )
        )
    }


def test_db_init_twice_on_existing_db_is_idempotent(module_db: Settings) -> None:
    # The second run re-hits every ADD COLUMN IF NOT EXISTS / IF NOT EXISTS path.
    dbmod.init_database(module_db)
    dbmod.init_database(module_db)
    engine = dbmod.make_rw_engine(module_db)
    try:
        columns = _pipeline_run_columns(engine)
    finally:
        engine.dispose()
    assert {"failed_nodes", "run_results_parsed_at"} <= columns


def test_migration_002_applies_to_a_pre_002_database(settings: Settings) -> None:
    """Simulate an existing DB that predates 002, then upgrade it in place."""
    source_dir = ROOT / "sql"

    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{LEGACY_DB}"')
        legacy_settings = settings.model_copy(update={"pg_database": LEGACY_DB})

        # 1) a database created before 002 has the 001 columns only (replay the
        # pre-002 files the way init_database does — one multi-statement trip)
        legacy_engine = dbmod.make_rw_engine(legacy_settings)
        try:
            with legacy_engine.connect() as conn:
                for name in ("000_roles.sql", "001_init.sql"):
                    conn.exec_driver_sql((source_dir / name).read_text(encoding="utf-8"))
                conn.commit()
        finally:
            legacy_engine.dispose()
        engine = dbmod.make_rw_engine(legacy_settings)
        try:
            before = _pipeline_run_columns(engine)
        finally:
            engine.dispose()
        assert "failed_nodes" not in before

        # 2) rerunning init with the REAL sql dir applies 002, twice, safely
        dbmod.init_database(legacy_settings)
        dbmod.init_database(legacy_settings)
        engine = dbmod.make_rw_engine(legacy_settings)
        try:
            after = _pipeline_run_columns(engine)
        finally:
            engine.dispose()
        assert {"failed_nodes", "run_results_parsed_at"} <= after
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)')
        admin.dispose()


def test_f1_day_records_failing_nodes_and_impact_prints_the_report(
    module_db: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # dbt picks the database from the process env (profiles.yml); point it at
    # the module DB so the dev warehouse is never touched by a test build.
    monkeypatch.setenv("PGDATABASE", MODULE_DB)
    result = pipeline.load_day(
        module_db,
        FAULT_DATE,
        SEED,
        run_dbt=True,  # a REAL failing dbt build against the module DB
        specs=parse_fault_specs([F1_SPEC]),
    )
    assert result["dbt_exit"] not in (0, None)  # staging breaks loudly on the rename

    engine = dbmod.make_rw_engine(module_db)
    try:
        # 1) the failing nodes were parsed at build time and stored with the run
        record = dbt_run.latest_run_for_batch(engine, "batch_20260205")
        assert record is not None
        assert record.run_results_parsed_at is not None
        assert record.failed_nodes, "a broken build must keep parsed failing nodes"
        assert all(n.status in ("fail", "error") for n in record.failed_nodes)
        assert any("stg_orders" in n.unique_id for n in record.failed_nodes)

        # 2) the monitor creates the F1 incident
        contracts = store.load_approved(CONTRACTS_DIR)
        monitor_mod.run_monitor(engine, contracts, None)
        incidents = incidents_mod.list_incidents(engine)
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.table == "raw.orders"

        # 3) the CLI report prints the acceptance details and persists impact
        from typer.testing import CliRunner

        from dqs.cli import app as dqs_app

        monkeypatch.setattr("dqs.cli.load_settings", lambda: module_db)
        runner = CliRunner()
        cli_result = runner.invoke(dqs_app, ["impact", str(incident.incident_id)])
        out = cli_result.output
        if cli_result.exception is not None:
            raise AssertionError(f"impact CLI raised: {cli_result.exception!r}\noutput:\n{out}")
        assert "stg_orders" in out  # affected models
        assert "revenue_dashboard" in out  # exposures printed
        assert "Finance Analytics" in out  # exposures with owners
        assert "blast radius score" in out.lower()

        persisted = incidents_mod.get_incident(engine, incident.incident_id)
        assert persisted is not None
        assert persisted.impact is not None
        payload = persisted.impact
        assert isinstance(payload, dict)
        model_names = [m["name"] for m in payload.get("affected_models", [])]
        assert "stg_orders" in model_names
        assert "score" in payload["summary"].lower()
        # the full report JSON is a valid structure (spot-check fields)
        assert json.dumps(payload["affected_exposures"], ensure_ascii=False)
    finally:
        engine.dispose()
