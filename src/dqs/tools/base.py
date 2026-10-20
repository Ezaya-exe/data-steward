"""Shared tool plumbing: typed context, safe identifiers, and data envelopes.

Why: every tool is exposed to a model, so its output crosses a trust boundary
in both directions. The model supplies arguments (which must be validated
before SQL interpolation) and receives rows/log text that may contain hostile
instructions (which must be *data*, never instructions — AGENTS.md §7.6).

This module centralises the two guarantees:

* `data_tool` wraps every tool return value as `<data>{compact JSON}</data>`
  truncated to at most 4 KB; tool errors also become data so the investigator
  can reason about them instead of crashing.
* `ToolContext` carries the read-only engine and lazily builds the lineage
  graph once per investigation (Phase-3 review risk: repeated manifest loads).
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import logging
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from dqs.contracts.profiler import IDENTIFIER_RE
from dqs.dbt_run import PROJECT_DIR
from dqs.errors import DqsError, ToolError
from dqs.lineage.graph import LineageGraph

logger = logging.getLogger(__name__)

MAX_OUTPUT_BYTES = 4096
DATA_OPEN = "<data>"
DATA_CLOSE = "</data>"
ALLOWED_SCHEMAS = frozenset({"raw", "staging", "marts", "dq"})
RAW_SCHEMA = "raw"
SYSTEM_COLUMNS = frozenset({"_batch_id", "_loaded_at"})


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    """Allow only `^[a-z_][a-z0-9_]*$` before any SQL interpolation (AGENTS §6)."""
    if not isinstance(value, str) or IDENTIFIER_RE.match(value) is None:
        raise ToolError(f"{kind} {value!r} is not a safe lowercase identifier")
    return value


def validate_qualified_table(table: str, *, schemas: frozenset[str] = ALLOWED_SCHEMAS) -> str:
    """Validate `schema.table` and its schema against an allowlist."""
    parts = table.split(".")
    if len(parts) != 2:
        raise ToolError(f"table must be qualified as schema.table, got {table!r}")
    schema, name = parts
    validate_identifier(schema, kind="schema")
    validate_identifier(name, kind="table")
    if schema not in schemas:
        allowed = ", ".join(sorted(schemas))
        raise ToolError(f"schema {schema!r} is not readable by tools; allowed: {allowed}")
    return table


def validate_raw_table(table: str) -> tuple[str, str]:
    """Only `raw.<table>` is batch-addressable; return its two parts."""
    qualified = validate_qualified_table(table, schemas=frozenset({RAW_SCHEMA}))
    schema, name = qualified.split(".")
    return schema, name


def to_jsonable(value: Any) -> Any:
    """Convert DB/profiler values into deterministic JSON-compatible data."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return to_jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max(0, max_bytes)].decode("utf-8", errors="ignore")


def compact_json(payload: Any) -> str:
    """Deterministic compact JSON (sorted keys, no ASCII escaping)."""
    return json.dumps(
        to_jsonable(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def wrap_data(payload: Any) -> str:
    """Wrap a payload as `<data>…</data>` and keep the whole string ≤ 4 KB.

    When truncation is needed the result stays *valid JSON*: the body becomes
    `{"truncated":true,"preview":"…"}`, so a model always receives parseable
    data rather than a cut string.
    """
    body = compact_json(payload)
    budget = MAX_OUTPUT_BYTES - len(DATA_OPEN) - len(DATA_CLOSE)
    if len(body.encode("utf-8")) <= budget:
        return f"{DATA_OPEN}{body}{DATA_CLOSE}"

    preview = _truncate_utf8(body, max(0, budget - 64))
    while True:
        truncated = compact_json({"truncated": True, "preview": preview})
        if len(truncated.encode("utf-8")) <= budget:
            return f"{DATA_OPEN}{truncated}{DATA_CLOSE}"
        if not preview:
            return f'{DATA_OPEN}{{"truncated":true,"preview":""}}{DATA_CLOSE}'
        preview = _truncate_utf8(preview, max(0, len(preview.encode("utf-8")) - 128))


def data_tool[**P](func: Callable[P, Any]) -> Callable[P, str]:
    """Decorator: every tool result is wrapped as data, errors included.

    `ValueError` is included because identifier/profiler validation raises it;
    the model should see a precise correction, not a traceback. Programming
    errors are not caught (they must surface in tests), but DB failures are
    reported as data because they are part of an investigation's evidence.
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return wrap_data(func(*args, **kwargs))
        except (DqsError, ValueError) as exc:
            return wrap_data({"error": str(exc)})
        except SQLAlchemyError as exc:
            logger.warning("tool %s database error: %s", func.__name__, exc)
            return wrap_data({"error": f"database error: {exc}"})

    setattr(wrapper, "_wraps_data", True)  # noqa: B010 - marker used by the registry test
    return wrapper


@dataclass
class ToolContext:
    """Dependencies shared by every tool in one investigation."""

    engine: Engine
    contracts_dir: Path = Path("contracts")
    project_dir: Path = PROJECT_DIR
    _graph: LineageGraph | None = field(default=None, init=False, repr=False)

    def graph(self) -> LineageGraph:
        """Build the manifest DAG once per context (not once per lineage call)."""
        if self._graph is None:
            self._graph = LineageGraph.from_manifest(project_dir=self.project_dir)
        return self._graph
