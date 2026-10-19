"""Null-rate check: nulls above the contract's allowance (§7.3).

Severity follows the contract's `nullable` flag: a non-nullable column has zero
tolerance (breaking), a nullable column allows up to `max_null_rate` (warning).
The rate is over the batch's rows; an empty table has rate 0 and never fires.
A column missing from the header is skipped here — `schema.missing_column`
reports it (docs/DECISIONS.md Phase 2b), which is exactly the F1/F2 non-cascade.
"""

from __future__ import annotations

from dqs.checks.base import CheckContext, present_columns
from dqs.models.enums import Severity
from dqs.models.violation import Violation


class NullRateCheck:
    """Null rate above the column's allowance (§7.3)."""

    name = "nulls.rate"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.profile is None or ctx.profile.row_count == 0 or ctx.contract is None:
            return []
        columns = present_columns(ctx) & set(ctx.contract.columns)
        violations: list[Violation] = []
        for column in sorted(columns):
            stats = ctx.profile.column_stats.get(column)
            if stats is None:
                continue
            spec = ctx.contract.columns[column]
            bound = 0.0 if not spec.nullable else (spec.max_null_rate or 0.0)
            rate = stats.null_count / ctx.profile.row_count
            if rate > bound:
                severity = Severity.BREAKING if not spec.nullable else Severity.WARNING
                violations.append(
                    Violation(
                        check=self.name,
                        severity=severity,
                        table=ctx.contract.table,
                        column=column,
                        batch_id=ctx.batch_id,
                        message=(
                            f"null rate {rate:.2%} exceeds the allowed {bound:.2%} "
                            f"for {'non-nullable' if not spec.nullable else 'nullable'} "
                            f"column {column!r}"
                        ),
                        observed={
                            "null_count": stats.null_count,
                            "row_count": ctx.profile.row_count,
                        },
                        expected={"max_null_rate": bound, "nullable": spec.nullable},
                    )
                )
        return violations
