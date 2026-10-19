"""Unit tests for the ImpactReport builder and its scoring formula (DECISIONS 60).

Pure tests over the fixture graph and hand-built Incident objects; no DB, no
dbt, no LLM. The summary must come from the deterministic template here — the
Phase-4 LLM hook replaces it only in later phases.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

import pytest

from dqs.lineage.graph import LineageGraph
from dqs.lineage.impact import build_impact_report, render_summary
from dqs.models.enums import IncidentStatus, Severity
from dqs.models.incident import Incident
from dqs.models.pipeline import NodeResult, PipelineRunRecord
from dqs.models.violation import Violation

NOW = dt.datetime(2026, 10, 19, tzinfo=dt.UTC)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manifest_fixture.json"


def _violation(
    table: str, column: str | None, check: str, severity: Severity = Severity.BREAKING
) -> Violation:
    return Violation(
        check=check, severity=severity, table=table, column=column, batch_id="batch_20260205",
        message=f"{check} on {table}.{column}",
    )


def _incident(
    table: str | None, violations: list[Violation]
) -> Incident:
    return Incident(
        incident_id=uuid.uuid4(),
        fingerprint="x",
        batch_id=violations[0].batch_id if violations else None,
        table=table,
        status=IncidentStatus.OPEN,
        severity=max((v.severity for v in violations), key=lambda s: s.value) if violations else Severity.WARNING,
        violations=violations,
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture(scope="module")
def graph() -> LineageGraph:
    return LineageGraph.from_manifest(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_column_incident_uses_column_level_lineage(graph: LineageGraph) -> None:
    incident = _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")])
    report = build_impact_report(graph, incident)
    assert report.scope.value == "column"
    names = [m.name for m in report.affected_models]
    assert names == ["stg_orders"]
    assert report.affected_models[0].referencing_column is True
    assert report.affected_models[0].layer == "staging"
    # score = 100 × (high 1.0 + medium 0.6 + low 0.3) + 10 × (1/1) = 200.0
    assert report.blast_radius_score == pytest.approx(200.0)


def test_column_incident_keeps_only_exposures_fed_by_referencing_models(
    graph: LineageGraph,
) -> None:
    incident = _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")])
    report = build_impact_report(graph, incident)
    # stg_orders is upstream of all three exposures' chains? revenue_dashboard
    # sits on fct_daily_revenue ← fct_orders ← stg_orders, yes; and ops+360 too.
    assert [e.name for e in report.affected_exposures] == [
        "customer_360_dashboard",
        "ops_fulfilment_dashboard",
        "revenue_dashboard",
    ]


def test_table_scoped_incident_without_column_hits_all_downstream(
    graph: LineageGraph,
) -> None:
    # F9-like: whole table absent (volume.rows has column=None on raw.payments)
    incident = _incident("raw.payments", [_violation("raw.payments", None, "volume.rows")])
    report = build_impact_report(graph, incident)
    assert report.scope.value == "table"
    names = [m.name for m in report.affected_models]
    assert names == ["stg_payments", "fct_orders", "dim_customers", "fct_daily_revenue"]
    # not the products' exposures: all three dashboards sit downstream though
    assert len(report.affected_exposures) == 3


def test_batch_level_incident_without_table_is_warehouse_scope(
    graph: LineageGraph,
) -> None:
    # F8-like: freshness.sla is a batch-level check with table=None
    incident = _incident(None, [_violation("raw.payments", None, "freshness.sla")])
    report = build_impact_report(graph, incident)
    assert report.scope.value == "warehouse"
    # every model in the DAG (all 8), stg_* nearest
    assert len(report.affected_models) == 8
    assert report.affected_models[0].name == "stg_customers"
    assert len(report.affected_exposures) == 3


def test_failing_tests_come_from_the_stored_record_not_live_files(
    graph: LineageGraph,
) -> None:
    incident = _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")])
    record = PipelineRunRecord(
        run_id=7,
        batch_id="batch_20260205",
        status="failed",
        failed_nodes=[
            NodeResult(
                unique_id="test.dq_warehouse.not_null_stg_orders_customer_id.af79",
                name="not_null_stg_orders_customer_id",
                status="error",
                message="column customer_id does not exist",
                batch_id="batch_20260205",
            )
        ],
    )
    report = build_impact_report(graph, incident, record)
    assert len(report.failing_tests) == 1
    assert report.failing_tests[0].unique_id.endswith("af79")


def test_no_stored_record_leaves_failing_tests_empty(graph: LineageGraph) -> None:
    incident = _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")])
    report = build_impact_report(graph, incident, None)
    assert report.failing_tests == []


def test_score_formula_documented(graph: LineageGraph) -> None:
    from dqs.lineage.impact import blast_radius_score

    models = [
        _model("a", 1),
        _model("b", 2),
    ]
    exposures = [_exposure("dash", "high"), _exposure("other", "low")]
    # models: 10*(1/1 + 1/2) = 15.0 ; exposures: 100*(1.0 + 0.3) = 130.0
    assert blast_radius_score(models, exposures) == pytest.approx(145.0)


def _model(name: str, distance: int):
    from dqs.models.impact import AffectedModel

    return AffectedModel(unique_id=f"model.x.{name}", name=name, layer="staging", distance=distance)


def _exposure(name: str, maturity: str):
    from dqs.models.impact import AffectedExposure

    return AffectedExposure(
        unique_id=f"exposure.x.{name}", name=name, owner="o@example.com", maturity=maturity
    )


def test_report_is_deterministic(graph: LineageGraph) -> None:
    incident = _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")])
    one = build_impact_report(graph, incident)
    two = build_impact_report(graph, _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")]))
    a = one.model_dump(mode="json")
    b = two.model_dump(mode="json")
    a.pop("incident_id")
    b.pop("incident_id")
    assert a == b


def test_summary_is_the_template_not_an_llm_call(graph: LineageGraph) -> None:
    incident = _incident("raw.orders", [_violation("raw.orders", "customer_id", "schema.missing_column")])
    report = build_impact_report(graph, incident)
    assert "blast radius score" in report.summary.lower()
    assert "stg_orders" in report.summary


def test_render_summary_hook_marker_is_reachable(graph: LineageGraph) -> None:
    # The Phase-4 hook is render_summary's body; the template must keep working
    # even for the empty-fallback case (no models, no exposures, no record).
    from dqs.models.impact import ImpactReport

    empty = ImpactReport(
        incident_id=uuid.uuid4(),
        scope="column",
        source_table="raw.orders",
        column="customer_id",
    )
    text = render_summary(empty)
    assert "none downstream" in text
    assert "no exposures impacted" in text
