"""Integration test: read the UI view models from a real cloned warehouse.

Why a clone: the service layer's SQL (`dq.agent_steps`, `dq.fix_attempts`,
`dq.publications`) must be exercised against Postgres, not a mock. Cloning
`dqs_template` gives the real schema in seconds; the test drops only its own
`dqs_ui_it_*` database in `finally`, so it can never touch the template, the
warehouse, or a benchmark scenario database.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from dqs import fast_reset, services
from dqs import incidents as incidents_mod
from dqs.agents.steps import DbStepSink
from dqs.config import Settings, load_settings
from dqs.db import make_rw_engine
from dqs.models.enums import (
    IncidentStatus,
    ResolutionType,
    RootCauseCategory,
    Severity,
)
from dqs.models.fix import CriterionResult, Edit, FixPlan, VerificationResult
from dqs.models.impact import AffectedExposure, AffectedModel, ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import EvidenceItem, RootCause
from dqs.models.state import WorkflowOutcome
from dqs.models.violation import Violation
from dqs.publish.base import PublicationKind, PublicationMode, PublicationResult
from dqs.publish.store import DbPublicationStore
from dqs.verify.sandbox import persist_attempt

pytestmark = pytest.mark.integration


def _seed(engine: Any, settings: Settings) -> uuid.UUID:
    """Create one incident with every artifact the detail page renders."""
    draft = incidents_mod.IncidentDraft(
        fingerprint="ui-it-fingerprint",
        table="raw.orders",
        batch_id="batch_20260131",
        severity=Severity.BREAKING,
        violations=[
            Violation(
                check="nulls.rate",
                severity=Severity.BREAKING,
                table="raw.orders",
                column="status",
                batch_id="batch_20260131",
                message="null rate 0.40 exceeds 0.10",
                observed={"null_rate": 0.4},
                expected={"max_null_rate": 0.1},
            )
        ],
    )
    incident_id = incidents_mod.persist_incidents(engine, [draft]).created[0]

    root = RootCause(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        summary="The source renamed status to order_status.",
        affected_table="raw.orders",
        affected_columns=["status"],
        evidence=[EvidenceItem(tool="run_readonly_sql", finding="column list shows order_status")],
        confidence=0.7,
        suggested_resolution=ResolutionType.PR,
    )
    incidents_mod.save_root_cause(engine, incident_id, root.model_dump(mode="json"))
    impact = ImpactReport(
        incident_id=incident_id,
        source_table="raw.orders",
        column="status",
        scope="column",
        affected_models=[
            AffectedModel(
                unique_id="model.proj.stg_orders",
                name="stg_orders",
                layer="staging",
                distance=1,
            )
        ],
        affected_exposures=[
            AffectedExposure(
                unique_id="exposure.proj.dashboard",
                name="orders_dashboard",
                owner="analytics",
                maturity="high",
            )
        ],
        blast_radius_score=2.0,
        summary="One staging model and one dashboard.",
    )
    incidents_mod.save_impact(engine, incident_id, impact.model_dump(mode="json"))
    incidents_mod.set_status(
        engine,
        incident_id,
        IncidentStatus.AWAITING_APPROVAL,
        reason="ui integration test",
    )

    DbStepSink(
        engine,
        node="investigator",
        incident_id=incident_id,
        settings=settings,
    ).record(
        kind="tool",
        name="run_readonly_sql",
        input={"sql": "select 1"},
        output={"rows": 1},
        latency_ms=4,
    )

    plan = FixPlan(
        incident_id=incident_id,
        batch_id="batch_20260131",
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        affected_table="raw.orders",
        affected_columns=["status"],
        summary="Cast the source column to text",
        edits=[
            Edit(
                path="models/staging/stg_orders.sql",
                old="    status,\n",
                new="    status::text as status,\n",
            )
        ],
        rationale="the renamed column is text in the source",
        expected_effect="the model builds and preserves rows",
        risk="low: one cast",
    )
    verification = VerificationResult.from_criteria(
        [
            CriterionResult(
                name="targeted_check",
                passed=True,
                detail="the targeted check passes",
                observed={"nulls": 0},
            ),
            CriterionResult(
                name="row_preservation",
                passed=True,
                detail="row count unchanged",
                observed={"before": 10, "after": 10},
            ),
        ]
    )
    persist_attempt(engine, plan, verification, attempt_no=1)

    store = DbPublicationStore(engine)
    store.begin(incident_id, PublicationKind.PR, PublicationMode.DRY_RUN)
    store.complete(
        PublicationResult(
            incident_id=incident_id,
            kind=PublicationKind.PR,
            mode=PublicationMode.DRY_RUN,
            url="out/pr_previews/ui-it.patch",
        )
    )
    return incident_id


def test_ui_services_round_trip_on_a_cloned_template() -> None:
    base = load_settings()
    if not fast_reset.template_exists(base):
        pytest.skip("dqs_template is absent; run `dqs bench preflight` first")

    name = f"dqs_ui_it_{uuid.uuid4().hex[:8]}"
    scoped = base.model_copy(update={"pg_database": name})
    engine = None
    try:
        fast_reset.clone_template(base, name)
        engine = make_rw_engine(scoped)
        incident_id = _seed(engine, scoped)

        rows = services.list_incident_rows(engine)
        selected = services.filter_incident_rows(rows, search=str(incident_id))
        assert selected, "the seeded incident is not in the incident table"
        assert selected[0].status == "awaiting_approval"
        assert selected[0].table == "raw.orders"
        assert "nulls.rate" in selected[0].summary

        detail = services.incident_detail(engine, incident_id)
        assert detail is not None
        assert detail.incident.status is IncidentStatus.AWAITING_APPROVAL
        assert detail.root_cause is not None
        assert detail.root_cause.category is RootCauseCategory.SOURCE_COLUMN_RENAMED
        assert detail.impact is not None
        assert detail.impact.affected_exposures[0].owner == "analytics"
        assert [step.name for step in detail.steps] == ["run_readonly_sql"]
        assert len(detail.attempts) == 1
        assert "status::text as status" in detail.latest_diff
        assert detail.attempts[0].checklist[0].name == "targeted_check"
        assert '"nulls": 0' in detail.attempts[0].checklist[0].observed
        assert len(detail.publications) == 1
        assert detail.publications[0].status == "done"
        assert detail.publications[0].mode == "dry_run"

        calls: list[dict[str, Any]] = []

        def fake_resume(
            _settings: Settings,
            _engine: Any,
            resumed_id: uuid.UUID,
            *,
            decision: str,
            approver: str,
            note: str,
        ) -> WorkflowOutcome:
            calls.append(
                {
                    "incident_id": resumed_id,
                    "decision": decision,
                    "approver": approver,
                    "note": note,
                }
            )
            return WorkflowOutcome(
                incident_id=resumed_id,
                status=IncidentStatus.RESOLVED,
                completed=True,
                checkpoint_path="not-used",
            )

        result = services.decide_incident(
            scoped,
            incident_id,
            decision="approve",
            approver="integration-test",
            note="looks good",
            engine=engine,
            resume=fake_resume,
        )
        assert result.status == "resolved"
        assert calls == [
            {
                "incident_id": incident_id,
                "decision": "approve",
                "approver": "integration-test",
                "note": "looks good",
            }
        ]
        assert isinstance(incidents_mod.get_incident(engine, incident_id), Incident)
    finally:
        if engine is not None:
            engine.dispose()
        fast_reset.drop_database(base, name)
        assert name not in fast_reset.list_steward_databases(base)
