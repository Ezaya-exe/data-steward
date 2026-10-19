"""Contract acceptance tests (Phase 2a) on a THROWAWAY Postgres database.

Same pattern as ``test_faults_e2e.py`` — create/init/drop ``dqs_test`` and
never touch the dev warehouse — but with contract-specific shape: the module
fixture seeds a 30-day clean baseline, generates contracts *from it*, then
loads clean held-out days (2026-01-31…02-04) so the fit and fault-sensitivity
tests profile future data against past-derived bounds. The dev-warehouse
regeneration determinism test is separate (read-only, no throwaway DB) because
it must produce the exact bytes committed in ``contracts/``.

Skipped when Postgres is unreachable; marked ``integration``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs import db as dbmod
from dqs.config import Settings
from dqs.contracts import fitting, profiler, store
from dqs.contracts.generation import generate_all
from dqs.errors import ContractError
from dqs.models.contract import Contract
from dqs.sim import loader, pipeline
from dqs.sim.faults import FK_MAP, parse_fault_specs

pytestmark = pytest.mark.integration

TEST_DB = "dqs_test"
SEED = 42
BASELINE_DAYS = 30
HOLDOUT_DATES = [dt.date(2026, 1, 31) + dt.timedelta(days=i) for i in range(5)]

# One distinct date per fault so every day isolates exactly one fault kind.
# Layout: (label, raw table, date, spec, expected stat findings). Dates advance
# after the held-out window so baseline/held-out days are never faulted.
FAULTS: list[tuple[str, str, dt.date, list[str], set[str]]] = [
    (
        "F1 column_rename",
        "orders",
        dt.date(2026, 2, 10),
        ["column_rename:orders.customer_id:new_name=cust_id"],
        {"schema.missing_column", "schema.unexpected_column"},
    ),
    (
        "F2 column_removed",
        "payments",
        dt.date(2026, 2, 11),
        ["column_removed:payments.method"],
        {"schema.missing_column"},
    ),
    (
        "F3 type_change",
        "orders",
        dt.date(2026, 2, 12),
        ["type_change:orders.total_amount"],
        {"type.logical"},
    ),
    (
        "F4 null_spike",
        "orders",
        dt.date(2026, 2, 13),
        ["null_spike:orders.status:fraction=0.5"],
        {"nulls.rate"},
    ),
    (
        "F5 orphan_keys",
        "payments",
        dt.date(2026, 2, 14),
        ["orphan_keys:payments.order_id:fraction=0.5"],
        {"fk.integrity"},
    ),
    (
        "F6 duplicate_rows",
        "orders",
        dt.date(2026, 2, 15),
        ["duplicate_rows:orders:fraction=0.5"],
        {"unique.pk"},
    ),
    (
        "F7 scale_shift",
        "orders",
        dt.date(2026, 2, 16),
        ["scale_shift:orders.total_amount:factor=100"],
        {"range.bounds(value)", "mean_bounds"},
    ),
    (
        "F9 missing_table",
        "payments",
        dt.date(2026, 2, 17),
        ["missing_table:payments"],
        {"volume.rows"},
    ),
    (
        "F10 column_added",
        "orders",
        dt.date(2026, 2, 18),
        ["column_added:orders:new_column=coupon_code"],
        {"schema.unexpected_column"},
    ),
]
LATE_BATCH_DATE = dt.date(2026, 2, 9)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"


@dataclass
class ContractsEnv:
    """The throwaway warehouse plus the contracts generated from its baseline."""

    settings: Settings
    engine: Engine
    contracts: dict[str, Contract]


@pytest.fixture(scope="module")
def contracts_env(settings: Settings) -> Iterator[ContractsEnv]:
    """Create, seed, generate over, and finally drop the throwaway warehouse."""
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
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEST_DB}"')
        test_settings = settings.model_copy(update={"pg_database": TEST_DB})
        dbmod.init_database(test_settings)
        pipeline.seed_baseline(test_settings, BASELINE_DAYS, SEED, run_dbt=False)
        engine = dbmod.make_rw_engine(test_settings)
        try:
            contracts = {c.table: c for c in generate_all(engine)}  # 30 batches only
            # Held-out clean days arrive AFTER generation: future data vs past rules.
            for day in HOLDOUT_DATES:
                pipeline.load_day(test_settings, day, SEED, run_dbt=False)
            yield ContractsEnv(settings=test_settings, engine=engine, contracts=contracts)
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
    finally:
        admin.dispose()


def _refs_on(engine: Engine, dates: list[dt.date]) -> list[profiler.BatchRef]:
    wanted = set(dates)
    return [ref for ref in profiler.list_batches(engine) if ref.business_date in wanted]


def _raw_contracts(env: ContractsEnv) -> dict[str, Contract]:
    return {t: c for t, c in env.contracts.items() if t.startswith("raw.")}


# --- held-out fit -------------------------------------------------------------------


def test_clean_heldout_days_fit_every_raw_contract(contracts_env: ContractsEnv) -> None:
    """Every clean future-day statistic lies inside the baseline-derived bounds.

    The generator widens tolerance so ordinary variation fits (floor/ceil
    shares, ×1.5 null multiplier, 4σ mean window); should this ever fail, the
    fix goes there — never into weakening the fit assertion.
    """
    env = contracts_env
    refs = _refs_on(env.engine, HOLDOUT_DATES)
    assert len(refs) == len(HOLDOUT_DATES)
    report: dict[str, list[str]] = {}
    for table, contract in sorted(_raw_contracts(env).items(), key=lambda kv: kv[0]):
        profile = profiler.profile_raw_table(env.engine, table.removeprefix("raw."), refs)
        findings = fitting.compare(profile, contract)
        report[table] = [f"{f.stat}:{f.column}" for f in findings]
    assert report == {table: [] for table in report}, "clean held-out days must fit"


def test_heldout_report_names_stats_within_ten_percent_of_a_bound(
    contracts_env: ContractsEnv,
) -> None:
    """Near-bound evidence: margin ≤ NEAR_BOUND_SHARE per stat/batch."""
    env = contracts_env
    refs = _refs_on(env.engine, HOLDOUT_DATES)
    lines: list[str] = []
    for table, contract in sorted(_raw_contracts(env).items(), key=lambda kv: kv[0]):
        profile = profiler.profile_raw_table(env.engine, table.removeprefix("raw."), refs)
        for margin in fitting.margins(profile, contract):
            if fitting.near_bound(margin):
                lines.append(f"{table} {margin.stat}[{margin.column}] margin={margin.margin:.3f}")
    out = REPO_ROOT / "out" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "phase-2a-contract-fit.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    # the report may be empty; near-bound stats are evidence, not a failure mode


# --- fault sensitivity ---------------------------------------------------------------


@pytest.mark.parametrize("fault", FAULTS, ids=[fault[0] for fault in FAULTS])
def test_fault_pushes_a_stat_outside_the_contract_bounds(
    contracts_env: ContractsEnv, fault: tuple[str, str, dt.date, list[str], set[str]]
) -> None:
    label, table, date, specs, expected = fault
    env = contracts_env
    result = pipeline.load_day(
        env.settings, date, SEED, run_dbt=False, specs=parse_fault_specs(specs)
    )
    if result["rows"].get(table) is not None:
        # F9 removes the whole table: the missing count IS the fault
        assert result["rows"][table] > 0, f"{label}: faulted day loaded no rows"
    profile = profiler.profile_raw_table(env.engine, table, _refs_on(env.engine, [date]))
    stats = {finding.stat for finding in fitting.compare(profile, env.contracts[f"raw.{table}"])}
    missing = expected - stats
    assert not missing, f"{label}: expected findings {sorted(missing)}, saw {sorted(stats)}"


def test_late_batch_is_invisible_to_stat_bounds_but_breaks_the_sla(
    contracts_env: ContractsEnv,
) -> None:
    """F8 mutates only arrival time: no stat moves (it is freshness's job)."""
    env = contracts_env
    pipeline.load_day(
        env.settings,
        LATE_BATCH_DATE,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(["late_batch:payments:hours=24"]),
    )
    profile = profiler.profile_raw_table(
        env.engine, "payments", _refs_on(env.engine, [LATE_BATCH_DATE])
    )
    findings = fitting.compare(profile, env.contracts["raw.payments"])
    report = [f"{f.stat}:{f.column}" for f in findings]
    # F8 invisible to stat bounds — documented deviation, never a test fix
    assert report == []
    # the delay it did cause is exactly what the freshness check (Phase 2b) sees
    with env.engine.connect() as conn:
        arrived = conn.execute(
            text("SELECT arrived_at FROM dq.batches WHERE batch_id = :b"),
            {"b": loader.batch_id_for_date(LATE_BATCH_DATE)},
        ).scalar_one()
    delay_minutes = (arrived - loader.arrived_at_for(LATE_BATCH_DATE)).total_seconds() / 60
    sla = env.contracts["raw.payments"].freshness.max_delay_minutes
    assert delay_minutes > sla


# --- artifact facts (no warehouse needed) --------------------------------------------


def test_contract_files_match_the_fault_catalog_fk_map() -> None:
    """Every FK_MAP pair is reproduced by the contract's references (exact match)."""
    contracts = store.load_contracts(CONTRACTS_DIR)
    for child, parent in sorted(FK_MAP.items()):
        table, column = child.split(".", 1)
        reference = contracts[f"raw.{table}"].columns[column].references
        assert reference is not None, f"{child}: contract declares no reference"
        parent_schema, parent_table, parent_column = parent.split(".")
        assert (reference.table, reference.column) == (
            f"{parent_schema}.{parent_table}",
            parent_column,
        )


def test_thirteen_contract_files_exist_and_validate() -> None:
    """The committed catalog holds the 5 raw + 8 staged contracts, all valid."""
    contracts = store.load_contracts(CONTRACTS_DIR)
    expected = {
        "raw.customers",
        "raw.products",
        "raw.orders",
        "raw.order_items",
        "raw.payments",
        "staging.stg_customers",
        "staging.stg_orders",
        "staging.stg_order_items",
        "staging.stg_payments",
        "staging.stg_products",
        "marts.dim_customers",
        "marts.fct_orders",
        "marts.fct_daily_revenue",
    }
    assert len(contracts) == 13
    assert set(contracts) == expected


def test_unknown_keys_are_rejected_when_loading(tmp_path: Path) -> None:
    """A typo'd or extra key must fail loudly (§7.1), never be dropped silently."""
    corrupt = tmp_path / "bad.yml"
    corrupt.write_text(
        (CONTRACTS_DIR / "raw.payments.yml").read_text(encoding="utf-8") + "\nbogus_key: hmm\n",
        encoding="utf-8",
    )
    with pytest.raises(ContractError):
        store.load_contract(corrupt)


# --- dev-warehouse regeneration determinism -------------------------------------------


def _without_status(data: bytes) -> bytes:
    """Drop the `status:` line: approval is the one allowed diff (DECISIONS 44).

    Generation always writes `status: draft`; the committed catalog has been
    approved by the human owner. Everything else — every bound, tolerance,
    ordering, float formatting and line ending — must still be byte-identical,
    so the determinism guarantee is asserted on the remaining bytes.
    """
    return b"".join(
        line for line in data.splitlines(keepends=True) if not line.startswith(b"status: ")
    )


def test_regenerated_contracts_are_byte_identical_to_committed(
    ro_engine: Engine, tmp_path: Path
) -> None:
    """Two fresh regenerations over the dev baseline yield identical bytes.

    Compared to the committed catalog ignoring only the `status:` line, because
    the owner approves contracts in place (docs/DECISIONS.md 44).
    """
    runs: list[Path] = []
    for index in range(2):
        directory = tmp_path / f"run{index}"
        for contract in generate_all(ro_engine):
            store.save(contract, directory)
        runs.append(directory)
    first = {p.name: p.read_bytes() for p in sorted(runs[0].glob("*.yml"))}
    second = {p.name: p.read_bytes() for p in sorted(runs[1].glob("*.yml"))}
    assert first == second, "two runs over one baseline diverged"
    committed = {p.name: p.read_bytes() for p in sorted(CONTRACTS_DIR.glob("*.yml"))}
    assert set(first) == set(committed), "regenerated and committed file sets differ"
    differing = {
        name: (first[name], committed[name])
        for name in first
        if _without_status(first[name]) != _without_status(committed[name])
    }
    assert not differing, (
        "regenerated files differ from the committed contracts beyond the status line: "
        f"{sorted(differing)}"
    )
    # Guard the assumption: the committed catalog really is approved everywhere.
    assert all(b"\nstatus: approved\n" in data for data in committed.values())
