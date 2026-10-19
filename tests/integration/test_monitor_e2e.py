"""Monitor / check-engine acceptance on a TEMPLATE Postgres database (Phase 2b).

Why a template: every scenario needs its own clean warehouse seeded from one
30-day baseline, but re-seeding that baseline per test is slow and
non-deterministic to schedule. We seed it **once** into ``dqs_monitor_template``
(then add the clean held-out days 2026-01-31…02-04 shared by every scenario) and
clone it per test with ``CREATE DATABASE … TEMPLATE``. Clones are dropped
afterwards; the dev warehouse is never touched.

Contiguity matters: customer ids are date-slotted (DECISIONS 18), so days must
be loaded in order — a gap would make clean FK values look orphaned. Fault days
therefore start on the day right after the shared clean window.

Skipped when Postgres is unreachable; marked ``integration``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs import db as dbmod
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.checks.engine import CheckEngine
from dqs.config import Settings
from dqs.contracts import store
from dqs.models.enums import IncidentStatus, Severity
from dqs.sim import pipeline
from dqs.sim.faults import parse_fault_specs

pytestmark = pytest.mark.integration

TEMPLATE_DB = "dqs_monitor_template"
SEED = 42
BASELINE_DAYS = 30
CLEAN_DATES = [dt.date(2026, 1, 31) + dt.timedelta(days=i) for i in range(5)]
FAULT_DATE = dt.date(2026, 2, 5)  # first day after the shared clean window

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

# (label, specs, expected {(check, table, column, severity)}) — one fault per day,
# so the set is exact: a violation on any other column would fail the test.
FAULTS: list[tuple[str, list[str], set[tuple[str, str | None, str | None, Severity]]]] = [
    (
        "F1_column_rename",
        ["column_rename:orders.customer_id:new_name=cust_id"],
        {
            ("schema.missing_column", "raw.orders", "customer_id", Severity.BREAKING),
            ("schema.unexpected_column", "raw.orders", "cust_id", Severity.WARNING),
        },
    ),
    (
        "F2_column_removed",
        ["column_removed:payments.method"],
        {("schema.missing_column", "raw.payments", "method", Severity.BREAKING)},
    ),
    (
        "F3_type_change",
        ["type_change:orders.total_amount"],
        {("type.logical", "raw.orders", "total_amount", Severity.BREAKING)},
    ),
    (
        "F4_null_spike",
        ["null_spike:orders.status:fraction=0.5"],
        {("nulls.rate", "raw.orders", "status", Severity.BREAKING)},
    ),
    (
        "F5_orphan_keys",
        ["orphan_keys:payments.order_id:fraction=0.5"],
        {("fk.integrity", "raw.payments", "order_id", Severity.BREAKING)},
    ),
    (
        "F6_duplicate_rows",
        ["duplicate_rows:orders:fraction=0.05"],
        {("unique.pk", "raw.orders", "order_id", Severity.BREAKING)},
    ),
    (
        "F7_scale_shift",
        ["scale_shift:orders.total_amount:factor=100"],
        {("range.bounds", "raw.orders", "total_amount", Severity.BREAKING)},
    ),
    (
        "F8_late_batch",
        ["late_batch:payments:hours=24"],
        {("freshness.sla", None, None, Severity.BREAKING)},
    ),
    (
        "F9_missing_table",
        ["missing_table:payments"],
        {("volume.rows", "raw.payments", None, Severity.BREAKING)},
    ),
    (
        "F10_column_added",
        ["column_added:orders:new_column=coupon_code"],
        {("schema.unexpected_column", "raw.orders", "coupon_code", Severity.WARNING)},
    ),
]


@pytest.fixture(scope="session")
def template(settings: Settings) -> Iterator[Settings]:
    """Seed the shared 30-day baseline once; drop the template at exit.

    Only the *baseline* lives in the template: each scenario then loads the
    future days it needs, so the missing-batch case can genuinely skip a date
    while the other cases stay contiguous.
    """
    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        try:
            with admin.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            pytest.skip(f"Postgres not reachable: {exc}")
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEMPLATE_DB}"')
        template_settings = settings.model_copy(update={"pg_database": TEMPLATE_DB})
        dbmod.init_database(template_settings)
        pipeline.seed_baseline(template_settings, BASELINE_DAYS, SEED, run_dbt=False)
        yield template_settings
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def clone(settings: Settings, template: Settings) -> Iterator[tuple[Settings, Engine]]:
    """A throwaway copy of the seeded template (dropped after the test)."""
    name = f"dqs_mnt_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"')
        clone_settings = settings.model_copy(update={"pg_database": name})
        engine = dbmod.make_rw_engine(clone_settings)
        try:
            yield clone_settings, engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


def _contracts() -> dict:
    return store.load_approved(CONTRACTS_DIR)


def _load_clean(clone_settings: Settings) -> None:
    """Load the shared clean held-out window, keeping FK ids contiguous."""
    for day in CLEAN_DATES:
        pipeline.load_day(clone_settings, day, SEED, run_dbt=False)


def _incident_violations(engine: Engine) -> list:
    return [
        violation
        for incident in incidents_mod.list_incidents(engine)
        for violation in incident.violations
    ]


# --- zero false positives ------------------------------------------------------------


def test_clean_baseline_and_heldout_days_produce_no_incidents(
    clone: tuple[Settings, Engine],
) -> None:
    """The acceptance guard: clean data must not raise a single incident."""
    clone_settings, engine = clone
    _load_clean(clone_settings)
    result = monitor_mod.run_monitor(engine, _contracts())
    assert result.missing_batches == []
    assert result.created == [] and result.updated == []
    assert incidents_mod.list_incidents(engine) == []
    # every loaded batch was consumed exactly once
    with engine.connect() as conn:
        statuses = dict(
            conn.execute(text("SELECT status, count(*) FROM dq.batches GROUP BY status")).fetchall()
        )
    assert statuses == {"checked": BASELINE_DAYS + len(CLEAN_DATES)}


# --- detection matrix ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("specs", "expected"),
    [(specs, expected) for _, specs, expected in FAULTS],
    ids=[label for label, _, _ in FAULTS],
)
def test_engine_flags_exactly_the_expected_violations(
    clone: tuple[Settings, Engine],
    specs: list[str],
    expected: set[tuple[str, str | None, str | None, Severity]],
) -> None:
    """F1–F10 fire on the expected table/column and nowhere unrelated."""
    clone_settings, engine = clone
    _load_clean(clone_settings)
    pipeline.load_day(
        clone_settings, FAULT_DATE, SEED, run_dbt=False, specs=parse_fault_specs(specs)
    )
    batch_id = f"batch_{FAULT_DATE:%Y%m%d}"
    violations = CheckEngine(engine, _contracts()).run(batch_id)
    observed = {(v.check, v.table, v.column, v.severity) for v in violations}
    assert observed == expected


def test_f10_column_added_is_a_warning_not_breaking(
    clone: tuple[Settings, Engine],
) -> None:
    """F10 is schema drift that must not page anyone: warning severity only."""
    clone_settings, engine = clone
    _load_clean(clone_settings)
    pipeline.load_day(
        clone_settings,
        FAULT_DATE,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(["column_added:orders:new_column=coupon_code"]),
    )
    batch_id = f"batch_{FAULT_DATE:%Y%m%d}"
    violations = CheckEngine(engine, _contracts()).run(batch_id)
    assert {v.severity for v in violations} == {Severity.WARNING}


# --- missing batch -------------------------------------------------------------------


def test_a_skipped_business_date_becomes_a_missing_batch_incident(
    clone: tuple[Settings, Engine],
) -> None:
    """Skip 2026-02-01, monitor as-of 2026-02-03 -> monitor.missing_batch."""
    clone_settings, engine = clone
    pipeline.load_day(clone_settings, dt.date(2026, 1, 31), SEED, run_dbt=False)
    pipeline.load_day(clone_settings, dt.date(2026, 2, 2), SEED, run_dbt=False)
    as_of = monitor_mod.parse_as_of("2026-02-03T00:00Z")
    result = monitor_mod.run_monitor(engine, _contracts(), as_of)
    assert result.missing_batches == [dt.date(2026, 2, 1)]
    missing = [
        violation
        for violation in _incident_violations(engine)
        if violation.check == "monitor.missing_batch"
    ]
    assert [violation.observed["business_date"] for violation in missing] == ["2026-02-01"]
    assert all(violation.batch_id is None for violation in missing)


def test_missing_batch_check_needs_no_approved_contracts(
    clone: tuple[Settings, Engine],
) -> None:
    """Even with no contracts, a missing date is a fact the monitor can report."""
    clone_settings, engine = clone
    pipeline.load_day(clone_settings, dt.date(2026, 1, 31), SEED, run_dbt=False)
    pipeline.load_day(clone_settings, dt.date(2026, 2, 2), SEED, run_dbt=False)
    result = monitor_mod.run_monitor(engine, {}, monitor_mod.parse_as_of("2026-02-03T00:00Z"))
    assert result.missing_batches == [dt.date(2026, 2, 1)]


# --- de-duplication ------------------------------------------------------------------


def test_same_fault_on_two_days_keeps_one_open_incident_and_grows_batches(
    clone: tuple[Settings, Engine],
) -> None:
    """The de-dup acceptance: consecutive-day recurrence merges, never duplicates."""
    clone_settings, engine = clone
    _load_clean(clone_settings)
    contracts = _contracts()
    specs = parse_fault_specs(["column_removed:payments.method"])

    pipeline.load_day(clone_settings, FAULT_DATE, SEED, run_dbt=False, specs=specs)
    first = monitor_mod.run_monitor(engine, contracts, monitor_mod.parse_as_of("2026-02-06T00:00Z"))
    assert len(first.created) == 1 and first.updated == []
    incident_id = first.created[0]
    created = incidents_mod.get_incident(engine, incident_id)
    assert created is not None and created.status is IncidentStatus.OPEN
    assert created.batches == [f"batch_{FAULT_DATE:%Y%m%d}"]

    next_day = FAULT_DATE + dt.timedelta(days=1)
    pipeline.load_day(clone_settings, next_day, SEED, run_dbt=False, specs=specs)
    second = monitor_mod.run_monitor(
        engine, contracts, monitor_mod.parse_as_of("2026-02-07T00:00Z")
    )
    assert second.created == [] and second.updated == [incident_id]

    merged = incidents_mod.get_incident(engine, incident_id)
    assert merged is not None
    assert merged.batches == [f"batch_{FAULT_DATE:%Y%m%d}", f"batch_{next_day:%Y%m%d}"]
    assert len(merged.violations) == 2  # one per day, not four
    assert merged.status is IncidentStatus.OPEN
    assert len(incidents_mod.list_incidents(engine)) == 1
