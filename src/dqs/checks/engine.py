"""The check engine: contract → violations, per batch (raw) or snapshot (staged).

Why this shape: `CheckEngine.run(batch_id)` evaluates every approved raw
contract for one `dq.batches` row and returns a flat `list[Violation]` — the
exact exchange `docs/ARCHITECTURE.md §7.3` describes. `run_table(table)` does
the same for a staging/marts whole-table snapshot (the scope Phase 5's fix
verification reuses). It enforces **only approved contracts** and warns loudly
when none exist, because a draft contract is a hypothesis, not a rule.

It builds every raw profile through `dqs.contracts.profiler` and compares only
columns present in the observed header, so value-level checks never cascade off
a `schema.missing_column` (docs/DECISIONS.md Phase 2b).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from sqlalchemy import Engine, text

from dqs.checks.base import Check, CheckContext, bare_table, raw_profile
from dqs.checks.fk import FkIntegrityCheck
from dqs.checks.freshness import FreshnessSlaCheck
from dqs.checks.nulls import NullRateCheck
from dqs.checks.ranges import RangeBoundsCheck
from dqs.checks.schema import MissingColumnCheck, UnexpectedColumnCheck
from dqs.checks.types import LogicalTypeCheck
from dqs.checks.unique import UniquePkCheck
from dqs.checks.volume import VolumeRowsCheck
from dqs.contracts import profiler, store
from dqs.errors import CheckError
from dqs.models.contract import Contract
from dqs.models.enums import Layer
from dqs.models.violation import Violation

logger = logging.getLogger(__name__)

# Every §7.3 table check. Batch-level freshness is run once per batch by the
# engine (it is not tied to a table); monitor.missing_batch is run by the
# monitor, which owns the `as_of` window.
DEFAULT_TABLE_CHECKS: tuple[Check, ...] = (
    MissingColumnCheck(),
    UnexpectedColumnCheck(),
    LogicalTypeCheck(),
    NullRateCheck(),
    UniquePkCheck(),
    FkIntegrityCheck(),
    RangeBoundsCheck(),
    VolumeRowsCheck(),
)

STAGED_SCHEMA: dict[Layer, str] = {Layer.STAGING: "staging", Layer.MARTS: "marts"}


class CheckEngine:
    """Runs the §7.3 checks for one warehouse against a set of contracts."""

    def __init__(
        self,
        engine: Engine,
        contracts: Mapping[str, Contract],
        checks: Sequence[Check] | None = None,
    ) -> None:
        self._engine = engine
        self._contracts = dict(contracts)
        self._checks = tuple(checks) if checks is not None else DEFAULT_TABLE_CHECKS
        self._freshness: Check = FreshnessSlaCheck()

    @classmethod
    def from_directory(
        cls, engine: Engine, directory: Path, checks: Sequence[Check] | None = None
    ) -> CheckEngine:
        """Build an engine from the approved contracts in `directory` (§7.1)."""
        contracts = store.load_approved(directory)
        if not contracts:
            logger.warning(
                "no approved contracts in %s - the check engine will enforce nothing; "
                "run 'dqs contracts approve --all' first",
                directory,
            )
        return cls(engine, contracts, checks)

    @property
    def contracts(self) -> dict[str, Contract]:
        """The approved contracts this engine enforces."""
        return dict(self._contracts)

    def run(self, batch_id: str, as_of: dt.datetime | None = None) -> list[Violation]:
        """Every violation for one raw batch across all approved raw contracts.

        `as_of` is accepted for the §7.3 signature; the per-batch checks are
        driven by the batch's own clock (`business_date`/`arrived_at`), so it is
        only consumed by the monitor's separate missing-batch pass.
        """
        meta = self._batch_meta(batch_id)
        if meta is None:
            raise CheckError(f"no batch {batch_id!r} in dq.batches")
        business_date, arrived_at = meta
        raw = sorted(
            (c for c in self._contracts.values() if c.layer is Layer.RAW),
            key=lambda c: c.table,
        )
        violations: list[Violation] = []
        for contract in raw:
            profile = raw_profile(self._engine, bare_table(contract.table), batch_id, business_date)
            ctx = CheckContext(
                engine=self._engine,
                contract=contract,
                profile=profile,
                batch_id=batch_id,
                business_date=business_date,
                arrived_at=arrived_at,
            )
            violations.extend(_run(self._checks, ctx))
        if raw:  # freshness is batch-level: reported once, not once per table
            deadline_ctx = CheckContext(
                engine=self._engine,
                contract=raw[0],
                batch_id=batch_id,
                business_date=business_date,
                arrived_at=arrived_at,
            )
            violations.extend(self._freshness.run(deadline_ctx))
        return violations

    def run_table(self, table: str) -> list[Violation]:
        """Every violation for one staging/marts snapshot contract (table scope)."""
        contract = self._contracts.get(table)
        if contract is None:
            raise CheckError(f"no approved contract for {table!r}")
        if contract.layer is Layer.RAW:
            raise CheckError(f"{table!r} is a raw contract; use run() for batch scope")
        ref = profiler.StagedTableRef(
            layer=contract.layer,
            schema_name=STAGED_SCHEMA[contract.layer],
            table=bare_table(contract.table),
        )
        profile = profiler.profile_table_state(self._engine, ref)
        ctx = CheckContext(engine=self._engine, contract=contract, profile=profile.batches[0])
        return _run(self._checks, ctx)

    def _batch_meta(self, batch_id: str) -> tuple[dt.date, dt.datetime] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT business_date, arrived_at FROM dq.batches WHERE batch_id = :b"),
                {"b": batch_id},
            ).fetchone()
        if row is None:
            return None
        return row[0], row[1]


def _run(checks: Sequence[Check], ctx: CheckContext) -> list[Violation]:
    violations: list[Violation] = []
    for check in checks:
        violations.extend(check.run(ctx))
    return violations
