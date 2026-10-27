"""Compact step-summary tests (Phase 8c A3). Reporting only, so no DB needed."""

from __future__ import annotations

import uuid

from dqs.bench.summary import summarize_steps
from dqs.models.step import StepRecord


def _step(
    seq: int,
    *,
    node: str = "investigate",
    kind: str = "system",
    name: str = "event",
    output: dict[str, object] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> StepRecord:
    return StepRecord(
        incident_id=uuid.uuid4(),
        seq=seq,
        node=node,
        kind=kind,  # type: ignore[arg-type]
        name=name,
        output=output or {},
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )


def test_summary_keeps_tool_order_counts_and_tokens_per_node() -> None:
    records = [
        _step(1, kind="system", name="investigation_start"),
        _step(2, kind="llm", name="m", tokens_in=10, tokens_out=5),
        _step(3, kind="tool", name="get_contract"),
        _step(4, kind="llm", name="m", tokens_in=20, tokens_out=10),
        _step(5, kind="tool", name="run_readonly_sql"),
        _step(6, node="plan_fix", kind="llm", name="m", tokens_in=7, tokens_out=3),
    ]
    summary = summarize_steps(records, fix_attempts=2, failed_criteria=["row_preservation"])

    assert summary.tool_sequence == ["get_contract", "run_readonly_sql"]
    assert summary.tool_calls == 2
    assert summary.llm_calls == 3
    assert summary.tokens_by_node["investigate"].tokens_in == 30
    assert summary.tokens_by_node["investigate"].tokens_out == 15
    assert summary.tokens_by_node["plan_fix"].tokens_in == 7
    assert summary.fix_attempts == 2
    assert summary.failed_criteria == ["row_preservation"]


def test_summary_records_the_events_that_explain_a_stop() -> None:
    records = [
        _step(1, name="final_channel", output={"channel": "root_cause_tool"}),
        _step(2, name="provider_tool_use_failed", output={"path": "salvaged"}),
        _step(3, name="final_channel_fallback"),
        _step(4, name="budget_exhausted"),
        _step(5, name="budget_synthesis", output={"channel": "budget_synthesis"}),
        _step(6, name="evidence_dropped", output={"kept": 1}),
        _step(7, name="root_cause_tool_invalid"),
        _step(8, name="unrelated_system_step"),
    ]
    summary = summarize_steps(records)
    assert summary.events == [
        "final_channel=root_cause_tool",
        "failed_generation=salvaged",
        "final_channel_fallback",
        "budget_exhausted",
        "budget_synthesis=budget_synthesis",
        "evidence_dropped=1",
        "root_cause_tool_invalid",
    ]


def test_summary_deduplicates_failed_criteria() -> None:
    summary = summarize_steps([], fix_attempts=3, failed_criteria=["a", "b", "a"])
    assert summary.failed_criteria == ["a", "b"]
