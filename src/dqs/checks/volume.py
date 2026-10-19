"""Volume check: row count outside the window, or a required table absent (§7.3).

Absence and emptiness are deliberately different: a table missing from
`dq.batch_tables` (F9) is a violation only when the contract says
`required_in_batch: true` (docs/DECISIONS.md Phase 2b) — an optional dimension
that simply had nothing new to send is not a fault. A *present* table is judged
by `[min_rows, max_rows]` alone, except the baseline's day-1 initial load, which
the contract records as excluded from the window (DECISIONS 36).
"""

from __future__ import annotations

from dqs.checks.base import CheckContext
from dqs.models.enums import Severity
from dqs.models.violation import Violation


def _is_excluded_initial_load(ctx: CheckContext) -> bool:
    """True for the batch the volume window was derived *without* (DECISIONS 36).

    The generator omits the day-1 initial load when it fits dimension volume
    bounds and records that with `volume.initial_load_excluded`; enforcement then
    skips the one batch whose business date is the baseline's first date.
    """
    if ctx.contract is None or ctx.business_date is None:
        return False
    volume = ctx.contract.volume
    return bool(volume.initial_load_excluded) and (
        ctx.business_date.isoformat() == ctx.contract.baseline.first
    )


class VolumeRowsCheck:
    """Row count outside `[min_rows, max_rows]`, or a required table absent (§7.3)."""

    name = "volume.rows"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.profile is None or ctx.contract is None:
            return []
        volume = ctx.contract.volume
        if not ctx.profile.present:
            if not volume.required_in_batch:
                return []
            return [
                Violation(
                    check=self.name,
                    severity=self.severity,
                    table=ctx.contract.table,
                    batch_id=ctx.batch_id,
                    message=f"required table {ctx.contract.table} is absent from the batch",
                    observed={"present": False},
                    expected={"required_in_batch": True},
                )
            ]
        if _is_excluded_initial_load(ctx):
            return []
        rows = ctx.profile.row_count
        if volume.min_rows <= rows <= volume.max_rows:
            return []
        return [
            Violation(
                check=self.name,
                severity=self.severity,
                table=ctx.contract.table,
                batch_id=ctx.batch_id,
                message=(
                    f"row count {rows} is outside the contract window "
                    f"[{volume.min_rows}, {volume.max_rows}]"
                ),
                observed={"row_count": rows},
                expected={"min_rows": volume.min_rows, "max_rows": volume.max_rows},
            )
        ]
