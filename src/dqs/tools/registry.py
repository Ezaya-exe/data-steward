"""Registry: expose the read-only tools to a chat model with typed arguments.

Why a registry: the investigator must never receive a raw callable with
untyped `**kwargs`. Every tool here has a Pydantic argument schema and a
description written for the model; the registry is also the single list used by
tests to assert that every tool wraps its result as data.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from dqs.tools.base import ToolContext
from dqs.tools.contracts_tool import get_contract
from dqs.tools.dbt_tools import get_model_sql, get_pipeline_run
from dqs.tools.lineage_tool import get_lineage
from dqs.tools.profiling import compare_profiles, get_batch_profile, sample_rows
from dqs.tools.readonly_sql import run_readonly_sql
from dqs.tools.renames import find_rename_candidates


class GetContractArgs(BaseModel):
    table: str = Field(description="Qualified table, e.g. 'raw.orders'.")


class GetBatchProfileArgs(BaseModel):
    table: str = Field(description="Qualified raw table, e.g. 'raw.orders'.")
    batch_id: str = Field(description="Batch id, e.g. 'batch_20260204'.")


class CompareProfilesArgs(BaseModel):
    table: str = Field(description="Qualified raw table, e.g. 'raw.orders'.")
    batch_a: str = Field(description="First batch id (usually a clean baseline).")
    batch_b: str = Field(description="Second batch id (usually the suspect batch).")


class FindRenameCandidatesArgs(BaseModel):
    table: str = Field(description="Qualified raw table, e.g. 'raw.orders'.")
    batch_id: str = Field(description="Batch where contract columns are missing.")


class SampleRowsArgs(BaseModel):
    table: str = Field(description="Qualified raw table, e.g. 'raw.orders'.")
    batch_id: str = Field(description="Batch id to sample.")
    column: str | None = Field(default=None, description="Optional single column.")
    only_violating: bool = Field(
        default=False,
        description="Keep rows that breach the approved contract (best effort).",
    )
    limit: int = Field(default=10, ge=1, le=10, description="Maximum rows (≤10).")


class RunReadonlySqlArgs(BaseModel):
    sql: str = Field(
        description=(
            "Exactly one SELECT or WITH ... SELECT against raw/staging/marts/dq. "
            "A LIMIT is added/capped at 200; DDL/DML and admin functions are rejected."
        )
    )


class GetPipelineRunArgs(BaseModel):
    batch_id: str = Field(description="Batch id whose latest dbt run to read.")


class GetModelSqlArgs(BaseModel):
    model_name: str = Field(description="Bare dbt model name, e.g. 'stg_orders'.")


class GetLineageArgs(BaseModel):
    node: str = Field(description="Unique id, raw.table, or bare model name.")
    direction: Literal["upstream", "downstream"] = Field(
        default="downstream", description="Traversal direction."
    )


@dataclass(frozen=True)
class ToolSpec:
    """One tool's stable name, description, argument schema, and implementation."""

    name: str
    description: str
    args_schema: type[BaseModel]
    function: Callable[..., Any]


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "get_contract",
        "Return the approved data contract for a table (columns, rules, volume, freshness).",
        GetContractArgs,
        get_contract,
    ),
    ToolSpec(
        "get_batch_profile",
        "Per-column null rate, parse rates, distinct count and min/max/mean for one batch.",
        GetBatchProfileArgs,
        get_batch_profile,
    ),
    ToolSpec(
        "compare_profiles",
        "Diff two batches of the same table: null/mean/distinct deltas and missing columns.",
        CompareProfilesArgs,
        compare_profiles,
    ),
    ToolSpec(
        "find_rename_candidates",
        "For each missing contract column, rank new columns by name, type and value overlap.",
        FindRenameCandidatesArgs,
        find_rename_candidates,
    ),
    ToolSpec(
        "sample_rows",
        "Return up to 10 evidence rows for a batch; optionally only contract-violating rows.",
        SampleRowsArgs,
        sample_rows,
    ),
    ToolSpec(
        "run_readonly_sql",
        "Run one guarded SELECT/WITH query on the read-only role (LIMIT ≤ 200, ≤5s).",
        RunReadonlySqlArgs,
        run_readonly_sql,
    ),
    ToolSpec(
        "get_pipeline_run",
        "Newest stored dbt run for a batch: status, parsed failed nodes and log excerpt.",
        GetPipelineRunArgs,
        get_pipeline_run,
    ),
    ToolSpec(
        "get_model_sql",
        "Return the source SQL of a staging/mart dbt model by bare model name.",
        GetModelSqlArgs,
        get_model_sql,
    ),
    ToolSpec(
        "get_lineage",
        "List upstream/downstream dbt nodes (sources, models, tests, exposures) for a node.",
        GetLineageArgs,
        get_lineage,
    ),
)

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {spec.name: spec.function for spec in TOOL_SPECS}
TOOL_NAMES: tuple[str, ...] = tuple(TOOL_FUNCTIONS)


def build_tools(ctx: ToolContext) -> list[BaseTool]:
    """Instantiate the registry for one investigation context."""
    tools: list[BaseTool] = []
    for spec in TOOL_SPECS:
        bound = functools.partial(spec.function, ctx)
        tools.append(
            StructuredTool.from_function(
                func=bound,
                name=spec.name,
                description=spec.description,
                args_schema=spec.args_schema,
            )
        )
    return tools


def registry_names() -> Sequence[str]:
    """Stable tool names (used by prompts and tests)."""
    return TOOL_NAMES
