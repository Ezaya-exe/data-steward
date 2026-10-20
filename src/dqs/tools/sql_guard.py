"""Deny-by-default SQL guard and read-only execution (AGENTS.md §7.2).

Why: `run_readonly_sql` is the only tool that accepts free-form SQL from a
model. The model is untrusted output, so the guard is a positive allowlist, not
a blocklist: exactly one statement, parsed by sqlglot (postgres dialect), a
top-level SELECT/WITH-SELECT, no DML/DDL anywhere (including inside a CTE), no
`SELECT ... INTO`, no locking clauses, only the four steward schemas, no
table-format abuse, and no dangerous admin/file/dblink functions. The LIMIT is
capped at 200 regardless of what the model asked for.

Execution is defence in depth: the same statement runs on the `dqs_ro` engine
inside `SET TRANSACTION READ ONLY` with `statement_timeout` ≤ 5 s. Even if the
parser were bypassed, the role and transaction cannot write.

The parser is deliberately strict; false rejections are acceptable, silent
acceptance is not.
"""

from __future__ import annotations

import re
from typing import Any, Final

import sqlglot
import sqlglot.expressions as exp
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs.errors import SqlExecutionError, SqlGuardError
from dqs.tools.base import ALLOWED_SCHEMAS, to_jsonable

DIALECT: Final = "postgres"
MAX_ROWS: Final = 200
MAX_TIMEOUT_MS: Final = 5000
_DOLLAR_RE: Final = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_FORBIDDEN_NODES: Final = tuple(
    node
    for node in (
        getattr(exp, name, None)
        for name in (
            "Insert",
            "Update",
            "Delete",
            "Create",
            "Drop",
            "Alter",
            "TruncateTable",
            "Command",
            "Copy",
            "Merge",
            "Grant",
            "Revoke",
            "Into",
            "Lock",
            "Set",
            "Use",
            "Transaction",
            "Commit",
            "Rollback",
        )
    )
    if node is not None
)
_DANGEROUS_EXACT: Final = frozenset(
    {
        "set_config",
        "current_setting",
        "query_to_xml",
        "database_to_xml",
        "has_database_privilege",
        "has_schema_privilege",
        "has_table_privilege",
        "loread",
        "lowrite",
    }
)
_DANGEROUS_PREFIXES: Final = ("pg_", "lo_", "dblink")


class QueryResult(BaseModel):
    """A bounded read-only result set (already JSON-compatible)."""

    model_config = ConfigDict(extra="forbid")

    sql: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


def _has_embedded_semicolon(sql: str) -> bool:
    """True when a real (non-quoted, non-comment) `;` sits before the end.

    One trailing semicolon is allowed. This is what catches
    `SELECT 1; -- ; DROP TABLE ...`: sqlglot tokenizes away the comment, but the
    raw text still contains an executable statement separator. Semicolons
    inside strings, quoted identifiers, dollar-quoted bodies and comments are
    ignored.
    """
    body = sql.rstrip()
    if body.endswith(";"):
        body = body[:-1]
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char in ("'", '"'):
            quote = char
            index += 1
            while index < length:
                if body[index] == quote:
                    if index + 1 < length and body[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == "-" and index + 1 < length and body[index + 1] == "-":
            index += 2
            while index < length and body[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and body[index + 1] == "*":
            index += 2
            while index + 1 < length and not (body[index] == "*" and body[index + 1] == "/"):
                index += 1
            index += 2
            continue
        if char == "$":
            match = _DOLLAR_RE.match(body, index)
            if match is not None:
                tag = match.group(0)
                close = body.find(tag, match.end())
                index = length if close == -1 else close + len(tag)
                continue
        if char == ";":
            return True
        index += 1
    return False


def _limit_value(statement: exp.Expression) -> int | None:
    """The numeric LIMIT value, or None when absent/non-literal (LIMIT ALL)."""
    limit = statement.args.get("limit")
    if limit is None:
        return None
    expression = getattr(limit, "expression", None)
    if isinstance(expression, exp.Literal) and not expression.is_string:
        try:
            return int(str(expression.this))
        except (TypeError, ValueError):
            return None
    return None


def _cte_names(statement: exp.Expression) -> set[str]:
    names: set[str] = set()
    for cte in statement.find_all(exp.CTE):
        alias = cte.alias_or_name
        if alias:
            names.add(alias.lower())
    return names


def _check_relations(statement: exp.Expression) -> None:
    cte_names = _cte_names(statement)
    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        db = str(table.db or "").lower()
        catalog = str(table.catalog or "").lower()
        if not isinstance(table.this, exp.Identifier):
            raise SqlGuardError(
                f"table functions are not allowed in FROM (found {table.sql(dialect=DIALECT)})"
            )
        if catalog:
            raise SqlGuardError(
                f"catalog-qualified relation {table.sql(dialect=DIALECT)!r} is not allowed"
            )
        if not db:
            if name in cte_names:
                continue
            raise SqlGuardError(
                f"relation {name!r} must be schema-qualified (allowed: "
                f"{', '.join(sorted(ALLOWED_SCHEMAS))})"
            )
        if db not in ALLOWED_SCHEMAS:
            raise SqlGuardError(
                f"schema {db!r} is not readable; allowed: {', '.join(sorted(ALLOWED_SCHEMAS))}"
            )


def _check_functions(statement: exp.Expression) -> None:
    for function in (*statement.find_all(exp.Anonymous), *statement.find_all(exp.Func)):
        name = str(getattr(function, "name", "") or "").lower()
        if not name:
            continue
        if name in _DANGEROUS_EXACT or name.startswith(_DANGEROUS_PREFIXES):
            raise SqlGuardError(
                f"function {name!r} is not allowed in read-only agent SQL "
                "(file, admin, session and dblink functions are denied)"
            )


def validate_sql(sql: str, *, max_rows: int = MAX_ROWS) -> str:
    """Validate and return the capped SQL, or raise `SqlGuardError`.

    The returned text is what may be executed: one statement, safe relations,
    and a LIMIT no greater than `max_rows` (≤ 200).
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlGuardError("SQL is empty")
    row_cap = max(1, min(int(max_rows), MAX_ROWS))
    if _has_embedded_semicolon(sql):
        raise SqlGuardError(
            "multiple statements are not allowed (a semicolon was found before the end)"
        )
    try:
        parsed = sqlglot.parse(sql, read=DIALECT)
    except (sqlglot.ParseError, ValueError) as exc:
        raise SqlGuardError(f"SQL does not parse as postgres: {exc}") from exc
    statements = [statement for statement in parsed if statement is not None]
    if len(statements) != 1:
        raise SqlGuardError(f"exactly one statement is required; parsed {len(statements)}")
    statement = statements[0]

    for node in statement.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlGuardError(
                f"{type(node).__name__.upper()} is not allowed; only SELECT / WITH ... SELECT"
            )
    if not isinstance(statement, (exp.Select, exp.Union)):
        raise SqlGuardError(
            f"only SELECT / WITH ... SELECT is allowed, got {type(statement).__name__}"
        )
    if statement.args.get("into") is not None:
        raise SqlGuardError("SELECT ... INTO is not allowed")
    if statement.args.get("locks"):
        raise SqlGuardError("locking clauses (FOR UPDATE/SHARE) are not allowed")
    _check_relations(statement)
    _check_functions(statement)

    limit = _limit_value(statement)
    if limit is None or limit > row_cap:
        try:
            statement = statement.limit(row_cap)
        except (AttributeError, ValueError) as exc:  # defensive: unusual statement shape
            raise SqlGuardError(f"could not apply the mandatory LIMIT: {exc}") from exc
    return statement.sql(dialect=DIALECT)


def execute_readonly(
    engine: Engine,
    sql: str,
    *,
    max_rows: int = MAX_ROWS,
    timeout_ms: int = MAX_TIMEOUT_MS,
) -> QueryResult:
    """Validate, then run on the supplied read-only engine with hard limits."""
    capped = validate_sql(sql, max_rows=max_rows)
    row_cap = max(1, min(int(max_rows), MAX_ROWS))
    timeout = max(1, min(int(timeout_ms), MAX_TIMEOUT_MS))
    try:
        with engine.connect() as conn:
            # SET TRANSACTION must be the first statement of the transaction;
            # SQLAlchemy starts it on this execute. SET LOCAL scopes the timeout
            # to this transaction only.
            conn.execute(text("SET TRANSACTION READ ONLY"))
            conn.execute(text(f"SET LOCAL statement_timeout = {timeout}"))
            result = conn.execute(text(capped))
            columns = [str(name) for name in result.keys()]
            fetched = result.fetchmany(row_cap)
            rows = [
                {column: to_jsonable(value) for column, value in zip(columns, row, strict=False)}
                for row in fetched
            ]
            conn.rollback()
    except SQLAlchemyError as exc:
        raise SqlExecutionError(f"read-only query failed: {exc}") from exc
    return QueryResult(
        sql=capped,
        columns=columns,
        rows=rows,
        row_count=len(rows),
        truncated=len(rows) >= row_cap,
    )
