"""Streamlit page smoke tests (AppTest), with no database, network or LLM.

Why a fake service module: the pages must be testable in empty and populated
states without Docker. `ui/common.py` reads the service and settings from
session state when present, so these tests exercise the real page code and the
real widgets while swapping only the data source. The one write (approve/reject)
is asserted through the fake, not by resuming a real checkpoint.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

from dqs import services
from dqs.config import Settings
from dqs.models.enums import IncidentStatus, ResolutionType, RootCauseCategory, Severity
from dqs.models.impact import AffectedExposure, AffectedModel, ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import EvidenceItem, RootCause
from dqs.models.violation import Violation
from dqs.services import (
    BenchmarkFile,
    BenchmarkMetadataRow,
    BenchmarkView,
    ChecklistItem,
    ContractView,
    DatabaseChoice,
    DecisionResult,
    FixAttemptView,
    IncidentDetail,
    IncidentRow,
    PublicationView,
    StepView,
)

UI_DIR = Path(__file__).resolve().parents[2] / "ui"
INCIDENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
NOW = dt.datetime(2026, 1, 31, 12, 0, tzinfo=dt.UTC)


# --- stub view models ------------------------------------------------------------------


def make_incident(status: IncidentStatus) -> Incident:
    return Incident(
        incident_id=INCIDENT_ID,
        fingerprint="fp-ui",
        batch_id="batch_20260131",
        table="raw.orders",
        status=status,
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
                sample=["<script>alert(1)</script>"],
            )
        ],
        root_cause=RootCause(
            category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
            summary="The source renamed status to order_status.",
            affected_table="raw.orders",
            affected_columns=["status"],
            evidence=[
                EvidenceItem(tool="run_readonly_sql", finding="column list shows order_status")
            ],
            confidence=0.72,
            suggested_resolution=ResolutionType.PR,
            hypotheses_rejected=["manual deletion"],
        ).model_dump(mode="json"),
        impact=ImpactReport(
            incident_id=INCIDENT_ID,
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
        ).model_dump(mode="json"),
        created_at=NOW,
        updated_at=NOW,
    )


def make_detail(status: IncidentStatus) -> IncidentDetail:
    incident = make_incident(status)
    return IncidentDetail(
        incident=incident,
        root_cause=RootCause.model_validate(incident.root_cause),
        impact=ImpactReport.model_validate(incident.impact),
        steps=[
            StepView(
                seq=1,
                node="investigator",
                kind="tool",
                name="run_readonly_sql",
                args_preview='{"sql": "select status from raw.orders"}',
                result_preview='{"rows": 3}',
                latency_ms=12,
                tokens_in=0,
                tokens_out=0,
                created_at=NOW,
            )
        ],
        attempts=[
            FixAttemptView(
                attempt_no=1,
                passed=False,
                verification_passed=False,
                failed_criteria=["row_preservation"],
                checklist=[
                    ChecklistItem(
                        name="targeted_check",
                        passed=True,
                        detail="the targeted check passes",
                        observed='{"nulls": 0}',
                    ),
                    ChecklistItem(
                        name="row_preservation",
                        passed=False,
                        detail="rows were dropped",
                        observed='{"before": 10, "after": 8}',
                    ),
                ],
                summary="Map the renamed source column",
                category="SOURCE_COLUMN_RENAMED",
                target_files=["models/staging/stg_orders.sql"],
                risk="low",
                expected_effect="the model builds with all rows",
                diff="--- a/models/staging/stg_orders.sql\n+++ b/models/staging/stg_orders.sql\n",
                created_at=NOW,
            )
        ],
        publications=[
            PublicationView(
                kind="pr",
                mode="dry_run",
                url="out/pr_previews/incident.patch",
                status="done",
                created_at=NOW,
            )
        ],
        latest_diff="--- a/models/staging/stg_orders.sql\n+++ b/models/staging/stg_orders.sql\n",
    )


class FakeService:
    """The subset of `dqs.services` the pages call, backed by stub data."""

    def __init__(
        self,
        *,
        rows: list[IncidentRow] | None = None,
        detail: IncidentDetail | None = None,
        contracts: list[ContractView] | None = None,
        files: list[BenchmarkFile] | None = None,
        benchmark: BenchmarkView | None = None,
    ) -> None:
        self.rows = rows or []
        self.detail = detail
        self.contracts = contracts or []
        self.files = files or []
        self.benchmark = benchmark
        self.decisions: list[dict[str, Any]] = []

    def database_choices(self, _settings: Settings) -> list[DatabaseChoice]:
        return [DatabaseChoice(name="dqs_demo", is_default=True)]

    def engine_for(self, _settings: Settings, _database: str) -> object:
        return object()

    def list_incident_rows(self, _engine: object) -> list[IncidentRow]:
        return list(self.rows)

    def filter_incident_rows(self, rows: list[IncidentRow], **kwargs: Any) -> list[IncidentRow]:
        return services.filter_incident_rows(rows, **kwargs)

    def incident_detail(self, _engine: object, _incident_id: uuid.UUID) -> IncidentDetail | None:
        return self.detail

    def list_contracts(self, _directory: Path) -> list[ContractView]:
        return list(self.contracts)

    def contract_counts(self, contracts: list[ContractView]) -> tuple[int, int]:
        return services.contract_counts(contracts)

    def list_benchmark_files(self, _directory: Path) -> list[BenchmarkFile]:
        return list(self.files)

    def benchmark_view(self, _name: str, _directory: Path) -> BenchmarkView:
        assert self.benchmark is not None
        return self.benchmark

    def decide_incident(
        self,
        _settings: Settings,
        incident_id: uuid.UUID,
        *,
        decision: str,
        approver: str,
        note: str = "",
        database: str | None = None,
    ) -> DecisionResult:
        self.decisions.append(
            {
                "incident_id": incident_id,
                "decision": decision,
                "approver": approver,
                "note": note,
                "database": database,
            }
        )
        return DecisionResult(
            incident_id=incident_id,
            status="rejected" if decision == "reject" else "resolved",
            completed=True,
            message=f"Incident {incident_id} is now {decision}d.",
        )


def settings_for(mode: str) -> Settings:
    return Settings(_env_file=None, github_mode=mode)


def open_app(fake: FakeService, mode: str = "dry_run") -> AppTest:
    at = AppTest.from_file(str(UI_DIR / "app.py"), default_timeout=30)
    at.session_state["_dqs_service"] = fake
    at.session_state["_dqs_settings"] = settings_for(mode)
    at.run()
    assert not at.exception, [element.value for element in at.exception]
    return at


def visit(at: AppTest, page: str) -> AppTest:
    at.switch_page(f"pages/{page}").run()
    assert not at.exception, [element.value for element in at.exception]
    return at


def text_values(at: AppTest) -> str:
    return "\n".join(element.value for element in at.text)


# --- Incidents page --------------------------------------------------------------------


def test_incidents_page_empty_state() -> None:
    at = open_app(FakeService())
    assert any("No incidents in this database" in info.value for info in at.info)
    assert len(at.dataframe) == 0


def test_incidents_page_populated_and_filtered() -> None:
    resolved = services.incident_row(
        make_incident(IncidentStatus.RESOLVED).model_copy(
            update={
                "incident_id": uuid.UUID("44444444-4444-4444-4444-444444444444"),
                "table": "raw.customers",
                "severity": Severity.WARNING,
            }
        )
    )
    opened = services.incident_row(make_incident(IncidentStatus.OPEN))
    at = open_app(FakeService(rows=[opened, resolved]))

    assert any("2 of 2 incidents" in caption.value for caption in at.caption)
    assert len(at.dataframe[0].value) == 2

    at.multiselect(key="filter_status").set_value(["resolved"]).run()
    assert len(at.dataframe[0].value) == 1
    assert at.dataframe[0].value.iloc[0]["Table"] == "raw.customers"

    at.text_input(key="filter_table").set_value("orders").run()
    assert any("No incident matches these filters" in info.value for info in at.info)


# --- Incident detail page --------------------------------------------------------------


def test_detail_page_empty_state() -> None:
    at = open_app(FakeService())
    visit(at, "incident_detail.py")
    assert any("No incidents in this database" in info.value for info in at.info)


def test_detail_page_renders_the_approval_payload() -> None:
    at = open_app(
        FakeService(
            rows=[services.incident_row(make_incident(IncidentStatus.AWAITING_APPROVAL))],
            detail=make_detail(IncidentStatus.AWAITING_APPROVAL),
        )
    )
    visit(at, "incident_detail.py")

    texts = text_values(at)
    assert "SOURCE_COLUMN_RENAMED" in texts
    assert "model-reported, uncalibrated" in texts
    assert "run_readonly_sql" in texts
    assert "row_preservation" in texts
    exposures = [frame.value for frame in at.dataframe if "Exposure" in frame.value.columns]
    assert exposures and exposures[0].iloc[0]["Exposure"] == "orders_dashboard"
    # The hostile sample lives in the violations table, never in rendered markdown.
    assert "<script>" not in "\n".join(markdown.value for markdown in at.markdown)

    checks = [item.value for item in at.code]
    assert any("+++ b/models/staging/stg_orders.sql" in item for item in checks)


def test_detail_page_approval_actions_are_enabled_for_awaiting_dry_run() -> None:
    at = open_app(
        FakeService(
            rows=[services.incident_row(make_incident(IncidentStatus.AWAITING_APPROVAL))],
            detail=make_detail(IncidentStatus.AWAITING_APPROVAL),
        ),
        mode="dry_run",
    )
    visit(at, "incident_detail.py")

    assert at.button(key="approve").disabled is False
    assert at.button(key="reject").disabled is True  # a note is required
    assert any("dry_run" in info.value for info in at.info)
    assert len(at.checkbox) == 0


def test_detail_page_rejection_resumes_with_a_note() -> None:
    fake = FakeService(
        rows=[services.incident_row(make_incident(IncidentStatus.AWAITING_APPROVAL))],
        detail=make_detail(IncidentStatus.AWAITING_APPROVAL),
    )
    at = open_app(fake, mode="dry_run")
    visit(at, "incident_detail.py")

    at.text_area(key="decision_note").set_value("the source fix is wrong").run()
    assert at.button(key="reject").disabled is False
    at.button(key="reject").click().run()

    assert not at.exception, [element.value for element in at.exception]
    assert fake.decisions == [
        {
            "incident_id": INCIDENT_ID,
            "decision": "reject",
            "approver": "ui-operator",
            "note": "the source fix is wrong",
            "database": "dqs_demo",
        }
    ]
    assert any("is now reject" in success.value for success in at.success)


def test_detail_page_live_mode_needs_explicit_confirmation() -> None:
    at = open_app(
        FakeService(
            rows=[services.incident_row(make_incident(IncidentStatus.AWAITING_APPROVAL))],
            detail=make_detail(IncidentStatus.AWAITING_APPROVAL),
        ),
        mode="live",
    )
    visit(at, "incident_detail.py")

    assert at.button(key="approve").disabled is True
    assert any("LIVE" in warning.value for warning in at.warning)

    at.checkbox(key="live_confirm").check().run()
    assert at.button(key="approve").disabled is False


def test_detail_page_hides_actions_when_not_awaiting_approval() -> None:
    at = open_app(
        FakeService(
            rows=[services.incident_row(make_incident(IncidentStatus.RESOLVED))],
            detail=make_detail(IncidentStatus.RESOLVED),
        )
    )
    visit(at, "incident_detail.py")

    assert len(at.button) == 0
    assert any("status is resolved" in info.value for info in at.info)


# --- Contracts page --------------------------------------------------------------------


def make_contract(table: str, status: str, yaml_text: str) -> ContractView:
    return ContractView(
        table=table,
        layer="raw",
        status=status,
        generated_at=NOW,
        yaml=yaml_text,
    )


def test_contracts_page_empty_state() -> None:
    at = open_app(FakeService())
    visit(at, "contracts.py")
    assert any("No contracts found" in info.value for info in at.info)


def test_contracts_page_lists_and_renders_yaml() -> None:
    at = open_app(
        FakeService(
            contracts=[
                make_contract(
                    "raw.customers", "approved", "table: raw.customers\nstatus: approved\n"
                ),
                make_contract("raw.orders", "draft", "table: raw.orders\nstatus: draft\n"),
            ]
        )
    )
    visit(at, "contracts.py")

    assert len(at.dataframe[0].value) == 2
    assert [metric.value for metric in at.metric] == ["1", "1", "2"]
    assert any("table: raw.customers" in code.value for code in at.code)

    at.selectbox(key="contract_table").set_value("raw.orders").run()
    assert any("table: raw.orders" in code.value for code in at.code)


# --- Benchmark page --------------------------------------------------------------------


def make_benchmark_view() -> BenchmarkView:
    return BenchmarkView(
        name="20260131T120000Z_test-model.json",
        model="test-model",
        split="dev",
        notes=["Pilot run: only 4 catalogue scenarios."],
        metadata=[
            BenchmarkMetadataRow(label="Model", value="test-model"),
            BenchmarkMetadataRow(label="Code fingerprint", value="abc123"),
            BenchmarkMetadataRow(label="Harness version", value="8b.1"),
            BenchmarkMetadataRow(label="Prompt hashes", value="investigator=deadbeef"),
        ],
        report_markdown="# Benchmark - test-model\n\n## Overall\n\n| scope | n |\n|---|---:|\n",
        infra_errors=2,
        scenario_count=13,
        fault_kinds=["clean", "null_spike"],
    )


def test_benchmark_page_empty_state() -> None:
    at = open_app(FakeService())
    visit(at, "benchmark.py")
    assert any("No benchmark results found" in info.value for info in at.info)


def test_benchmark_page_shows_notes_metadata_and_report() -> None:
    at = open_app(
        FakeService(
            files=[
                BenchmarkFile(
                    name="20260131T120000Z_test-model.json",
                    size_bytes=4096,
                    modified_at=NOW,
                )
            ],
            benchmark=make_benchmark_view(),
        )
    )
    visit(at, "benchmark.py")

    assert any("Pilot run" in warning.value for warning in at.warning)
    assert [metric.value for metric in at.metric] == ["13", "2", "2"]
    assert len(at.dataframe[0].value) == 4
    assert any("## Overall" in markdown.value for markdown in at.markdown)
    assert any("Wilson" in caption.value for caption in at.caption)


# --- static safety guards --------------------------------------------------------------


def test_ui_contains_no_raw_sql_and_no_unsafe_html() -> None:
    files = sorted(UI_DIR.rglob("*.py"))
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "SELECT " not in text, f"raw SQL in {path}"
        assert "FROM dq." not in text, f"raw SQL in {path}"
        assert ".connect()" not in text, f"database connection in {path}"
        assert not re.search(r"unsafe_allow_html\s*=\s*True", text), f"unsafe HTML in {path}"
