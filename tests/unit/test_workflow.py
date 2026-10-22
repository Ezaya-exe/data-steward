"""Unit tests for the Phase-6b incident workflow (ARCHITECTURE §10.1/§10.2).

No network, no Postgres, no dbt: a scripted chat model drives the real
investigator and fixer, a fake verifier returns deterministic verdicts, an
in-memory journal records status transitions, and a recording publisher proves
the approval gate. The real SQLite checkpointer is used so resume behaviour and
the "checkpoint survives a failure" rule are exercised for real.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

from dqs.agents.fixer import CandidateModel
from dqs.agents.llm import BudgetTracker, LLMClient
from dqs.agents.steps import MemoryStepSink, StepSink
from dqs.config import Settings
from dqs.errors import WorkflowError
from dqs.models.enums import (
    ImpactScope,
    IncidentStatus,
    ResolutionType,
    RootCauseCategory,
    Severity,
)
from dqs.models.fix import CriterionResult, VerificationResult
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.violation import Violation
from dqs.publish.base import (
    ApprovalDecision,
    PublicationKind,
    PublicationMode,
    PublicationResult,
)
from dqs.workflow.graph import draw_mermaid
from dqs.workflow.journal import MemoryJournal
from dqs.workflow.nodes import WorkflowDeps
from dqs.workflow.runner import IncidentWorkflow

PATH = "models/staging/stg_orders.sql"
OLD = "    customer_id::integer          as customer_id,"
NEW = "    coalesce(customer_id::integer, cust_id::integer) as customer_id,"


# --- fixtures -------------------------------------------------------------------------


def _incident() -> Incident:
    now = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    violation = Violation(
        check="schema.missing_column",
        severity=Severity.BREAKING,
        table="raw.orders",
        column="customer_id",
        batch_id="batch_20260131",
        message="contract column customer_id is missing",
    )
    return Incident(
        incident_id=uuid.uuid4(),
        fingerprint="fp-workflow",
        batch_id="batch_20260131",
        table="raw.orders",
        status=IncidentStatus.OPEN,
        severity=Severity.BREAKING,
        violations=[violation],
        created_at=now,
        updated_at=now,
    )


def _root_cause(
    category: RootCauseCategory = RootCauseCategory.SOURCE_COLUMN_RENAMED,
    confidence: float = 0.92,
) -> dict[str, Any]:
    return {
        "category": category.value,
        "summary": "customer_id was renamed to cust_id in the source batch.",
        "affected_table": "raw.orders",
        "affected_columns": ["customer_id"],
        "evidence": [],
        "confidence": confidence,
        "suggested_resolution": "PR",
        "hypotheses_rejected": [],
    }


def _draft() -> dict[str, Any]:
    return {
        "summary": "coalesce the renamed column",
        "edits": [{"path": PATH, "old": OLD, "new": NEW}],
        "rationale": "cust_id carries the original identifier in the new batch.",
        "expected_effect": "customer_id is populated and all rows remain.",
        "risk": "a value present in both columns prefers the old column.",
    }


def _impact(incident: Incident) -> ImpactReport:
    return ImpactReport(
        incident_id=incident.incident_id,
        source_table="raw.orders",
        column="customer_id",
        scope=ImpactScope.COLUMN,
        summary="stg_orders and one dashboard are affected.",
    )


def _candidate() -> CandidateModel:
    return CandidateModel(
        name="stg_orders",
        path=PATH,
        sql=f"select\n{OLD}\nfrom {{{{ source('raw', 'orders') }} }}",
        contract_yaml="table: staging.stg_orders\nlayer: staging\n",
    )


def _verdict(passed: bool) -> VerificationResult:
    if passed:
        return VerificationResult(
            passed=True,
            criteria=[CriterionResult(name="dbt_build", passed=True)],
            dbt_exit_code=0,
        )
    return VerificationResult(
        passed=False,
        failed_criteria=["row_preservation"],
        criteria=[
            CriterionResult(
                name="row_preservation",
                passed=False,
                detail="the sandbox produced fewer rows than the baseline",
            )
        ],
        dbt_log_excerpt="row count mismatch",
        dbt_exit_code=1,
    )


class WorkflowChatModel(BaseChatModel):
    """Serves the RootCause schema, then one FixPlanDraft per planner attempt."""

    root_cause: dict[str, Any] = Field(default_factory=_root_cause)
    drafts: list[dict[str, Any]] = Field(default_factory=lambda: [_draft()])
    calls: int = 0
    _mode: str = PrivateAttr(default="loop")
    _draft_index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "dqs-scripted-workflow"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        _ = kwargs
        names = {getattr(item, "name", None) for item in tools}
        if any(
            item is not None and getattr(item, "__name__", "") == "FixPlanDraft" for item in tools
        ):
            self._mode = "fixer"
        elif any(getattr(item, "__name__", "") == "RootCause" for item in tools):
            self._mode = "root_cause"
        elif "submit_root_cause" in names:
            self._mode = "submit"
        else:
            self._mode = "loop"
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        _ = messages, stop, run_manager, kwargs
        self.calls += 1
        if self._mode == "fixer":
            if not self.drafts:
                raise AssertionError("the test scripted no fix drafts")
            # Reuse the last draft on a retry so one scripted plan can be
            # verified more than once without a second fixture.
            args = self.drafts[min(self._draft_index, len(self.drafts) - 1)]
            self._draft_index += 1
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FixPlanDraft",
                        "args": args,
                        "id": f"fix_{self.calls}",
                        "type": "tool_call",
                    }
                ],
            )
        elif self._mode in {"root_cause", "submit"}:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "RootCause",
                        "args": self.root_cause,
                        "id": f"cause_{self.calls}",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            # The investigator loop gets an answer with no tool calls, so it
            # moves to the structured final-answer channel.
            message = AIMessage(content="evidence gathered")
        message.usage_metadata = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


class RecordingPublisher:
    """A real publisher-shaped double that records exactly what was published."""

    def __init__(self) -> None:
        self.pr_calls: list[tuple[Any, Any, Any, ApprovalDecision]] = []
        self.issue_calls: list[tuple[Any, Any, ApprovalDecision]] = []

    def create_pr(self, plan, incident, verification, approval):
        self.pr_calls.append((plan, incident, verification, approval))
        return PublicationResult(
            incident_id=incident.incident_id,
            kind=PublicationKind.PR,
            mode=PublicationMode.DRY_RUN,
            url=f"out/pr_previews/{incident.incident_id.hex[:8]}.md",
        )

    def create_issue(self, issue, incident, approval):
        self.issue_calls.append((issue, incident, approval))
        return PublicationResult(
            incident_id=incident.incident_id,
            kind=PublicationKind.ISSUE,
            mode=PublicationMode.DRY_RUN,
            url=f"out/issues/{incident.incident_id.hex[:8]}.md",
        )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "max_tool_calls": 10,
        "max_tokens_per_incident": 100_000,
        "max_fix_attempts": 3,
        "incident_timeout_seconds": 900,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _deps(
    incident: Incident,
    *,
    chat: WorkflowChatModel | None = None,
    verdicts: list[bool] | None = None,
    settings: Settings | None = None,
    budget: BudgetTracker | None = None,
    publisher: RecordingPublisher | None = None,
    on_verify: Any = None,
) -> tuple[WorkflowDeps, RecordingPublisher, MemoryJournal, BudgetTracker]:
    settings = settings or _settings()
    model = chat or WorkflowChatModel()
    tracker = budget or BudgetTracker(max_tool_calls=10, max_tokens=100_000, timeout_seconds=900)
    client = LLMClient(
        model,
        provider="fake",
        model="scripted-workflow",
        budget=tracker,
        secret_values=[],
    )
    journal = MemoryJournal(incident)
    recorder = publisher or RecordingPublisher()

    def sink(node: str, _incident_id: uuid.UUID) -> StepSink:
        return MemoryStepSink(node=node, settings=settings)

    queue = list(verdicts or [True])

    def verifier(_settings: Any, _plan: Any, **_kwargs: Any) -> VerificationResult:
        if on_verify is not None:
            on_verify()
        passed = queue.pop(0) if queue else False
        return _verdict(passed)

    deps = WorkflowDeps(
        settings=settings,
        journal=journal,
        client=client,
        budget=tracker,
        publisher=recorder,  # type: ignore[arg-type] - structural test double
        sink=sink,
        impact_report=lambda item: _impact(item),
        candidates=lambda _incident, _root_cause: [_candidate()],
        verifier=verifier,
        tool_context=None,
        tools=[],
    )
    return deps, recorder, journal, tracker


def _run(
    deps: WorkflowDeps, incident: Incident, tmp_path: Path, *, auto_approve: bool = False
) -> tuple[IncidentWorkflow, Any]:
    workflow = IncidentWorkflow(deps, checkpoint_path=tmp_path / "checkpoints.sqlite")
    return workflow, workflow.run(incident.incident_id, auto_approve=auto_approve)


# --- routing --------------------------------------------------------------------------


def test_pr_path_interrupts_then_publishes_after_approval(tmp_path: Path) -> None:
    incident = _incident()
    deps, publisher, journal, _ = _deps(incident)
    workflow, outcome = _run(deps, incident, tmp_path)

    assert outcome.interrupted is True
    assert outcome.status is IncidentStatus.AWAITING_APPROVAL
    assert outcome.resolution_type is ResolutionType.PR
    assert publisher.pr_calls == []
    request = outcome.approval_request
    assert request is not None
    assert request["kind"] == "pr"
    assert "customer_id" in request["diff"]
    assert request["verification"]["passed"] is True

    resumed = workflow.resume(
        incident.incident_id, decision="approve", approver="shravan", note="reviewed"
    )
    workflow.close()

    assert resumed.interrupted is False
    assert resumed.completed is True
    assert resumed.status is IncidentStatus.RESOLVED
    assert resumed.publication is not None
    assert len(publisher.pr_calls) == 1
    assert publisher.pr_calls[0][3].approver == "shravan"
    assert publisher.pr_calls[0][3].auto is False
    statuses = [status for status, _reason, _at in journal.transitions]
    assert statuses == [
        IncidentStatus.INVESTIGATING,
        IncidentStatus.INVESTIGATING,
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.RESOLVED,
    ]


@pytest.mark.parametrize(
    ("category", "confidence"),
    [
        (RootCauseCategory.UNKNOWN, 0.0),
        (RootCauseCategory.SOURCE_COLUMN_RENAMED, 0.3),
    ],
)
def test_non_eligible_or_low_confidence_goes_to_an_issue(
    tmp_path: Path, category: RootCauseCategory, confidence: float
) -> None:
    incident = _incident()
    chat = WorkflowChatModel(root_cause=_root_cause(category, confidence))
    deps, publisher, _journal, _ = _deps(incident, chat=chat)
    workflow, outcome = _run(deps, incident, tmp_path)

    assert outcome.interrupted is True
    assert outcome.resolution_type is ResolutionType.ISSUE
    assert publisher.pr_calls == []
    assert outcome.approval_request is not None
    assert outcome.approval_request["kind"] == "issue"

    resumed = workflow.resume(incident.incident_id, decision="approve", approver="shravan")
    workflow.close()

    assert resumed.status is IncidentStatus.RESOLVED
    assert len(publisher.issue_calls) == 1
    assert publisher.pr_calls == []


def test_a_failed_verification_retries_then_publishes_the_passing_attempt(
    tmp_path: Path,
) -> None:
    incident = _incident()
    deps, publisher, _journal, _ = _deps(
        incident, verdicts=[False, True], settings=_settings(max_fix_attempts=3)
    )
    workflow, outcome = _run(deps, incident, tmp_path, auto_approve=True)
    workflow.close()

    assert outcome.interrupted is False
    assert outcome.completed is True
    assert outcome.attempts == 2
    assert outcome.resolution_type is ResolutionType.PR
    assert len(publisher.pr_calls) == 1
    approval = publisher.pr_calls[0][3]
    assert approval.auto is True
    assert approval.label == "approved (auto, demo mode)"


def test_exhausted_attempts_file_an_issue_with_every_attempt(tmp_path: Path) -> None:
    incident = _incident()
    deps, publisher, journal, _ = _deps(
        incident,
        verdicts=[False, False],
        settings=_settings(max_fix_attempts=2),
    )
    workflow, outcome = _run(deps, incident, tmp_path, auto_approve=True)
    workflow.close()

    assert outcome.resolution_type is ResolutionType.ISSUE
    assert outcome.attempts == 2
    assert len(publisher.issue_calls) == 1
    assert publisher.pr_calls == []
    issue_md = publisher.issue_calls[0][0].markdown
    assert "Attempt 1" in issue_md
    assert "Attempt 2" in issue_md
    assert journal.resolution is not None
    assert journal.resolution["type"] == "ISSUE"


def test_rejection_ends_the_run_without_publishing(tmp_path: Path) -> None:
    incident = _incident()
    deps, publisher, journal, _ = _deps(incident)
    workflow, outcome = _run(deps, incident, tmp_path)
    assert outcome.interrupted is True

    with pytest.raises(WorkflowError, match="must include a note"):
        workflow.resume(incident.incident_id, decision="reject", approver="shravan", note=" ")

    resumed = workflow.resume(
        incident.incident_id,
        decision="reject",
        approver="shravan",
        note="the source schema should be fixed upstream",
    )
    workflow.close()

    assert resumed.completed is True
    assert resumed.status is IncidentStatus.REJECTED
    assert publisher.pr_calls == []
    assert publisher.issue_calls == []
    assert journal.resolution is not None
    assert journal.resolution["approval"]["decision"] == "reject"
    assert "source schema" in journal.resolution["status_reason"]


def test_warning_only_resolution_ends_without_a_gate(tmp_path: Path) -> None:
    incident = _incident()
    chat = WorkflowChatModel(root_cause=_root_cause(RootCauseCategory.SOURCE_COLUMN_ADDED, 0.99))
    deps, publisher, journal, _ = _deps(incident, chat=chat)
    workflow, outcome = _run(deps, incident, tmp_path)
    workflow.close()

    assert outcome.interrupted is False
    assert outcome.completed is True
    assert outcome.resolution_type is ResolutionType.NONE
    assert outcome.status is IncidentStatus.RESOLVED
    assert publisher.pr_calls == []
    assert publisher.issue_calls == []
    assert journal.transitions[-1][0] is IncidentStatus.RESOLVED
    assert "warning-only" in (journal.transitions[-1][1] or "")


# --- durability, budgets and failures -------------------------------------------------


def test_interrupt_and_resume_across_a_new_process_object_publish_once(
    tmp_path: Path,
) -> None:
    incident = _incident()
    deps, first_publisher, _journal, _ = _deps(incident)
    checkpoint = tmp_path / "checkpoints.sqlite"
    first = IncidentWorkflow(deps, checkpoint_path=checkpoint)
    started = first.run(incident.incident_id)
    assert started.interrupted is True
    first.close()
    assert checkpoint.exists()

    # A new runner (new graph, new publisher, same incident + checkpoint file)
    # is the unit-level equivalent of a process restart.
    restarted_deps, second_publisher, _journal2, _ = _deps(incident)
    second = IncidentWorkflow(restarted_deps, checkpoint_path=checkpoint)
    resumed = second.resume(incident.incident_id, decision="approve", approver="shravan")
    assert resumed.completed is True
    assert len(second_publisher.pr_calls) == 1
    assert first_publisher.pr_calls == []

    # The checkpoint is complete; a third invocation refuses rather than
    # publishing a second time.
    with pytest.raises(WorkflowError, match="not waiting"):
        second.resume(incident.incident_id, decision="approve", approver="shravan")
    assert len(second_publisher.pr_calls) == 1
    second.close()


def test_budget_exhaustion_files_an_issue_with_partial_findings(tmp_path: Path) -> None:
    incident = _incident()
    budget = BudgetTracker(max_tool_calls=0, max_tokens=100_000, timeout_seconds=900)
    deps, publisher, journal, _ = _deps(incident, budget=budget)
    workflow, outcome = _run(deps, incident, tmp_path, auto_approve=True)
    workflow.close()

    assert outcome.resolution_type is ResolutionType.ISSUE
    assert len(publisher.issue_calls) == 1
    issue_md = publisher.issue_calls[0][0].markdown
    assert "Partial findings" in issue_md
    assert journal.resolution is not None
    assert journal.resolution["reason"] == "budget exhausted before a verified fix"
    assert outcome.state["budget"]["tool_calls_used"] == 0


def test_wall_clock_timeout_escalates_to_an_issue(tmp_path: Path) -> None:
    incident = _incident()
    now = [0.0]
    budget = BudgetTracker(
        max_tool_calls=10, max_tokens=100_000, timeout_seconds=10, clock=lambda: now[0]
    )

    def advance_clock() -> None:
        now[0] = 11.0

    deps, publisher, journal, _ = _deps(
        incident, budget=budget, verdicts=[True], on_verify=advance_clock
    )
    workflow, outcome = _run(deps, incident, tmp_path, auto_approve=True)
    workflow.close()

    assert outcome.resolution_type is ResolutionType.ISSUE
    assert outcome.attempts == 1
    assert len(publisher.issue_calls) == 1
    assert publisher.pr_calls == []
    assert journal.resolution is not None
    assert journal.resolution["reason"] == "budget exhausted before a verified fix"


def test_node_exception_marks_the_incident_failed_and_keeps_the_checkpoint(
    tmp_path: Path,
) -> None:
    incident = _incident()
    deps, publisher, _journal, _ = _deps(incident, verdicts=[True])

    def broken_impact(_incident: Incident) -> ImpactReport:
        raise RuntimeError("manifest disappeared mid-run")

    deps.impact_report = broken_impact
    workflow = IncidentWorkflow(deps, checkpoint_path=tmp_path / "checkpoints.sqlite")
    outcome = workflow.run(incident.incident_id)
    snapshot = workflow.graph.get_state({"configurable": {"thread_id": str(incident.incident_id)}})
    workflow.close()

    assert outcome.status is IncidentStatus.FAILED
    assert outcome.error is not None
    assert "RuntimeError" in outcome.error
    assert "manifest disappeared" in outcome.error
    assert publisher.pr_calls == []
    assert Path(outcome.checkpoint_path).exists()
    assert snapshot.values  # the investigate result survived the failure
    assert "assess_impact" in {str(node) for node in snapshot.next}


def test_compiled_graph_diagram_names_every_node() -> None:
    deps, _publisher, _journal, _ = _deps(_incident())
    mermaid = draw_mermaid(deps)
    for node in (
        "investigate",
        "assess_impact",
        "plan_resolution",
        "plan_fix",
        "verify_fix",
        "build_issue",
        "approval_gate",
        "publish",
    ):
        assert node in mermaid
