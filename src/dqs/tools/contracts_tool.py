"""`get_contract` — the approved contract as structured data (§10.3)."""

from __future__ import annotations

from typing import Any

from dqs.contracts.store import load_approved, load_contracts
from dqs.errors import ToolError
from dqs.tools.base import ToolContext, data_tool, validate_qualified_table


@data_tool
def get_contract(ctx: ToolContext, table: str) -> dict[str, Any]:
    """Return the approved contract for `schema.table` (drafts are rejected)."""
    qualified = validate_qualified_table(table)
    approved = load_approved(ctx.contracts_dir)
    contract = approved.get(qualified)
    if contract is None:
        available = load_contracts(ctx.contracts_dir)
        if qualified in available:
            raise ToolError(
                f"contract for {qualified} is {available[qualified].status.value}, "
                "not approved; the investigator may only rely on approved rules"
            )
        raise ToolError(f"no contract file for {qualified} in {ctx.contracts_dir}")
    return contract.model_dump(mode="json", exclude_none=True)
