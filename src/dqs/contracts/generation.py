"""One-pass contract generation over the current warehouse baseline.

Why a separate module: the CLI must stay presentation-only (Phase 1 rule)
and tests reuse this exact path, so acceptance numbers and CLI output can
never drift. Generates contracts for (a) every raw table that appears in
`dq.batch_tables` — batch-level facts per ARCHITECTURE §7.2 — and (b) every
staging/marts base table that exists right now — snapshot facts, because dbt
owns those tables and rebuilds them whole.
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from dqs.contracts import profiler
from dqs.contracts.generator import Window, generate_contract
from dqs.errors import ContractError
from dqs.models.contract import Contract


def raw_table_names(engine: Engine) -> list[str]:
    """Tables that appeared in any batch, deterministic order."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT table_name FROM dq.batch_tables ORDER BY table_name")
        ).fetchall()
    return [str(r[0]) for r in rows]


def window_of(batches: list[profiler.BatchRef]) -> Window:
    """First→last business date of `dq.batches` (deterministic content source)."""
    if not batches:
        raise ContractError("dq.batches has no rows: seed the baseline first")
    return Window(first=batches[0].business_date, last=batches[-1].business_date)


def generate_raw_contracts(engine: Engine) -> list[Contract]:
    """One contract per raw table profiled from the current batches."""
    batches = profiler.list_batches(engine)
    window = window_of(batches)
    contracts: list[Contract] = []
    for table in raw_table_names(engine):
        profile = profiler.profile_raw_table(engine, table, batches)
        contracts.append(generate_contract(profile, window))
    return contracts


def generate_staged_contracts(engine: Engine) -> list[Contract]:
    """One contract per existing staging/marts base table (snapshot facts)."""
    window = None
    contracts: list[Contract] = []
    for ref in profiler.discover_staged_tables(engine):
        profile = profiler.profile_table_state(engine, ref)
        if window is None:
            window = window_of(profiler.list_batches(engine))
        contracts.append(generate_contract(profile, window))
    if window is None:
        raise ContractError("dq.batches has no rows: seed the baseline first")
    return contracts


def generate_all(engine: Engine) -> list[Contract]:
    """All 13 contracts when the baseline (30 raw batches) is present."""
    return generate_raw_contracts(engine) + generate_staged_contracts(engine)
