"""Referential-integrity check: orphans above the tolerance (§7.3).

FK health is the share of **non-null** child values with no matching parent
(nulls are a null-rate concern, DECISIONS 29), measured with the profiler's SQL
anti-join — the engine reuses `profiler.fk_orphan_rate` rather than writing its
own query, so detection and contract generation agree on the definition. The
per-batch rate is preferred from the profile (already computed for known FK
columns); the direct anti-join is the fallback for hand-written references.
"""

from __future__ import annotations

from dqs.checks.base import CheckContext, bare_table, present_columns
from dqs.contracts import profiler
from dqs.contracts.fitting import FK_ORPHAN_MAX
from dqs.models.enums import Severity
from dqs.models.violation import Violation


class FkIntegrityCheck:
    """Orphan rate above 0.5% against the parent table (§7.3)."""

    name = "fk.integrity"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.profile is None or ctx.contract is None:
            return []
        columns = present_columns(ctx) & set(ctx.contract.columns)
        violations: list[Violation] = []
        for column in sorted(columns):
            spec = ctx.contract.columns[column]
            reference = spec.references
            if reference is None:
                continue
            stats = ctx.profile.column_stats.get(column)
            rate = stats.orphan_rate if stats is not None else None
            if rate is None and ctx.engine is not None and ctx.batch_id is not None:
                rate = profiler.fk_orphan_rate(
                    ctx.engine,
                    bare_table(ctx.contract.table),
                    column,
                    bare_table(reference.table),
                    reference.column,
                    batch_id=ctx.batch_id,
                )
            if rate is None or rate <= FK_ORPHAN_MAX:
                continue
            violations.append(
                Violation(
                    check=self.name,
                    severity=self.severity,
                    table=ctx.contract.table,
                    column=column,
                    batch_id=ctx.batch_id,
                    message=(
                        f"orphan rate {rate:.2%} exceeds {FK_ORPHAN_MAX:.1%} for "
                        f"{column!r} -> {reference.table}.{reference.column}"
                    ),
                    observed={"orphan_rate": round(rate, 6)},
                    expected={"max_orphan_rate": FK_ORPHAN_MAX, "parent": reference.table},
                )
            )
        return violations
