"""CLI wiring tests for `dqs incident plan-fix` (Phase 5b).

No Postgres and no LLM: the command's dependencies are replaced at the CLI
boundary, so these tests cover argument handling, routing refusal, output and
exit codes without pretending to verify a fix.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from typer.testing import CliRunner

from dqs.agents.fixer import CandidateModel, FixAttempt, PlanFixResult
from dqs.cli import app as dqs_app
from dqs.config import Settings
from dqs.models.enums import (
    ImpactScope,
    IncidentStatus,
    ResolutionType,
    RootCauseCategory,
    Severity,
)
from dqs.models.fix import CriterionResult, Edit, FixPlan, VerificationResult
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause
from dqs.models.violation import Violation

NOW = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
INCIDENT_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _incident(category: RootCauseCategory = RootCauseCategory.SOURCE_COLUMN_RENAMED) -> Incident:
    return Incident(
        incident_id=INCIDENT_ID,
        fingerprint="fp",
        batch_id="batch_20260131",
        table="raw.orders",
        status=IncidentStatus.INVESTIGATING,
        severity=Severity.BREAKING,
        violations=[
            Violation(
                check="schema.missing_column",
                severity=Severity.BREAKING,
                table="raw.orders",
                column="customer_id",
                batch_id="batch_20260131",
                message="missing column",
            )
        ],
        root_cause=RootCause(
            category=category,
            summary="diagnosis",
            affected_table="raw.orders",
            affected_columns=["customer_id"],
            confidence=0.9,
            suggested_resolution=ResolutionType.ISSUE,
        ).model_dump(mode="json"),
        impact=ImpactReport(
            incident_id=INCIDENT_ID,
            source_table="raw.orders",
            column="customer_id",
            scope=ImpactScope.COLUMN,
            summary="impact",
        ).model_dump(mode="json"),
        created_at=NOW,
        updated_at=NOW,
    )


def _passed_result() -> PlanFixResult:
    plan = FixPlan(
        incident_id=INCIDENT_ID,
        batch_id="batch_20260131",
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        affected_table="raw.orders",
        affected_columns=["customer_id"],
        summary="use cust_id",
        edits=[
            Edit(
                path="models/staging/stg_orders.sql",
                old="customer_id as customer_id,",
                new="cust_id as customer_id,",
            )
        ],
        rationale="renamed",
        expected_effect="no nulls",
        risk="none",
    )
    verification = VerificationResult(
        passed=True,
        criteria=[CriterionResult(name="dbt_build", passed=True, detail="exit 0")],
        duration_s=1.0,
    )
    return PlanFixResult(
        incident_id=INCIDENT_ID,
        passed=True,
        exhausted=False,
        attempts=[
            FixAttempt(attempt_no=1, plan=plan, verification=verification, final_channel="x")
        ],
        tokens=12,
        retries=0,
        candidate_models=["stg_orders"],
    )


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    engine = _FakeEngine()
    monkeypatch.setattr("dqs.cli.load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("dqs.cli.dbmod.make_rw_engine", lambda settings: engine)
    monkeypatch.setattr(
        "dqs.cli.incidents_mod.get_incident", lambda engine, incident_id: _incident()
    )
    return engine


def test_plan_fix_refuses_an_issue_routed_incident(
    fake_cli: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "dqs.cli.incidents_mod.get_incident",
        lambda engine, incident_id: _incident(RootCauseCategory.UPSTREAM_NULLS_INTRODUCED),
    )
    monkeypatch.setattr("dqs.cli.fixer_mod.select_candidates", lambda incident, root_cause: [])

    result = CliRunner().invoke(dqs_app, ["incident", "plan-fix", str(INCIDENT_ID)])

    assert result.exit_code == 1
    assert "resolution=ISSUE" in result.output
    assert "plan-fix only runs for PR-eligible incidents" in result.output
    assert fake_cli.disposed


def test_plan_fix_requires_a_stored_root_cause(
    fake_cli: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = _incident().model_copy(update={"root_cause": None})
    monkeypatch.setattr("dqs.cli.incidents_mod.get_incident", lambda engine, incident_id: incident)

    result = CliRunner().invoke(dqs_app, ["incident", "plan-fix", str(INCIDENT_ID)])

    assert result.exit_code == 1
    assert "run investigate first" in result.output
    assert fake_cli.disposed


def test_plan_fix_prints_the_plan_and_attempts_for_a_passing_run(
    fake_cli: _FakeEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = CandidateModel(
        name="stg_orders",
        path="models/staging/stg_orders.sql",
        sql="select 1",
        contract_yaml=None,
    )
    monkeypatch.setattr(
        "dqs.cli.fixer_mod.select_candidates", lambda incident, root_cause: [candidate]
    )
    monkeypatch.setattr("dqs.cli.LLMClient.from_provider", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "dqs.cli.DbStepSink",
        lambda engine, **kwargs: _MemorySink(**kwargs),
    )
    monkeypatch.setattr(
        "dqs.cli.fixer_mod.plan_fix",
        lambda *args, **kwargs: _passed_result(),
    )

    result = CliRunner().invoke(dqs_app, ["incident", "plan-fix", str(INCIDENT_ID)])

    assert result.exit_code == 0, result.output
    assert "resolution=PR" in result.output
    assert "attempts=1" in result.output
    assert "PASS" in result.output
    assert '"summary": "use cust_id"' in result.output
    assert fake_cli.disposed


def test_plan_fix_rejects_a_non_uuid_argument() -> None:
    result = CliRunner().invoke(dqs_app, ["incident", "plan-fix", "not-a-uuid"])
    assert result.exit_code != 0
    assert "uuid" in result.output


class _MemorySink:
    """Minimal sink stand-in; the CLI never inspects it in this test."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
