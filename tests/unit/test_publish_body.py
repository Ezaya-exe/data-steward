"""Unit tests for the deterministic PR body (Phase 6a, ARCHITECTURE §12).

The body must always contain the required sections, render the verification
evidence honestly, and neutralize anything hostile that came from a model or
from data. Determinism is asserted directly (same inputs -> same Markdown).
"""

from __future__ import annotations

import datetime as dt
import uuid

from dqs.config import Settings
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
from dqs.models.pipeline import NodeResult
from dqs.models.root_cause import EvidenceItem, RootCause
from dqs.models.violation import Violation
from dqs.publish.body import REQUIRED_SECTIONS, REVIEW_FOOTER, render_pr_body
from dqs.publish.diff import FilePatch

NOW = dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.UTC)
INCIDENT_ID = uuid.UUID("12345678-aaaa-bbbb-cccc-ddddeeeeffff")


def _plan() -> FixPlan:
    return FixPlan(
        incident_id=INCIDENT_ID,
        batch_id="batch_20260131",
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        affected_table="raw.orders",
        affected_columns=["customer_id"],
        summary="Read cust_id when customer_id is absent.",
        edits=[
            Edit(
                path="models/staging/stg_orders.sql",
                old="customer_id::integer          as customer_id,",
                new="coalesce(customer_id::integer, cust_id::integer) as customer_id,",
            )
        ],
        rationale="The source renamed the column; coalesce keeps both histories readable.",
        expected_effect="No null customer ids in the fault batch.",
        risk="If cust_id is also absent, the staging column stays NULL and verification fails.",
    )


def _incident(*, hostile: bool = False) -> Incident:
    summary = (
        "The extract renamed customer_id to cust_id. @octocat see #1 <script>x</script> "
        "[link](https://evil.example)"
        if hostile
        else "customer_id was renamed to cust_id in the orders extract."
    )
    evidence = (
        "hostile finding @octocat #1 <script>x</script> "
        "[click](https://evil.example) token ghp_abcdefghijklmnopqrstuvwxyz"
        if hostile
        else "cust_id ranks first (name similarity 0.71, 100% value overlap)."
    )
    root_cause = RootCause(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        summary=summary,
        affected_table="raw.orders",
        affected_columns=["customer_id"],
        evidence=[EvidenceItem(tool="find_rename_candidates", finding=evidence)],
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
        failing_tests=[
            NodeResult(
                unique_id="test.dq_warehouse.not_null_stg_orders_customer_id",
                name="not_null_stg_orders_customer_id",
                status="fail",
                batch_id="batch_20260131",
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


def _verification() -> VerificationResult:
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
        row_counts={"stg_orders": 1000, "fct_orders": 1000},
        duration_s=12.5,
        sandbox_schema="dqs_sandbox_12345678",
        dbt_exit_code=0,
        contract_tables=["stg_orders"],
    )


def _patches() -> list[FilePatch]:
    return [
        FilePatch(
            path="models/staging/stg_orders.sql",
            old_text="old",
            new_text="new",
            patch=(
                "--- a/models/staging/stg_orders.sql\n"
                "+++ b/models/staging/stg_orders.sql\n"
                "@@ -1 +1 @@\n"
                "-old\n"
                "+new\n"
            ),
            added=1,
            removed=1,
        )
    ]


def test_body_contains_every_required_section() -> None:
    body = render_pr_body(_plan(), _incident(), _verification(), _patches())

    for section in REQUIRED_SECTIONS:
        assert section in body
    assert REVIEW_FOOTER in body
    assert "| model | layer | distance | column lineage |" in body
    assert "weekly_revenue" in body
    assert "analytics@example.com" in body
    assert "0.92" in body
    assert "dqs_sandbox_12345678" in body
    assert "100% of rows preserved" in body
    assert "| `stg_orders` | 1000 |" in body


def test_evidence_is_rendered_in_a_fenced_block() -> None:
    body = render_pr_body(_plan(), _incident(), _verification(), _patches())

    assert "### Evidence 1: `find_rename_candidates`" in body
    assert "```text" in body
    assert "cust_id ranks first" in body


def test_hostile_model_and_data_text_is_neutralized() -> None:
    body = render_pr_body(
        _plan().model_copy(update={"risk": "@octocat #1 <script>x</script> [x](https://evil)"}),
        _incident(hostile=True),
        _verification(),
        _patches(),
    )

    assert "@octocat" not in body
    assert "#1" not in body
    assert "<script>" not in body
    assert "](https://evil.example)" not in body
    assert "ghp_" not in body


def test_settings_secrets_are_redacted_from_the_body() -> None:
    settings = Settings(_env_file=None, github_token="ghp_settings_secret_value")
    body = render_pr_body(
        _plan().model_copy(update={"risk": "leaked ghp_settings_secret_value"}),
        _incident(),
        _verification(),
        _patches(),
        settings=settings,
    )

    assert "ghp_settings_secret_value" not in body


def test_same_inputs_produce_identical_markdown() -> None:
    first = render_pr_body(_plan(), _incident(), _verification(), _patches())
    second = render_pr_body(_plan(), _incident(), _verification(), _patches())

    assert first == second


def test_missing_root_cause_and_impact_are_stated_honestly() -> None:
    incident = _incident().model_copy(update={"root_cause": None, "impact": None})

    body = render_pr_body(_plan(), incident, _verification(), _patches())

    assert "## Root cause" in body
    assert "No valid root-cause record is stored" in body
    assert "## Impact" in body
    assert "No valid impact report is stored" in body


def test_failed_criteria_are_shown_but_the_renderer_does_not_refuse() -> None:
    failed = VerificationResult(
        passed=False,
        failed_criteria=["dbt_build"],
        criteria=[CriterionResult(name="dbt_build", passed=False, detail="exit 1")],
    )

    body = render_pr_body(_plan(), _incident(), failed, _patches())

    assert "- **Verdict:** FAIL" in body
    assert "- **Failed criteria:** `dbt_build`" in body
