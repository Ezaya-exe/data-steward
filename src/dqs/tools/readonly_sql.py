"""`run_readonly_sql` — the guarded, read-only SQL tool (AGENTS.md §7.2)."""

from __future__ import annotations

from typing import Any

from dqs.tools.base import ToolContext, data_tool
from dqs.tools.sql_guard import execute_readonly


@data_tool
def run_readonly_sql(ctx: ToolContext, sql: str) -> dict[str, Any]:
    """Run one guarded SELECT/WITH query on the read-only role (LIMIT ≤ 200)."""
    result = execute_readonly(ctx.engine, sql)
    return result.model_dump(mode="json")
