"""Schema-drift checks: a contract column missing, or an extra column (§7.3).

The observed header is the batch's truth (`dq.batch_tables.columns`), never the
DB table definition — schema evolution means the table keeps columns an
individual batch never delivered (docs/DECISIONS.md 23). Loader-owned metadata
columns are ignored on the observed side, so staged snapshots (whose header
includes `_batch_id`/`_loaded_at`) do not raise spurious warnings.
"""

from __future__ import annotations

from dqs.checks.base import IGNORED_COLUMNS, CheckContext
from dqs.models.enums import Severity
from dqs.models.violation import Violation


def _observed(ctx: CheckContext) -> set[str] | None:
    profile = ctx.profile
    # An absent table (F9) has no header to compare: leave it entirely to
    # `volume.rows`, so a missing table never cascades into one
    # `schema.missing_column` per contract column (docs/DECISIONS.md Phase 2b).
    if profile is None or not profile.present or profile.header is None:
        return None
    return set(profile.header) - IGNORED_COLUMNS


class MissingColumnCheck:
    """A contract column absent from this batch's header — breaking (§7.3)."""

    name = "schema.missing_column"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        observed = _observed(ctx)
        if observed is None or ctx.contract is None:
            return []
        missing = sorted(set(ctx.contract.columns) - observed)
        return [
            Violation(
                check=self.name,
                severity=self.severity,
                table=ctx.contract.table,
                column=column,
                batch_id=ctx.batch_id,
                message=f"contract column {column!r} is missing from the batch header",
                observed={"header": sorted(observed)},
                expected={"column": column},
            )
            for column in missing
        ]


class UnexpectedColumnCheck:
    """An observed column the contract does not know — warning (§7.3)."""

    name = "schema.unexpected_column"
    severity = Severity.WARNING

    def run(self, ctx: CheckContext) -> list[Violation]:
        observed = _observed(ctx)
        if observed is None or ctx.contract is None:
            return []
        unexpected = sorted(observed - set(ctx.contract.columns))
        return [
            Violation(
                check=self.name,
                severity=self.severity,
                table=ctx.contract.table,
                column=column,
                batch_id=ctx.batch_id,
                message=f"column {column!r} is not declared in the contract",
                observed={"header": sorted(observed)},
                expected={"column": "declared in contract"},
            )
            for column in unexpected
        ]
