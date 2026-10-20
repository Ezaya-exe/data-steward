"""Real-LLM acceptance for the Phase-5b fixer on F1/F3/F6 (marker `llm`).

This is the one place the fixer talks to a real provider. It runs the real
investigator first, stores its `RootCause`, builds the deterministic impact
report, then lets the fixer plan and the sandbox verifier judge the patch. If
verification passes, the test-only clean-data oracle also compares the patched
staging batch with the generator's clean frame; a verifier pass with an oracle
failure is recorded and fails the test, never hidden.

Skipped unless `RUN_LLM_TESTS=1` and a real provider is configured. The
deterministic fake provider is refused so no number can be mistaken for model
evidence (AGENTS.md §7.9). Each scenario uses a throwaway clone; the dev
warehouse is never touched.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, text
from tests.helpers import oracle

from dqs import db as dbmod
from dqs import dbt_run
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.agents import fixer as fixer_mod
from dqs.agents.investigator import NODE_NAME as INVESTIGATOR_NODE
from dqs.agents.investigator import investigate
from dqs.agents.llm import BudgetTracker, LLMClient
from dqs.agents.steps import DbStepSink
from dqs.config import Settings
from dqs.contracts import store
from dqs.lineage.graph import LineageGraph
from dqs.lineage.impact import build_impact_report
from dqs.models.incident import Incident
from dqs.resolve import plan_resolution
from dqs.sim import pipeline
from dqs.sim.faults import parse_fault_specs
from dqs.tools.base import ToolContext

pytestmark = [pytest.mark.integration, pytest.mark.llm]

TEMPLATE_DB = "dqs_llm_fixer_template"
SEED = 42
BASELINE_DAYS = 30
FAULT_DATE = dt.date(2026, 1, 31)
BATCH_ID = f"batch_{FAULT_DATE:%Y%m%d}"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
OUT_DIR = Path("out")

SCENARIOS = [
    ("F1_column_rename", ["column_rename:orders.customer_id:new_name=cust_id"]),
    ("F3_type_change", ["type_change:orders.total_amount"]),
    ("F6_duplicate_rows", ["duplicate_rows:orders:fraction=0.05"]),
]


def _require_real_llm(settings: Settings) -> None:
    if os.environ.get("RUN_LLM_TESTS") != "1":
        pytest.skip("set RUN_LLM_TESTS=1 to run real-LLM tests")
    provider = settings.llm_provider.strip().lower()
    if provider == "fake":
        pytest.skip("refusing to record fixer numbers from the deterministic fake provider")
    keys = {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "google_genai": settings.google_api_key,
        "openai_compatible": settings.llm_api_key,
    }
    if provider in keys and not keys[provider]:
        pytest.skip(f"LLM_PROVIDER={provider} has no API key configured")


@pytest.fixture(scope="module")
def template(settings: Settings) -> Iterator[Settings]:
    """Seed the 30-day raw baseline once; no dbt build is needed for this test."""
    _require_real_llm(settings)
    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEMPLATE_DB}"')
        template_settings = settings.model_copy(update={"pg_database": TEMPLATE_DB})
        dbmod.init_database(template_settings)
        pipeline.seed_baseline(template_settings, BASELINE_DAYS, SEED, run_dbt=False)
        engine = dbmod.make_rw_engine(template_settings)
        try:
            result = monitor_mod.run_monitor(
                engine,
                store.load_approved(CONTRACTS_DIR),
                monitor_mod.parse_as_of("2026-01-31T00:00Z"),
            )
            assert result.created == [] and result.updated == []
            assert incidents_mod.list_incidents(engine) == []
        finally:
            engine.dispose()
        yield template_settings
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def clone(settings: Settings, template: Settings) -> Iterator[tuple[Settings, Engine]]:
    name = f"dqs_llm_fix_{uuid.uuid4().hex[:8]}"
    admin = sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"')
        clone_settings = settings.model_copy(update={"pg_database": name})
        engine = dbmod.make_rw_engine(clone_settings)
        try:
            yield clone_settings, engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


@pytest.mark.parametrize(("scenario", "specs"), SCENARIOS, ids=[s for s, _ in SCENARIOS])
def test_real_llm_fixer_plan_verifies_and_matches_clean_data(
    settings: Settings,
    clone: tuple[Settings, Engine],
    scenario: str,
    specs: list[str],
) -> None:
    clone_settings, engine = clone
    pipeline.load_day(
        clone_settings,
        FAULT_DATE,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(specs),
    )
    monitor_mod.run_monitor(
        engine,
        store.load_approved(CONTRACTS_DIR),
        monitor_mod.parse_as_of("2026-02-01T00:00Z"),
    )
    incidents = [
        incident
        for incident in incidents_mod.list_incidents(engine)
        if incident.table == "raw.orders"
    ]
    assert len(incidents) == 1, f"{scenario}: expected one raw.orders incident"
    incident: Incident = incidents[0]

    budget = BudgetTracker.from_settings(settings)
    client = LLMClient.from_provider(
        settings.llm_provider,
        settings.llm_model,
        settings=settings,
        budget=budget,
    )
    context = ToolContext.from_settings(clone_settings)
    investigate_sink = DbStepSink(
        engine,
        node=INVESTIGATOR_NODE,
        incident_id=incident.incident_id,
        settings=clone_settings,
    )
    try:
        investigated = investigate(
            incident,
            client=client,
            context=context,
            sink=investigate_sink,
            budget=budget,
        )
    finally:
        context.close()
    incidents_mod.save_root_cause(
        engine, incident.incident_id, investigated.root_cause.model_dump(mode="json")
    )

    impact = build_impact_report(
        LineageGraph.from_manifest(),
        incident,
        dbt_run.latest_run_for_batch(engine, incident.batch_id),
    )
    incidents_mod.save_impact(engine, incident.incident_id, impact.model_dump(mode="json"))

    candidates = fixer_mod.select_candidates(incident, investigated.root_cause)
    resolution = plan_resolution(
        investigated.root_cause.category,
        investigated.root_cause.confidence,
        bool(candidates),
    )
    assert resolution.value == "PR", (
        f"{scenario}: investigator routed {investigated.root_cause.category.value} "
        f"({investigated.root_cause.confidence:.2f}) to {resolution.value}; "
        "a fixer acceptance run needs a PR-eligible diagnosis"
    )

    fixer_sink = DbStepSink(
        engine,
        node=fixer_mod.NODE_NAME,
        incident_id=incident.incident_id,
        settings=clone_settings,
    )
    result = fixer_mod.plan_fix(
        incident,
        investigated.root_cause,
        impact,
        client=client,
        sink=fixer_sink,
        settings=clone_settings,
        engine=engine,
        candidates=candidates,
    )

    oracle_record: dict[str, Any] | None = None
    oracle_ok: bool | None = None
    if result.passed:
        passed_plan = result.attempts[-1].plan
        assert passed_plan is not None
        comparison = oracle.compare_patched_staging(
            clone_settings,
            passed_plan,
            engine=engine,
            fault_date=FAULT_DATE,
            seed=SEED,
        )
        oracle_ok = comparison.passed
        oracle_record = {
            "build_passed": comparison.build_passed,
            "passed": comparison.passed,
            "rows_expected": comparison.rows_expected,
            "rows_actual": comparison.rows_actual,
            "missing_keys": comparison.missing_keys,
            "unexpected_keys": comparison.unexpected_keys,
            "duplicate_keys": comparison.duplicate_keys,
            "mismatched_cells": comparison.mismatched_cells,
            "detail": comparison.detail,
        }
        assert comparison.build_passed, comparison.detail

    attempts = [
        {
            "attempt_no": attempt.attempt_no,
            "channel": attempt.final_channel,
            "passed": attempt.verification.passed,
            "failed_criteria": attempt.verification.failed_criteria,
            "criteria": [
                criterion.model_dump(mode="json") for criterion in attempt.verification.criteria
            ],
            "dbt_exit_code": attempt.verification.dbt_exit_code,
            "plan": attempt.plan.model_dump(mode="json") if attempt.plan is not None else None,
        }
        for attempt in result.attempts
    ]
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT attempt_no, passed FROM dq.fix_attempts "
                "WHERE incident_id = :i ORDER BY attempt_no"
            ),
            {"i": incident.incident_id},
        ).fetchall()
        step_count = int(
            conn.execute(
                text("SELECT count(*) FROM dq.agent_steps WHERE incident_id = :i"),
                {"i": incident.incident_id},
            ).scalar_one()
        )
    persisted_attempts = [(int(row[0]), bool(row[1])) for row in rows]

    record: dict[str, Any] = {
        "scenario": scenario,
        "fault_specs": specs,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "run_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "incident_id": str(incident.incident_id),
        "root_cause": investigated.root_cause.model_dump(mode="json"),
        "investigate_tokens": investigated.tokens,
        "investigate_tool_calls": investigated.tool_calls,
        "candidates": [candidate.name for candidate in candidates],
        "resolution": resolution.value,
        "attempts_used": result.attempts_used,
        "verdict": "PASS" if result.passed else "FAIL",
        "budget_exceeded": result.budget_exceeded,
        "fixer_tokens": result.tokens,
        "fixer_retries": result.retries,
        "budget_tokens_total": budget.tokens,
        "oracle_ok": oracle_ok,
        "oracle": oracle_record,
        "attempts": attempts,
        "persisted_attempts": persisted_attempts,
        "agent_steps": step_count,
    }
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"phase-5b-llm-{scenario}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
    print(
        f"[fixer-llm] {scenario}: category={investigated.root_cause.category.value} "
        f"confidence={investigated.root_cause.confidence:.2f} "
        f"attempts={result.attempts_used} verdict={record['verdict']} "
        f"oracle={oracle_ok} tokens={result.tokens} retries={result.retries} "
        f"steps={step_count}"
    )

    assert persisted_attempts == [
        (attempt.attempt_no, attempt.verification.passed) for attempt in result.attempts
    ], "every fixer attempt must be persisted once"
    final_failed = result.final_verification.failed_criteria if result.final_verification else ["-"]
    assert result.passed, (
        f"{scenario}: fixer exhausted {result.attempts_used} attempt(s) "
        f"with failed criteria {final_failed}"
    )
    assert oracle_ok is True, (
        f"{scenario}: verification passed but the clean-data oracle failed: {oracle_record}"
    )
