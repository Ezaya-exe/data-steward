"""Compact, persisted summary of one scenario's agent timeline (Phase 8c).

Why: a results file that only stores the final score cannot explain *how* the
run got there. The step log in `dq.agent_steps` is detailed but deleted with the
scenario database; the LangGraph checkpoints keep workflow state but not the
tool sequence. This module turns the persisted step rows into a small, typed
record that travels with the result file, so a later read-only analysis does not
need the dropped database or the checkpoint SQLite file.

This is reporting only. It reads already-written rows and never influences an
agent decision, a budget or a score.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text

from dqs.models.fix import VerificationResult
from dqs.models.step import StepRecord


class NodeTokens(BaseModel):
    """Input/output tokens attributed to one graph node."""

    model_config = ConfigDict(extra="forbid")

    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)


class StepSummary(BaseModel):
    """The compact timeline stored next to a scenario score (A3)."""

    model_config = ConfigDict(extra="forbid")

    tool_sequence: list[str] = Field(default_factory=list)
    tool_calls: int = Field(default=0, ge=0)
    llm_calls: int = Field(default=0, ge=0)
    tokens_by_node: dict[str, NodeTokens] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    fix_attempts: int = Field(default=0, ge=0)
    failed_criteria: list[str] = Field(default_factory=list)


def _event(record: StepRecord) -> str | None:
    """One replayable event label for the handful of steps that explain a stop."""
    output = record.output
    if record.name == "final_channel":
        return f"final_channel={output.get('channel', 'unknown')}"
    if record.name == "provider_tool_use_failed":
        return f"failed_generation={output.get('path', 'unknown')}"
    if record.name == "final_channel_fallback":
        return "final_channel_fallback"
    if record.name == "budget_exhausted":
        return "budget_exhausted"
    if record.name == "budget_synthesis":
        return f"budget_synthesis={output.get('channel', 'unknown')}"
    if record.name == "evidence_dropped":
        return f"evidence_dropped={output.get('kept', 'unknown')}"
    if record.name in {
        "llm_error",
        "model_error_recovered",
        "root_cause_tool_invalid",
        "root_cause_tool",
    }:
        return record.name
    return None


def summarize_steps(
    records: Sequence[StepRecord],
    *,
    fix_attempts: int = 0,
    failed_criteria: Sequence[str] = (),
) -> StepSummary:
    """Build the deterministic step summary from ordered `StepRecord`s."""
    tokens: dict[str, NodeTokens] = {}
    events: list[str] = []
    for record in records:
        node = tokens.setdefault(record.node, NodeTokens())
        node.tokens_in += record.tokens_in
        node.tokens_out += record.tokens_out
        event = _event(record)
        if event is not None:
            events.append(event)
    return StepSummary(
        tool_sequence=[record.name for record in records if record.kind == "tool"],
        tool_calls=sum(1 for record in records if record.kind == "tool"),
        llm_calls=sum(1 for record in records if record.kind == "llm"),
        tokens_by_node=tokens,
        events=events,
        fix_attempts=max(0, fix_attempts),
        failed_criteria=list(dict.fromkeys(failed_criteria)),
    )


def load_step_records(engine: Engine, incident_id: uuid.UUID) -> list[StepRecord]:
    """Read one incident's step rows in timeline order (read-only)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT incident_id, seq, node, kind, name, input, output, "
                "tokens_in, tokens_out, latency_ms FROM dq.agent_steps "
                "WHERE incident_id = :i ORDER BY seq"
            ),
            {"i": incident_id},
        ).mappings()
        return [
            StepRecord(
                incident_id=row["incident_id"],
                seq=row["seq"],
                node=row["node"],
                kind=row["kind"],
                name=row["name"],
                input=row["input"] or {},
                output=row["output"] or {},
                tokens_in=row["tokens_in"] or 0,
                tokens_out=row["tokens_out"] or 0,
                latency_ms=row["latency_ms"] or 0,
            )
            for row in rows
        ]


def load_fix_history(engine: Engine, incident_id: uuid.UUID) -> tuple[int, list[str]]:
    """Return `(attempt_count, failed_criteria)` from every stored fix attempt."""
    with engine.connect() as conn:
        rows: list[object] = list(
            conn.execute(
                text(
                    "SELECT verification FROM dq.fix_attempts "
                    "WHERE incident_id = :i ORDER BY attempt_no, attempt_id"
                ),
                {"i": incident_id},
            ).scalars()
        )
        criteria: list[str] = []
        count = 0
        for payload in rows:
            count += 1
            if payload is None:
                continue
            try:
                verification = VerificationResult.model_validate(payload)
            except Exception:
                # A corrupt row is still an attempt; never hide the count, and
                # do not guess which criteria failed.
                continue
            criteria.extend(verification.failed_criteria)
        return count, list(dict.fromkeys(criteria))


__all__ = [
    "NodeTokens",
    "StepSummary",
    "load_fix_history",
    "load_step_records",
    "summarize_steps",
]
