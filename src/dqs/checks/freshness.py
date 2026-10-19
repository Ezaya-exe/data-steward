"""Freshness and missing-batch checks — both batch-level, not per table (§7.3).

Freshness is measured on `dq.batches.arrived_at` against
`business_date + 1 day + max_delay_minutes` (docs/DECISIONS.md 37): F8 changes
only arrival time, so no per-table statistic can see it and only this check
fires. Missing-batch walks every business date from the first loaded batch up
to `as_of.date − 1` and flags the ones with no row — the second half of the
LATE_OR_MISSING_BATCH story, and the reason `dqs monitor run` needs an `as_of`.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Engine, text

from dqs.checks.base import CheckContext
from dqs.models.enums import Severity
from dqs.models.violation import Violation


class FreshnessSlaCheck:
    """`arrived_at` later than `business_date + 1d + max_delay_minutes` (§7.3)."""

    name = "freshness.sla"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.business_date is None or ctx.arrived_at is None or ctx.contract is None:
            return []
        sla_minutes = ctx.contract.freshness.max_delay_minutes
        deadline = dt.datetime.combine(
            ctx.business_date + dt.timedelta(days=1),
            dt.time(0, 0),
            tzinfo=dt.UTC,
        ) + dt.timedelta(minutes=sla_minutes)
        if ctx.arrived_at <= deadline:
            return []
        delay_minutes = int((ctx.arrived_at - deadline).total_seconds() // 60) + sla_minutes
        return [
            Violation(
                check=self.name,
                severity=self.severity,
                table=None,
                column=None,
                batch_id=ctx.batch_id,
                message=(
                    f"batch {ctx.batch_id} arrived {delay_minutes} min after the scheduled "
                    f"time (SLA {sla_minutes} min after business_date + 1 day)"
                ),
                observed={"arrived_at": ctx.arrived_at.isoformat()},
                expected={
                    "business_date": ctx.business_date.isoformat(),
                    "deadline": deadline.isoformat(),
                },
            )
        ]


class MissingBatchCheck:
    """A business date with no batch row, up to `as_of.date − 1` (§7.3)."""

    name = "monitor.missing_batch"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.engine is None or ctx.as_of is None:
            return []
        present = _business_dates(ctx.engine)
        if not present:
            return []
        last_expected = ctx.as_of.date() - dt.timedelta(days=1)
        first = min(present)
        expected = _date_range(first, last_expected)
        missing = sorted(set(expected) - present)
        return [
            Violation(
                check=self.name,
                severity=self.severity,
                table=None,
                column=None,
                batch_id=None,
                message=f"no batch row for business date {day.isoformat()}",
                observed={"business_date": day.isoformat()},
                expected={"monitored_through": last_expected.isoformat()},
            )
            for day in missing
        ]


def _date_range(first: dt.date, last: dt.date) -> list[dt.date]:
    if last < first:
        return []
    return [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]


def _business_dates(engine: Engine) -> set[dt.date]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT business_date FROM dq.batches")).fetchall()
    return {row[0] for row in rows}
