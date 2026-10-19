"""Range / mean check: values or the batch mean outside the contract (§7.3).

One check, two signals. A batch **mean** outside `mean_bounds` means the whole
distribution moved (unit/scale change, F7) and is breaking; an **isolated**
value outside `range` while the mean still holds is a warning (a single outlier,
not a systematic change). Values come from the profiler's numeric parse over
non-null values, so a column whose values are all unparseable (F3 currency text)
contributes no min/max/mean and is left entirely to `type.logical`.
"""

from __future__ import annotations

from dqs.checks.base import CheckContext, present_columns
from dqs.models.enums import Severity
from dqs.models.violation import Violation

# A mean needs a sample: over a single non-null value the batch "mean" *is* that
# value, so `mean_bounds` cannot tell a distribution shift (F7) from one odd row
# — suppress the mean signal for tiny batches to avoid a false positive.
_MIN_MEAN_SAMPLE = 2


def _outside(low: float, high: float, value: float) -> bool:
    return value < low or value > high


class RangeBoundsCheck:
    """A value outside `range`, or the batch mean outside `mean_bounds` (§7.3)."""

    name = "range.bounds"
    severity = Severity.BREAKING  # the mean case; isolated values are warnings

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.profile is None or ctx.profile.row_count == 0 or ctx.contract is None:
            return []
        columns = present_columns(ctx) & set(ctx.contract.columns)
        violations: list[Violation] = []
        for column in sorted(columns):
            spec = ctx.contract.columns[column]
            if spec.range is None and spec.mean_bounds is None:
                continue
            stats = ctx.profile.column_stats.get(column)
            if stats is None:
                continue
            observed = {
                "min": stats.min_value,
                "max": stats.max_value,
                "mean": stats.mean_value,
            }
            if (
                spec.mean_bounds is not None
                and stats.mean_value is not None
                and stats.non_null >= _MIN_MEAN_SAMPLE
            ):
                bounds = spec.mean_bounds
                if _outside(bounds.min, bounds.max, stats.mean_value):
                    violations.append(
                        Violation(
                            check=self.name,
                            severity=Severity.BREAKING,
                            table=ctx.contract.table,
                            column=column,
                            batch_id=ctx.batch_id,
                            message=(
                                f"batch mean {stats.mean_value} is outside mean_bounds "
                                f"[{bounds.min}, {bounds.max}]"
                            ),
                            observed=observed,
                            expected={"mean_min": bounds.min, "mean_max": bounds.max},
                        )
                    )
                    continue
            if (
                spec.range is not None
                and stats.min_value is not None
                and stats.max_value is not None
            ):
                bounds = spec.range
                if _outside(bounds.min, bounds.max, stats.min_value) or _outside(
                    bounds.min, bounds.max, stats.max_value
                ):
                    violations.append(
                        Violation(
                            check=self.name,
                            severity=Severity.WARNING,
                            table=ctx.contract.table,
                            column=column,
                            batch_id=ctx.batch_id,
                            message=(
                                f"values span [{stats.min_value}, {stats.max_value}] outside "
                                f"range [{bounds.min}, {bounds.max}] (isolated)"
                            ),
                            observed=observed,
                            expected={"min": bounds.min, "max": bounds.max},
                        )
                    )
        return violations
