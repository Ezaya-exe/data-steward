"""Structured dbt run records (Phase 3).

Why: `dq.pipeline_runs` historically held only status + log excerpt. The impact
report (and later the investigator agent) needs the *failing node ids and
statuses* parsed from `target/run_results.json` at build time — a live file
overwritten by the next build can never be trusted for a specific incident
(DECISIONS 55). These Pydantic models are the contract between `dbt_run`
(writer), the database (`dq.pipeline_runs.failed_nodes` jsonb) and the impact
builder (reader), so the stored JSON shape is validated, not guessed.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


class NodeResult(BaseModel):
    """One failing/errored dbt node as stored in `dq.pipeline_runs.failed_nodes`."""

    model_config = ConfigDict(extra="forbid")

    unique_id: str  # e.g. "test.dq_warehouse.not_null_stg_orders_customer_id.af79d5e4b5"
    name: str
    status: str  # dbt run_results status: fail | error
    message: str = ""
    batch_id: str | None = None  # recorded only so a stored entry stays self-describing


class PipelineRunRecord(BaseModel):
    """One `dq.pipeline_runs` row, including the parsed failure payload."""

    model_config = ConfigDict(extra="forbid")

    run_id: int
    batch_id: str | None = None
    command: str | None = None
    status: str | None = None  # success | failed
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    log_excerpt: str = ""
    run_results_parsed_at: dt.datetime | None = None
    failed_nodes: list[NodeResult] = Field(default_factory=list)
