"""Volume check: row count outside the window, or a required table absent (§7.3).

Absence and emptiness are deliberately different: a table missing from
`dq.batch_tables` (F9) is a violation only when the contract says
`required_in_batch: true` (docs/DECISIONS.md Phase 2b) — an optional dimension
that simply had nothing new to send is not a fault. A *present* table is judged
by `[min_rows, max_rows]` alone.
"""

from __future__ import annotations

from dqs.checks.base import CheckContext
from dqs.models.enums import Severity
from dqs.models.violation import Violation


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
