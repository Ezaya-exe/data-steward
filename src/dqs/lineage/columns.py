"""Column-level lineage via sqlglot on compiled dbt SQL — ARCHITECTURE §9.

Why here: dbt does not record column-level lineage in the manifest. For
column-scoped incidents (schema drift, F1/F2/F10) we approximate it
deterministically: parse each downstream model's **compiled** SQL with
sqlglot (postgres dialect) and check whether the model (a) selects from that
raw source and (b) references the column.

**This is a best-effort heuristic** (DECISIONS 58). Known limits, by
construction — never presented as ground truth:

- Scoping is not resolved across joins: if a query joins ``raw.orders`` and
  ``raw.customers`` and selects ``customer_id`` from ``customers``, an
  unqualified ``customer_id`` still counts as "referencing orders.customer_id".
- Aliases and CTE names are matched textually; a column named the same as a
  CTE-local column can be overcounted, a column appearing only inside a string
  literal or ``/* comment */`` is invisible (sqlglot parses both out).
- ``SELECT *`` does not dynamically expand *before* dbt compiles; compiled SQL
  still contains ``*`` for dbt-visible stars, so such columns are missed.
- The check is per *compiled* select: columns used only inside dbt-side
  constraints/macros that vanished during compilation are not counted.

Precision was the priority: we return references we can *prove* from compiled
text, and the impact report says "referencing (heuristic)".
"""

from __future__ import annotations

import logging
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

from dqs.errors import LineageError
from dqs.lineage.graph import LineageGraph

logger = logging.getLogger(__name__)

_DIALECT = "postgres"


def _compiled_sql(model_payload: dict[str, object], project_dir: Path) -> str | None:
    """Compiled SQL for a model node: manifest field first, compiled/ file second."""
    code = model_payload.get("compiled_code")
    if isinstance(code, str) and code.strip():
        return code
    # Fallback (dbt puts compiled files under target/compiled/<path>): read
    # from disk so manifests produced specifically by `dbt parse` still work.
    path = model_payload.get("original_file_path")
    if isinstance(path, str):
        candidate = project_dir / "target" / "compiled" / path
        try:
            text_on_disk = candidate.read_text(encoding="utf-8")
        except OSError:
            return None
        if text_on_disk.strip():
            return text_on_disk
    return None


def _source_name_parts(node_payload: dict[str, object], source_id: str) -> tuple[str, str]:
    """(schema, identifier) of the source the payload refers to."""
    schema = str(node_payload.get("schema", "") or "")
    identifier = str(node_payload.get("identifier", "") or "")
    if schema and identifier:
        return schema, identifier
    parts = source_id.split(".")
    return (parts[-2], parts[-1])


def _matching_table_tables(parsed: exp.Expression, source: tuple[str, str]) -> list[tuple[str, str]]:
    """Every table reference whose qualified name ends with `schema.identifier`."""
    schema, identifier = source
    found: list[tuple[str, str]] = []
    for table in parsed.find_all(exp.Table):
        qualified = (
            f"{table.catalog}.{table.db}.{table.name}"
            if table.catalog
            else f"{table.db}.{table.name}" if table.db else table.name
        ).lower()
        parts = qualified.split(".")
        # schema identifier are always the last two dotted parts; a catalog
        # (database) prefix may sit in front (e.g. warehouse.raw.orders).
        if len(parts) >= 2 and parts[-2] == schema.lower() and parts[-1] == identifier.lower():
            found.append((schema, identifier))
    return found


def _column_used(parsed: exp.Expression, column: str) -> bool:
    """True when `column` appears as a Column reference anywhere in the query."""
    return any(col.name.lower() == column for col in parsed.find_all(exp.Column))


def models_referencing_column(
    graph: LineageGraph,
    raw_table: str,
    column: str,
    project_dir: Path | None = None,
) -> list[str]:
    """Model names downstream of `raw_table` that use `column` (heuristic list).

    Deterministic: sorted by (distance from source, unique_id); models without
    compiled SQL are skipped with a warning (they cannot be checked, and that
    fact is recorded in the impact report, not invented here).
    """
    source_id = graph.nodes_for_raw_table(raw_table)
    source_payload = graph.node(source_id)
    source_parts = _source_name_parts(source_payload, source_id)
    if "." in raw_table and not column:
        raise LineageError(f"a column name is required for column-level lineage on {raw_table}")

    directory = project_dir or graph.project_dir
    matches: list[tuple[int, str]] = []
    for model_id in graph.downstream(source_id, types=("model",)):
        payload = graph.node(model_id)
        sql = _compiled_sql(payload, directory)
        if sql is None:
            logger.warning("no compiled SQL for %s; skipped in column references", model_id)
            continue
        try:
            parsed_statements = sqlglot.parse(sql, dialect=_DIALECT)
        except (sqlglot.ParseError, ValueError) as exc:
            logger.warning("unparseable SQL for %s (%s); skipped", model_id, exc)
            continue
        for statement in parsed_statements:
            if statement is None:
                continue
            tables = _matching_table_tables(statement, source_parts)
            if tables and _column_used(statement, column):
                matches.append((graph.distance(source_id, model_id), model_id))
                break
    return [model_id for _, model_id in sorted(matches)]
