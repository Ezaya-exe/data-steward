"""Shared plumbing for the check engine (docs/ARCHITECTURE.md §7.3).

Why a context object: every check is a tiny class with `name`, `severity` and
`run(ctx)`. The context carries exactly what a check may read — the loaded
contract, the profiler's facts for the batch (raw) or snapshot (staging/marts)
and the batch clock — so checks stay pure functions over already-computed data
and only the few that must (unique across batches, FK anti-joins) touch the DB.

The raw `BatchTableProfile` is produced by `dqs.contracts.profiler`, the module
the contract generator uses: the engine re-parses nothing itself, so detection
can never drift from generation (§7.3, docs/DECISIONS.md).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import Engine

from dqs.contracts import profiler
from dqs.models.contract import Contract
from dqs.models.enums import Severity
from dqs.models.violation import Violation

# Loader-owned metadata columns never appear in contract comparisons (§7.3: the
# raw header already excludes them by DECISIONS 23, the staged header does not).
IGNORED_COLUMNS: frozenset[str] = frozenset(profiler.SYSTEM_COLUMNS)


@dataclass(frozen=True)
class CheckContext:
    """Everything one check may read for one (contract, batch|snapshot).

    `engine` is optional so table checks that need no DB access (schema, type,
    nulls, range, volume) are pure and unit-testable without Postgres; only the
    cross-batch / anti-join paths use it and fall back to profile facts first.
    """

    engine: Engine | None
    contract: Contract | None = None
    profile: profiler.BatchTableProfile | None = None
    batch_id: str | None = None
    business_date: dt.date | None = None
    arrived_at: dt.datetime | None = None
    as_of: dt.datetime | None = None


class Check(Protocol):
    """One §7.3 check: a name, a nominal severity, and a `run` over a context."""

    name: str
    severity: Severity

    def run(self, ctx: CheckContext) -> list[Violation]: ...


def present_columns(ctx: CheckContext) -> set[str]:
    """Columns actually delivered in the batch header (empty when absent).

    Value-level checks iterate `present_columns ∩ contract.columns` only, so a
    column missing from the header is reported *solely* by
    `schema.missing_column` and never cascades into null/type/range noise
    (docs/DECISIONS.md Phase 2b).
    """
    header = ctx.profile.header if ctx.profile is not None else None
    return set(header) if header is not None else set()


def bare_table(qualified: str) -> str:
    """`raw.orders` -> `orders` (the profiler/SQL helpers take bare raw names)."""
    return qualified.split(".", maxsplit=1)[-1]


def raw_profile(
    engine: Engine, bare: str, batch_id: str, business_date: dt.date
) -> profiler.BatchTableProfile:
    """The profiler's per-batch facts for one raw table (no duplicate parsing)."""
    ref = profiler.BatchRef(batch_id=batch_id, business_date=business_date)
    table_profile = profiler.profile_raw_table(engine, bare, [ref])
    return table_profile.batches[0]
