"""Simulation orchestration: generate → load → dbt build → record.

Why here: the CLI would otherwise contain the seed/day loop logic, which is
hard to unit-test; the loop itself is deterministic and small, while CLI keeps
only presentation. ``dqs db reset`` must be run first for a clean base —
duplicate batches are refused by the loader by design (determinism).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from dqs import db as dbmod
from dqs import dbt_run
from dqs.config import Settings
from dqs.models.faults import FaultSpec
from dqs.sim import faults as faults_mod
from dqs.sim import generator, loader

BASELINE_START: Final = generator.BASELINE_START_DATE


def seed_baseline(
    settings: Settings, days: int, seed: int, run_dbt: bool = True
) -> list[dict[str, Any]]:
    """Load ``days`` clean baseline batches (initial dimension load on day 1)."""
    results: list[dict[str, Any]] = []
    engine = dbmod.make_rw_engine(settings)
    try:
        for index in range(days):
            business_date = BASELINE_START + dt.timedelta(days=index)
            batch = generator.tables_for_date(business_date, seed)
            counts = loader.load_batch(engine, business_date, batch)
            dbt_code: int | None = None
            if run_dbt:
                dbt_code, _out = dbt_run.run_dbt_build(
                    settings, target="dev", batch_id=loader.batch_id_for_date(business_date)
                )
            results.append({"date": business_date, "rows": counts, "dbt_exit": dbt_code})
    finally:
        engine.dispose()
    return results


def load_day(
    settings: Settings,
    business_date: dt.date,
    seed: int,
    run_dbt: bool = True,
    specs: list[FaultSpec] | None = None,
) -> dict[str, Any]:
    """Generate one day's batch, apply fault specs, load it, build dbt + record.

    Faults mutate the generated frames only (and arrival time for F8) — the
    generator itself is never touched, so skipped/other days stay identical.
    """
    batch = generator.tables_for_date(business_date, seed)
    faulted = faults_mod.apply_faults(batch, specs or [], business_date, seed)
    engine = dbmod.make_rw_engine(settings)
    try:
        counts = loader.load_batch(
            engine, business_date, faulted.tables, arrived_at=faulted.arrived_at
        )
    finally:
        engine.dispose()
    dbt_code: int | None = None
    if run_dbt:
        dbt_code, _out = dbt_run.run_dbt_build(
            settings, target="dev", batch_id=loader.batch_id_for_date(business_date)
        )
    return {
        "rows": counts,
        "dbt_exit": dbt_code,
        "ground_truth": faulted.ground_truth,
        "arrived_at": faulted.arrived_at,
    }
