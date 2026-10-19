"""Uniqueness check for contract-unique columns (§7.3).

Two failure modes: a key repeated *within* the batch (F6 replays rows verbatim)
and a key colliding with *already-loaded rows* (an id reused across days). The
within-batch part is pure — the profiler already computed `distinct == non_null`
for the slice; the cross-batch part is one EXISTS query against the same raw
table (batch-scope only — a staged/marts contract is a whole-table snapshot, so
its within-snapshot uniqueness is already the whole-table answer).
"""

from __future__ import annotations

from sqlalchemy import Engine, text

from dqs.checks.base import CheckContext, bare_table, present_columns
from dqs.contracts.profiler import IDENTIFIER_RE, RAW_SCHEMA
from dqs.errors import DatabaseError
from dqs.models.enums import Layer, Severity
from dqs.models.violation import Violation


def _validated(name: str) -> str:
    if IDENTIFIER_RE.match(name) is None:
        raise DatabaseError(f"identifier {name!r} is not permitted in SQL")
    return name


def _duplicate_values(engine: Engine, qualified: str, batch_id: str, column: str) -> list[str]:
    """Up to five values of `column` that collide inside this raw batch."""
    table = _validated(bare_table(qualified))
    col = _validated(column)
    sql = text(
        f'SELECT "{col}" FROM "{RAW_SCHEMA}"."{table}" WHERE "_batch_id" = :b '
        f'GROUP BY "{col}" HAVING count(*) > 1 ORDER BY "{col}" LIMIT 5'
    )
    with engine.connect() as conn:
        return [str(row[0]) for row in conn.execute(sql, {"b": batch_id})]


def _cross_batch_collisions(
    engine: Engine, qualified: str, batch_id: str, column: str
) -> list[str]:
    """Up to five batch values of `column` that also exist in another batch."""
    table = _validated(bare_table(qualified))
    col = _validated(column)
    sql = text(
        f'SELECT t."{col}" FROM "{RAW_SCHEMA}"."{table}" t '
        f'WHERE t."_batch_id" = :b AND EXISTS ('
        f'  SELECT 1 FROM "{RAW_SCHEMA}"."{table}" o '
        f'  WHERE o."{col}" = t."{col}" AND o."_batch_id" <> :b) '
        f'ORDER BY t."{col}" LIMIT 5'
    )
    with engine.connect() as conn:
        return [str(row[0]) for row in conn.execute(sql, {"b": batch_id})]


class UniquePkCheck:
    """A contract-unique column repeats within the batch or across batches (§7.3)."""

    name = "unique.pk"
    severity = Severity.BREAKING

    def run(self, ctx: CheckContext) -> list[Violation]:
        if ctx.profile is None or ctx.contract is None:
            return []
        contract = ctx.contract
        columns = present_columns(ctx) & set(contract.columns)
        violations: list[Violation] = []
        for column in sorted(columns):
            spec = contract.columns[column]
            if not spec.unique:
                continue
            stats = ctx.profile.column_stats.get(column)
            if stats is None:
                continue
            if not stats.unique:
                duplicates = (
                    _duplicate_values(ctx.engine, contract.table, ctx.batch_id, column)
                    if ctx.engine is not None and ctx.batch_id is not None
                    else []
                )
                violations.append(
                    self._violation(
                        ctx,
                        column,
                        kind="within the batch",
                        duplicates=duplicates,
                        distinct=stats.distinct,
                        non_null=stats.non_null,
                    )
                )
                continue
            if contract.layer is Layer.RAW and ctx.batch_id is not None and ctx.engine is not None:
                collisions = _cross_batch_collisions(
                    ctx.engine, contract.table, ctx.batch_id, column
                )
                if collisions:
                    violations.append(
                        self._violation(
                            ctx,
                            column,
                            kind="against already-loaded rows",
                            duplicates=collisions,
                            distinct=stats.distinct,
                            non_null=stats.non_null,
                        )
                    )
        return violations

    def _violation(
        self,
        ctx: CheckContext,
        column: str,
        *,
        kind: str,
        duplicates: list[str],
        distinct: int,
        non_null: int,
    ) -> Violation:
        assert ctx.contract is not None  # guarded by the caller
        return Violation(
            check=self.name,
            severity=self.severity,
            table=ctx.contract.table,
            column=column,
            batch_id=ctx.batch_id,
            message=f"duplicate values in unique column {column!r} ({kind})",
            observed={"distinct": distinct, "non_null": non_null},
            expected={"unique": True},
            sample=duplicates,
        )
