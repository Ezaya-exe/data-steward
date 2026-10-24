"""Unit tests for the UI service layer (no DB, no Streamlit, no LLM).

The DB-backed readers are exercised by the integration test; here the pure
builders and every guard the UI relies on are pinned with stub data. That split
matters because a view model is what the human approves — a wrong checklist or a
missing rejection note is a product bug, not a rendering detail.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import Engine

from dqs import services
from dqs.bench.report import BenchMetadata, BenchRunFile
from dqs.bench.scoring import ScenarioRun
from dqs.config import Settings
from dqs.errors import ConfigError, DqsError, WorkflowError
from dqs.models.enums import (
    ContractStatus,
    IncidentStatus,
    Layer,
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

INCIDENT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
NOW = dt.datetime(2026, 1, 31, 12, 0, tzinfo=dt.UTC)


def make_incident(**overrides: Any) -> Incident:
    payload: dict[str, Any] = {
        "incident_id": INCIDENT_ID,
        "fingerprint": "fp-abc123",
        "batch_id": "batch_20260131",
        "table": "raw.orders",
        "status": IncidentStatus.OPEN,
        "severity": Severity.BREAKING,
        "violations": [
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
        "created_at": NOW,
        "updated_at": NOW,
    }
    payload.update(overrides)
    return Incident(**payload)


def make_metadata(**overrides: Any) -> BenchMetadata:
    payload: dict[str, Any] = {
        "run_id": "20260131T120000Z",
        "model": "test-model",
        "provider": "fake",
        "split": "dev",
        "started_at": "2026-01-31T12:00:00Z",
        "finished_at": "2026-01-31T13:00:00Z",
        "git_sha": "deadbeef",
        "harness_version": "8b.1",
        "prompt_hashes": {"investigator": "a" * 40},
        "daily_token_cap": 180000,
        "tpm_limit": 9000,
        "pause_seconds": 0.0,
        "llm_retry_max_wait_seconds": 0.0,
        "catalogue_size": 46,
        "fault_count": 40,
        "control_count": 6,
        "holdout_count": 50,
    }
    payload.update(overrides)
    return BenchMetadata(**payload)


def make_plan(path: str = "models/staging/stg_orders.sql") -> FixPlan:
    return FixPlan(
        incident_id=INCIDENT_ID,
        batch_id="batch_20260131",
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        affected_table="raw.orders",
        affected_columns=["status"],
        summary="Map the renamed source column",
        edits=[Edit(path=path, old="select status", new="select order_status")],
        rationale="the source renamed status to order_status",
        expected_effect="the model builds and preserves rows",
        risk="low: one projection alias",
    )


def make_verification() -> VerificationResult:
    return VerificationResult.from_criteria(
        [
            CriterionResult(
                name="targeted_check",
                passed=True,
                detail="targeted check now passes",
                observed={"nulls": 0, "rows": 10},
            ),
            CriterionResult(
                name="row_preservation",
                passed=True,
                detail="row count unchanged",
                observed={"before": 10, "after": 10},
            ),
        ]
    )


# --- database selection ----------------------------------------------------------------


def test_database_names_never_exposes_benchmark_or_template() -> None:
    names = services.database_names(
        ["dqs_demo", "dqs_bench_abcd1234", "dqs_template", "dqs_ui_it_1234"],
        configured="warehouse",
    )
    assert names == ["warehouse", "dqs_demo"]
    assert services.default_database(names) == "dqs_demo"


def test_database_names_falls_back_to_warehouse() -> None:
    names = services.database_names(["dqs_bench_only"], configured="warehouse")
    assert names == ["warehouse"]
    assert services.default_database(names) == "warehouse"
    assert services.default_database([]) == "warehouse"


@pytest.mark.parametrize("name", ["dqs_bench_abcd1234", "dqs_template", "Warehouse", "bad name"])
def test_validate_database_name_rejects_unsafe_targets(name: str) -> None:
    with pytest.raises(ConfigError):
        services.validate_database_name(name)


def test_validate_database_name_allows_demo() -> None:
    assert services.validate_database_name("dqs_demo") == "dqs_demo"


def test_effective_publish_mode() -> None:
    dry = Settings(_env_file=None, github_mode="dry_run")
    live = Settings(_env_file=None, github_mode="live")
    assert services.effective_publish_mode(dry) == "dry_run"
    assert services.effective_publish_mode(live) == "live"


# --- incident rows and filters ---------------------------------------------------------


def test_incident_row_flattens_the_representative_violation() -> None:
    row = services.incident_row(make_incident())
    assert row.incident_id == INCIDENT_ID
    assert row.status == "open"
    assert row.severity == "breaking"
    assert row.table == "raw.orders"
    assert "nulls.rate" in row.summary
    assert "status" in row.summary


def test_filter_incident_rows_combines_filters() -> None:
    open_row = services.incident_row(make_incident())
    warning_row = services.incident_row(
        make_incident(
            incident_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
            status=IncidentStatus.RESOLVED,
            severity=Severity.WARNING,
            table="raw.customers",
            fingerprint="fp-xyz",
        )
    )
    rows = [open_row, warning_row]

    assert services.filter_incident_rows(rows) == rows
    assert services.filter_incident_rows(rows, statuses=["resolved"]) == [warning_row]
    assert services.filter_incident_rows(rows, severities=["breaking"]) == [open_row]
    assert services.filter_incident_rows(rows, table="CUSTOMERS") == [warning_row]
    assert services.filter_incident_rows(rows, search="fp-xyz") == [warning_row]
    assert services.filter_incident_rows(rows, statuses=["open"], table="customers") == []


def test_require_awaiting_approval_gate() -> None:
    waiting = make_incident(status=IncidentStatus.AWAITING_APPROVAL)
    assert services.require_awaiting_approval(waiting, INCIDENT_ID) is waiting
    with pytest.raises(WorkflowError):
        services.require_awaiting_approval(make_incident(), INCIDENT_ID)
    with pytest.raises(WorkflowError):
        services.require_awaiting_approval(None, INCIDENT_ID)


# --- fix attempts ----------------------------------------------------------------------


def test_build_attempt_view_renders_diff_and_checklist(tmp_path: Path) -> None:
    model = tmp_path / "models" / "staging" / "stg_orders.sql"
    model.parent.mkdir(parents=True)
    model.write_text("select status\nfrom raw.orders\n", encoding="utf-8")

    attempt = services.build_attempt_view(
        1,
        make_plan().model_dump(mode="json"),
        make_verification().model_dump(mode="json"),
        passed=True,
        created_at=NOW,
        project_dir=tmp_path,
    )

    assert attempt.verification_passed is True
    assert attempt.failed_criteria == []
    assert attempt.target_files == ["models/staging/stg_orders.sql"]
    assert "+select order_status" in attempt.diff
    assert "-select status" in attempt.diff
    assert attempt.diff_error == ""
    assert [item.name for item in attempt.checklist] == ["targeted_check", "row_preservation"]
    assert '"nulls": 0' in attempt.checklist[0].observed


def test_build_attempt_view_reports_an_unappliable_edit(tmp_path: Path) -> None:
    model = tmp_path / "models" / "staging" / "stg_orders.sql"
    model.parent.mkdir(parents=True)
    model.write_text("select status\n", encoding="utf-8")
    plan = make_plan().model_copy(
        update={"edits": [Edit(path="models/staging/stg_orders.sql", old="absent", new="x")]}
    )

    attempt = services.build_attempt_view(
        1,
        plan.model_dump(mode="json"),
        make_verification().model_dump(mode="json"),
        project_dir=tmp_path,
    )

    assert attempt.diff == ""
    assert "absent" in attempt.diff_error or "once" in attempt.diff_error


def test_build_attempt_view_survives_a_corrupt_plan() -> None:
    attempt = services.build_attempt_view(1, {"summary": "x"}, {})
    assert attempt.diff == ""
    assert "does not validate" in attempt.diff_error
    assert attempt.checklist == []


# --- incident detail ------------------------------------------------------------------


def test_build_incident_detail_parses_root_cause_and_impact() -> None:
    root = RootCause(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        summary="source renamed status",
        affected_table="raw.orders",
        affected_columns=["status"],
        evidence=[EvidenceItem(tool="run_readonly_sql", finding="column list shows order_status")],
        confidence=0.7,
        suggested_resolution=ResolutionType.PR,
    )
    impact = ImpactReport(
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
        summary="one staging model and one dashboard",
    )
    incident = make_incident(
        root_cause=root.model_dump(mode="json"), impact=impact.model_dump(mode="json")
    )
    attempt = services.FixAttemptView(attempt_no=1, diff="diff --git a/x b/x")

    detail = services.build_incident_detail(incident, attempts=[attempt])

    assert detail.root_cause is not None
    assert detail.root_cause.category is RootCauseCategory.SOURCE_COLUMN_RENAMED
    assert detail.impact is not None
    assert detail.impact.affected_exposures[0].owner == "analytics"
    assert detail.latest_diff == attempt.diff
    assert detail.warnings == []


def test_build_incident_detail_warns_on_stale_payloads() -> None:
    incident = make_incident(
        root_cause={"category": "NOT_A_CATEGORY"}, impact={"scope": "not-a-scope"}
    )
    detail = services.build_incident_detail(incident)
    assert detail.root_cause is None
    assert detail.impact is None
    assert len(detail.warnings) == 2


# --- benchmark view -------------------------------------------------------------------


def test_benchmark_notes_flag_pilot_partial_and_dirty() -> None:
    metadata = make_metadata(
        catalogue_size=4,
        fault_count=3,
        control_count=1,
        repeats=2,
        stop_reason="daily cap reached",
        git_dirty=True,
    )
    notes = services.benchmark_notes(metadata, scenario_count=5)
    text = " ".join(notes)
    assert "Pilot run" in text
    assert "Incomplete" in text
    assert "Stopped early" in text
    assert "dirty" in text


def test_benchmark_notes_are_silent_for_a_full_run() -> None:
    metadata = make_metadata()
    assert services.benchmark_notes(metadata, scenario_count=46) == []


def test_benchmark_notes_flag_holdout() -> None:
    notes = services.benchmark_notes(make_metadata(split="holdout"), scenario_count=46)
    assert any("Holdout" in note for note in notes)


def test_build_benchmark_view_reuses_the_report_and_counts_infra_errors() -> None:
    metadata = make_metadata()
    record = ScenarioRun(
        scenario_id="clean-01",
        split="dev",
        kind="clean",
        clean=True,
        infra_error="provider rate limited",
    )
    view = services.build_benchmark_view(
        "run.json", BenchRunFile(metadata=metadata, scenarios=[record])
    )

    assert view.name == "run.json"
    assert view.infra_errors == 1
    assert any("infrastructure" in note.lower() for note in view.notes)
    assert view.scenario_count == 1
    assert view.fault_kinds == ["clean"]
    assert "## Overall" in view.report_markdown
    assert "## By fault kind" in view.report_markdown
    assert {row.label for row in view.metadata} >= {
        "Code fingerprint",
        "Harness version",
        "Prompt hashes",
    }


# --- benchmark files ------------------------------------------------------------------


def test_list_benchmark_files_is_newest_first(tmp_path: Path) -> None:
    older = tmp_path / "a.json"
    newer = tmp_path / "b.json"
    older.write_text("{}", encoding="utf-8")
    newer.write_text("{}", encoding="utf-8")
    (tmp_path / "latest.md").write_text("not a result", encoding="utf-8")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))

    files = services.list_benchmark_files(tmp_path)
    assert [item.name for item in files] == ["b.json", "a.json"]


@pytest.mark.parametrize("name", ["../a.json", "nested/a.json", "a.txt", "a.json.bak"])
def test_load_benchmark_file_rejects_paths_outside_the_directory(tmp_path: Path, name: str) -> None:
    with pytest.raises(DqsError):
        services.load_benchmark_file(name, tmp_path)


def test_load_benchmark_file_rejects_bad_documents(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    (tmp_path / "empty.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DqsError):
        services.load_benchmark_file("broken.json", tmp_path)
    with pytest.raises(DqsError):
        services.load_benchmark_file("empty.json", tmp_path)


# --- contracts ------------------------------------------------------------------------


def test_contract_counts() -> None:
    def view(table: str, status: ContractStatus) -> services.ContractView:
        return services.ContractView(
            table=table,
            layer=Layer.RAW.value,
            status=status.value,
            generated_at=NOW,
            yaml="",
        )

    contracts = [
        view("raw.a", ContractStatus.APPROVED),
        view("raw.b", ContractStatus.APPROVED),
        view("raw.c", ContractStatus.DRAFT),
    ]
    assert services.contract_counts(contracts) == (2, 1)


# --- approval write path ---------------------------------------------------------------


def test_decide_incident_gates_status_and_rejection_note() -> None:
    settings = Settings(_env_file=None, github_mode="dry_run")
    calls: list[dict[str, Any]] = []

    def fake_resume(*_args: Any, **_kwargs: Any) -> WorkflowOutcome:
        calls.append(_kwargs)
        raise AssertionError("resume must not be called when the gate fails")

    def loader(_engine: Engine, _incident_id: uuid.UUID) -> Incident:
        return make_incident(status=IncidentStatus.OPEN)

    engine = cast(Engine, object())
    with pytest.raises(WorkflowError):
        services.decide_incident(
            settings,
            INCIDENT_ID,
            decision="approve",
            approver="alice",
            engine=engine,
            resume=fake_resume,
            get_incident=loader,
        )
    assert calls == []

    with pytest.raises(WorkflowError):
        services.decide_incident(
            settings,
            INCIDENT_ID,
            decision="reject",
            approver="alice",
            note="",
            engine=engine,
            resume=fake_resume,
            get_incident=loader,
        )
    assert calls == []


def test_decide_incident_resumes_an_awaiting_incident() -> None:
    settings = Settings(_env_file=None, github_mode="dry_run")
    captured: dict[str, Any] = {}

    def loader(_engine: Engine, _incident_id: uuid.UUID) -> Incident:
        return make_incident(status=IncidentStatus.AWAITING_APPROVAL)

    def fake_resume(
        _settings: Settings,
        _engine: Engine,
        incident_id: uuid.UUID,
        *,
        decision: str,
        approver: str,
        note: str,
    ) -> WorkflowOutcome:
        captured.update(incident_id=incident_id, decision=decision, approver=approver, note=note)
        return WorkflowOutcome(
            incident_id=incident_id,
            status=IncidentStatus.RESOLVED,
            completed=True,
            checkpoint_path="data/checkpoints.sqlite",
        )

    result = services.decide_incident(
        settings,
        INCIDENT_ID,
        decision="approve",
        approver=" alice ",
        note="looks good",
        engine=cast(Engine, object()),
        resume=fake_resume,
        get_incident=loader,
    )

    assert captured == {
        "incident_id": INCIDENT_ID,
        "decision": "approve",
        "approver": "alice",
        "note": "looks good",
    }
    assert result.status == "resolved"
    assert result.completed is True
    assert "resolved" in result.message


def test_describe_outcome_covers_failure_and_interrupt() -> None:
    failed = WorkflowOutcome(
        incident_id=INCIDENT_ID,
        status=IncidentStatus.FAILED,
        error="budget exceeded",
        checkpoint_path="x",
    )
    assert "budget exceeded" in services.describe_outcome(failed)

    interrupted = WorkflowOutcome(
        incident_id=INCIDENT_ID,
        status=IncidentStatus.AWAITING_APPROVAL,
        interrupted=True,
        checkpoint_path="x",
    )
    assert "paused again" in services.describe_outcome(interrupted)
