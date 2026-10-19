"""Logical-type check: non-null values must parse as the contract type (§7.3).

The parse rate comes from the profiler, which uses the shared
`dqs.contracts.parsing` module — the same rules contract generation used to
infer the type, so "does 1,299.50 parse as numeric?" is answered identically on
both sides. An all-null column (or a staged column with typed values and no
TEXT parse) yields no rate and is skipped: nulls are `nulls.rate`'s concern.
"""

from __future__ import annotations

from dqs.checks.base import CheckContext, present_columns
from dqs.contracts.fitting import TYPE_PARSE_MIN
from dqs.models.enums import Severity
from dqs.models.violation import Violation


class LogicalTypeCheck:
    """Fewer than 99% of non-null values parse as the contract type (§7.3)."""

    name = "type.logical"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.profile is None or ctx.contract is None:
            return []
        columns = present_columns(ctx) & set(ctx.contract.columns)
        violations: list[Violation] = []
        for column in sorted(columns):
            stats = ctx.profile.column_stats.get(column)
            if stats is None:
                continue
            logical = ctx.contract.columns[column].logical_type
            rate = stats.parse_rates.get(logical.value)
            if rate is None:  # no non-null values, or a typed (staged) column
                continue
            if rate < TYPE_PARSE_MIN:
                violations.append(
                    Violation(
                        check=self.name,
                        severity=self.severity,
                        table=ctx.contract.table,
                        column=column,
                        batch_id=ctx.batch_id,
                        message=(
                            f"only {rate:.2%} of non-null values parse as "
                            f"{logical.value} (need ≥ {TYPE_PARSE_MIN:.0%})"
                        ),
                        observed={"parse_rate": round(rate, 6), "non_null": stats.non_null},
                        expected={"logical_type": logical.value, "min_parse_rate": TYPE_PARSE_MIN},
                    )
                )
        return violations
