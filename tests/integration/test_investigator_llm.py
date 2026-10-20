"""Real-LLM smoke on F1, F4 and F6 (Phase 4 acceptance, marker `llm`).

This is the one place where the investigator talks to a real provider. It is
skipped unless `RUN_LLM_TESTS=1` and the configured provider has a key
(AGENTS §8), and it refuses the `fake` provider outright so a smoke number can
never be fabricated (AGENTS §7.9). Each scenario gets a throwaway clone of a
seeded 30-day baseline, loads the fault on 2026-01-31, monitors as-of
2026-02-01 and investigates the resulting incident. The dev warehouse is never
touched, and clones/templates are dropped.

No assertions are made about the diagnosed category: the prompt is not tuned
here. The assertion is that the model returns a schema-valid `RootCause`, and
the measured category/confidence/tokens are written to `out/` for the phase
note.
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
from sqlalchemy import Engine

from dqs import db as dbmod
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.agents.investigator import NODE_NAME, investigate
from dqs.agents.llm import BudgetTracker, LLMClient
from dqs.agents.steps import DbStepSink
from dqs.config import Settings
from dqs.contracts import store
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause
from dqs.sim import pipeline
from dqs.sim.faults import parse_fault_specs
from dqs.tools.base import ToolContext

pytestmark = [pytest.mark.integration, pytest.mark.llm]

TEMPLATE_DB = "dqs_llm_template"
SEED = 42
BASELINE_DAYS = 30
FAULT_DATE = dt.date(2026, 1, 31)
BATCH_ID = f"batch_{FAULT_DATE:%Y%m%d}"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"
OUT_DIR = Path("out")

SCENARIOS = [
    (
        "F1_column_rename",
        ["column_rename:orders.customer_id:new_name=cust_id"],
    ),
    (
        "F4_null_spike",
        ["null_spike:orders.status:fraction=0.5"],
    ),
    (
        "F6_duplicate_rows",
        ["duplicate_rows:orders:fraction=0.05"],
    ),
]


def _require_real_llm(settings: Settings) -> None:
    if os.environ.get("RUN_LLM_TESTS") != "1":
        pytest.skip("set RUN_LLM_TESTS=1 to run real-LLM tests")
    provider = settings.llm_provider.strip().lower()
    if provider == "fake":
        pytest.skip("refusing to record smoke numbers from the deterministic fake provider")
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
    """Seed the 30-day baseline once and mark it checked; drop it at exit."""
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
            assert result.created == [] and result.updated == [], "clean baseline must stay clean"
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
    name = f"dqs_llm_{uuid.uuid4().hex[:8]}"
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
def test_real_llm_returns_a_schema_valid_root_cause(
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
    sink = DbStepSink(
        engine, node=NODE_NAME, incident_id=incident.incident_id, settings=clone_settings
    )
    try:
        result = investigate(
            incident,
            client=client,
            context=context,
            sink=sink,
            budget=budget,
        )
    finally:
        context.close()

    # The whole point of the structured-output choice: the answer re-validates.
    validated = RootCause.model_validate(result.root_cause.model_dump(mode="json"))
    assert validated.category is not None
    assert 0.0 <= validated.confidence <= 1.0

    tool_names = {step.name for step in result.steps if step.kind == "tool"}
    for item in validated.evidence:
        assert item.tool in tool_names, "evidence must cite a tool that actually ran"

    incidents_mod.save_root_cause(engine, incident.incident_id, validated.model_dump(mode="json"))
    persisted = incidents_mod.get_incident(engine, incident.incident_id)
    assert persisted is not None and persisted.root_cause is not None
    assert persisted.root_cause["category"] == validated.category.value

    record: dict[str, Any] = {
        "scenario": scenario,
        "fault_specs": specs,
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "run_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "category": validated.category.value,
        "confidence": validated.confidence,
        "suggested_resolution": validated.suggested_resolution.value,
        "tool_calls": result.tool_calls,
        "tokens": result.tokens,
        "retries": result.retries,
        "budget_exceeded": result.budget_exceeded,
        "final_channel": result.final_channel,
        "evidence": [item.model_dump(mode="json") for item in validated.evidence],
        "root_cause": validated.model_dump(mode="json"),
        "steps": len(result.steps),
        "timeline": [step.model_dump(mode="json") for step in result.steps],
    }
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"phase-4b-llm-smoke-{scenario}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )
    print(
        f"[llm-smoke] {scenario}: category={record['category']} "
        f"confidence={record['confidence']} resolution={record['suggested_resolution']} "
        f"tools={record['tool_calls']} tokens={record['tokens']} retries={record['retries']} "
        f"channel={record['final_channel']} steps={record['steps']}"
    )
