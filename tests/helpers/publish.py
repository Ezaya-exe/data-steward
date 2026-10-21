"""Shared fixture builders for publish tests (Phase 6a).

Plain functions, not pytest fixtures, so unit and integration tests can build
the same incident/plan/verification without importing each other's modules.
"""

from __future__ import annotations

import datetime as dt
import uuid

from dqs.models.enums import (
    ImpactScope,
    IncidentStatus,
    ResolutionType,
    RootCauseCategory,
    Severity,
)
from dqs.models.fix import CriterionResult, Edit, FixPlan, VerificationResult
from dqs.models.impact import AffectedExposure, AffectedModel, ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import EvidenceItem, RootCause
from dqs.models.violation import Violation
from dqs.publish.base import ApprovalDecision

NOW = dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.UTC)
INCIDENT_ID = uuid.UUID("12345678-aaaa-bbbb-cccc-ddddeeeeffff")
F1_OLD = "    customer_id::integer          as customer_id,"
F1_NEW = "    coalesce(customer_id::integer, cust_id::integer) as customer_id,"


def make_plan() -> FixPlan:
    return FixPlan(
        incident_id=INCIDENT_ID,
        batch_id="batch_20260131",
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        affected_table="raw.orders",
        affected_columns=["customer_id"],
        summary="Read cust_id when customer_id is absent.",
        edits=[Edit(path="models/staging/stg_orders.sql", old=F1_OLD, new=F1_NEW)],
        rationale="The source renamed the column; coalesce keeps both histories readable.",
        expected_effect="No null customer ids in the fault batch.",
        risk="If cust_id is also absent, the column stays NULL and verification fails.",
    )


def make_incident() -> Incident:
    root_cause = RootCause(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        summary="customer_id was renamed to cust_id in the orders extract.",
        affected_table="raw.orders",
        affected_columns=["customer_id"],
        evidence=[
            EvidenceItem(
                tool="find_rename_candidates",
                finding="cust_id ranks first (name similarity 0.71, 100% value overlap).",
            )
        ],
        confidence=0.92,
        suggested_resolution=ResolutionType.PR,
        hypotheses_rejected=["The loader dropped the column."],
    )
    impact = ImpactReport(
        incident_id=INCIDENT_ID,
        source_table="raw.orders",
        column="customer_id",
        scope=ImpactScope.COLUMN,
        affected_models=[
            AffectedModel(
                unique_id="model.dq_warehouse.stg_orders",
                name="stg_orders",
                layer="staging",
                distance=1,
                referencing_column=True,
            )
        ],
        affected_exposures=[
            AffectedExposure(
                unique_id="exposure.dq_warehouse.weekly_revenue",
                name="weekly_revenue",
                owner="analytics@example.com",
                maturity="high",
            )
        ],
        blast_radius_score=42.0,
        summary="Two downstream models and the weekly revenue dashboard are affected.",
    )
    return Incident(
        incident_id=INCIDENT_ID,
        fingerprint="abcdef0123456789",
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
                message="contract column customer_id is missing from the batch header",
            )
        ],
        root_cause=root_cause.model_dump(mode="json"),
        impact=impact.model_dump(mode="json"),
        created_at=NOW,
        updated_at=NOW + dt.timedelta(hours=1),
    )


def make_verification(*, passed: bool = True) -> VerificationResult:
    if not passed:
        return VerificationResult(
            passed=False,
            failed_criteria=["dbt_build"],
            criteria=[CriterionResult(name="dbt_build", passed=False, detail="exit 1")],
        )
    return VerificationResult(
        passed=True,
        criteria=[
            CriterionResult(name="dbt_build", passed=True, detail="dbt build exited 0"),
            CriterionResult(name="dbt_tests", passed=True, detail="all tests passed"),
            CriterionResult(
                name="contract_conformance",
                passed=True,
                detail="published staging contract matched",
                observed={"columns": 7, "violations": 0},
            ),
            CriterionResult(
                name="row_preservation",
                passed=True,
                detail="100% of rows preserved",
                observed={"before": 1000, "after": 1000},
            ),
        ],
        dbt_log_excerpt="16:00:00  Completed successfully",
        row_counts={"stg_orders": 1000},
        duration_s=12.5,
        sandbox_schema="dqs_sandbox_12345678",
        dbt_exit_code=0,
        contract_tables=["stg_orders"],
    )


def make_approval(decision: str = "approve") -> ApprovalDecision:
    payload = {
        "approver": "shravan",
        "timestamp": NOW,
        "decision": "approve",
    }
    if decision != "approve":
        payload["decision"] = decision
    return ApprovalDecision.model_validate(payload)
