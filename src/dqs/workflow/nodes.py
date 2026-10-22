"""Nodes for the incident workflow (ARCHITECTURE §10.2).

Why this shape: every node is a small adapter over a component that already
exists and is independently tested. The node's job is to enforce lifecycle
semantics â€” persist the artifact, record the status, honour the shared budget,
and pass typed values along the graph. No node runs SQL chosen by a model, and
no node writes to GitHub except `publish`, which only runs after the gate.

`plan_fix` and `verify_fix` are separate nodes on purpose: the long dbt sandbox
build is checkpointed before it starts, so a crash during verification resumes
from the intended plan rather than re-burning model tokens.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langgraph.types import interrupt
from pydantic import ValidationError
from sqlalchemy import Engine

from dqs.agents import fixer as fixer_mod
from dqs.agents.fixer import CONTRACTS_DIR, CandidateModel, PlanAttempt
from dqs.agents.investigator import investigate
from dqs.agents.issue_builder import build_issue
from dqs.agents.llm import (
    BudgetExceeded,
    BudgetTracker,
    LLMClient,
    redact_secrets,
    settings_secrets,
)
from dqs.agents.steps import StepSink
from dqs.config import Settings
from dqs.errors import LineageError, WorkflowError
from dqs.models.enums import IncidentStatus, ResolutionType, RootCauseCategory
from dqs.models.fix import CriterionResult, VerificationResult
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause
from dqs.models.state import WorkflowBudget, WorkflowState
from dqs.publish.base import ApprovalDecision, Publisher
from dqs.publish.diff import build_file_patches, full_patch
from dqs.resolve import plan_resolution
from dqs.tools.base import ToolContext
from dqs.verify.patch import PROJECT_DIR
from dqs.verify.sandbox import verify_fix
from dqs.workflow.journal import WorkflowJournal

MAX_APPROVAL_DIFF_CHARS = 20_000


@dataclass
class WorkflowDeps:
    """Everything a node needs, injected once when the graph is compiled.

    None of these live in the checkpointed state. Tests can pass a
    `MemoryJournal`, a scripted `LLMClient` and a fake verifier while the real
    graph routing, status transitions and publication gate stay unchanged.
    """

    settings: Settings
    journal: WorkflowJournal
    client: LLMClient
    budget: BudgetTracker
    publisher: Publisher
    sink: Callable[[str, uuid.UUID], StepSink]
    impact_report: Callable[[Incident], ImpactReport]
    engine: Engine | None = None
    candidates: Callable[[Incident, RootCause], list[CandidateModel]] = field(
        default=fixer_mod.select_candidates
    )
    verifier: Callable[..., VerificationResult] = field(default=verify_fix)
    persister: fixer_mod.Persister | None = None
    tool_context: ToolContext | None = None
    tools: list[BaseTool] | None = None
    project_dir: Path = PROJECT_DIR
    contracts_dir: Path = CONTRACTS_DIR


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _budget_snapshot(deps: WorkflowDeps, started_at: dt.datetime) -> WorkflowBudget:
    snapshot = deps.budget.state()
    return WorkflowBudget(
        tool_calls_used=snapshot.tool_calls,
        tokens_used=snapshot.tokens,
        started_at=started_at,
        elapsed_seconds=snapshot.elapsed_seconds,
        max_tool_calls=snapshot.max_tool_calls,
        max_tokens=snapshot.max_tokens,
        timeout_seconds=snapshot.timeout_seconds,
    )


def _add_error(errors: list[str], message: str) -> list[str]:
    return [*errors, message]


def _record_transition(
    deps: WorkflowDeps,
    sink: StepSink,
    incident: Incident,
    status: IncidentStatus,
    reason: str,
) -> None:
    deps.journal.set_status(incident.incident_id, status, reason=reason)
    sink.record(
        kind="system",
        name="status_transition",
        input={"from": incident.status.value, "to": status.value},
        output={"reason": reason},
    )


def _root_cause(state: WorkflowState) -> RootCause:
    root_cause = state.get("root_cause")
    if root_cause is None:
        raise WorkflowError("workflow reached a step that needs a root cause, but none was stored")
    return root_cause


def _impact(state: WorkflowState) -> ImpactReport:
    impact = state.get("impact")
    if impact is None:
        raise WorkflowError(
            "workflow reached a step that needs an impact report, but none was stored"
        )
    return impact


def _resolution_payload(state: WorkflowState, resolution: ResolutionType) -> dict[str, Any]:
    root_cause = state.get("root_cause")
    return {
        "type": resolution.value,
        "category": root_cause.category.value if root_cause is not None else None,
        "confidence": root_cause.confidence if root_cause is not None else None,
        "decided_at": _now().isoformat(),
    }


def investigate_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Run the bounded investigator, persist its RootCause and partial budget."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        sink = deps.sink("investigate", incident.incident_id)
        _record_transition(
            deps, sink, incident, IncidentStatus.INVESTIGATING, "investigation started"
        )
        result = investigate(
            incident,
            client=deps.client,
            context=deps.tool_context,
            sink=sink,
            budget=deps.budget,
            tools=deps.tools,
        )
        deps.journal.save_root_cause(
            incident.incident_id, result.root_cause.model_dump(mode="json")
        )
        errors = list(state.get("errors", []))
        if result.budget_exceeded:
            errors = _add_error(
                errors,
                "investigation budget exhausted; the diagnosis is partial (UNKNOWN unless the "
                "model had already submitted a root cause)",
            )
        started_at = state.get("budget", WorkflowBudget(started_at=_now())).started_at
        return {
            "root_cause": result.root_cause,
            "errors": errors,
            "budget_exceeded": bool(state.get("budget_exceeded")) or result.budget_exceeded,
            "incident": incident.model_copy(update={"status": IncidentStatus.INVESTIGATING}),
            "budget": _budget_snapshot(deps, started_at),
        }

    return node


def assess_impact_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Compute and persist the deterministic impact report."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        sink = deps.sink("assess_impact", incident.incident_id)
        impact = deps.impact_report(incident)
        if impact.incident_id != incident.incident_id:
            raise WorkflowError("impact report belongs to a different incident")
        deps.journal.save_impact(incident.incident_id, impact.model_dump(mode="json"))
        sink.record(
            kind="system",
            name="impact_assessed",
            input={"incident_id": str(incident.incident_id)},
            output={
                "scope": impact.scope.value,
                "models": len(impact.affected_models),
                "exposures": len(impact.affected_exposures),
            },
        )
        started_at = state.get("budget", WorkflowBudget(started_at=_now())).started_at
        return {"impact": impact, "budget": _budget_snapshot(deps, started_at)}

    return node


def plan_resolution_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Apply the deterministic PR/ISSUE/NONE rule (§10.2)."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        root_cause = _root_cause(state)
        sink = deps.sink("plan_resolution", incident.incident_id)
        candidate_names: list[str] = []
        if root_cause.category is not RootCauseCategory.SOURCE_COLUMN_ADDED:
            try:
                candidates = deps.candidates(incident, root_cause)
            except (LineageError, fixer_mod.FixerError) as exc:
                # A missing manifest/candidate is not a routing crash: it is
                # exactly the "no file a fix could edit" case -> ISSUE.
                detail = redact_secrets(str(exc), secrets=settings_secrets(deps.settings))
                sink.record(
                    kind="system",
                    name="candidate_lookup_failed",
                    input={"incident_id": str(incident.incident_id)},
                    output={"error": detail},
                )
                candidates = []
            candidate_names = [candidate.name for candidate in candidates]

        resolution = plan_resolution(
            root_cause.category, root_cause.confidence, bool(candidate_names)
        )
        payload = _resolution_payload(state, resolution)
        payload["candidate_models"] = candidate_names
        deps.journal.save_resolution(incident.incident_id, payload)
        sink.record(
            kind="system",
            name="resolution_selected",
            input={
                "category": root_cause.category.value,
                "confidence": root_cause.confidence,
                "candidates": candidate_names,
            },
            output={"resolution": resolution.value},
        )
        if resolution is ResolutionType.NONE:
            _record_transition(
                deps, sink, incident, IncidentStatus.RESOLVED, "warning-only incident: no action"
            )
            return {
                "resolution_type": resolution,
                "completed": True,
                "candidate_models": candidate_names,
                "incident": incident.model_copy(update={"status": IncidentStatus.RESOLVED}),
            }
        return {"resolution_type": resolution, "candidate_models": candidate_names}

    return node


def plan_fix_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Ask for exactly one fix plan (the verifier gets its own node)."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        root_cause = _root_cause(state)
        impact = _impact(state)
        sink = deps.sink("plan_fix", incident.incident_id)
        attempt_no = int(state.get("attempt_no", 0)) + 1
        attempts = list(state.get("attempts", []))
        previous = (attempt_no - 1, attempts[-1]) if attempts else None

        try:
            candidates = deps.candidates(incident, root_cause)
        except (LineageError, fixer_mod.FixerError) as exc:
            candidates = []
            detail = redact_secrets(str(exc), secrets=settings_secrets(deps.settings))
        else:
            detail = ""

        if not candidates:
            failed = VerificationResult(
                passed=False,
                failed_criteria=["candidate_models"],
                criteria=[
                    CriterionResult(
                        name="candidate_models",
                        passed=False,
                        detail=(
                            detail
                            or "no staging model directly selects from the affected raw table"
                        )[:2000],
                    )
                ],
            )
            planned = PlanAttempt(
                attempt_no=attempt_no, failed=failed, error=failed.criteria[0].detail
            )
        else:
            planned = fixer_mod.plan_attempt(
                incident,
                root_cause,
                impact,
                client=deps.client,
                sink=sink,
                settings=deps.settings,
                attempt_no=attempt_no,
                previous=previous,
                candidates=candidates,
                engine=deps.engine,
                persister=deps.persister,
            )

        errors = list(state.get("errors", []))
        if planned.error and not planned.budget_exceeded:
            errors = _add_error(errors, planned.error)
        if planned.budget_exceeded:
            errors = _add_error(errors, planned.error or "fix planning budget exhausted")
        started_at = state.get("budget", WorkflowBudget(started_at=_now())).started_at
        return {
            "pending_plan": planned,
            "attempt_no": attempt_no,
            "errors": errors,
            "budget_exceeded": bool(state.get("budget_exceeded")) or planned.budget_exceeded,
            "budget": _budget_snapshot(deps, started_at),
        }

    return node


def verify_fix_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Run the deterministic verifier for the pending plan and checkpoint the verdict."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        planned = state.get("pending_plan")
        if planned is None:
            raise WorkflowError("verify_fix ran without a pending plan")
        sink = deps.sink("verify_fix", incident.incident_id)
        attempt = fixer_mod.verify_attempt(
            planned,
            incident=incident,
            settings=deps.settings,
            engine=deps.engine,
            verifier=deps.verifier,
            persister=deps.persister,
            sink=sink,
        )
        attempts = [*state.get("attempts", []), attempt.verification]
        fix_attempts = [*state.get("fix_attempts", []), attempt]
        errors = list(state.get("errors", []))
        budget_exceeded = bool(state.get("budget_exceeded"))
        try:
            deps.budget.check()
        except BudgetExceeded as exc:
            budget_exceeded = True
            errors = _add_error(
                errors,
                redact_secrets(str(exc), secrets=settings_secrets(deps.settings)),
            )
        started_at = state.get("budget", WorkflowBudget(started_at=_now())).started_at
        return {
            "attempts": attempts,
            "fix_attempts": fix_attempts,
            "fix_plan": attempt.plan if attempt.plan is not None else state.get("fix_plan"),
            "verification_passed": attempt.verification.passed,
            "budget_exceeded": budget_exceeded,
            "errors": errors,
            "budget": _budget_snapshot(deps, started_at),
        }

    return node


def build_issue_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Render the deterministic issue, including exhausted fix attempts."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        root_cause = _root_cause(state)
        impact = _impact(state)
        sink = deps.sink("build_issue", incident.incident_id)
        issue = build_issue(
            incident,
            root_cause,
            impact,
            resolution=ResolutionType.ISSUE,
            attempts=state.get("fix_attempts", []),
        )
        payload = _resolution_payload(state, ResolutionType.ISSUE)
        payload["issue_title"] = issue.title
        if state.get("budget_exceeded"):
            payload["reason"] = "budget exhausted before a verified fix"
        deps.journal.save_resolution(incident.incident_id, payload)
        sink.record(
            kind="system",
            name="issue_built",
            input={"incident_id": str(incident.incident_id)},
            output={"title": issue.title, "attempts": len(state.get("fix_attempts", []))},
        )
        return {"issue": issue, "resolution_type": ResolutionType.ISSUE}

    return node


def _approval_payload(state: WorkflowState, secrets: list[str]) -> dict[str, Any]:
    """Everything the human needs to approve (and nothing secret)."""
    incident = state["incident"]
    root_cause = state.get("root_cause")
    impact = state.get("impact")
    resolution = state.get("resolution_type") or ResolutionType.ISSUE
    payload: dict[str, Any] = {
        "incident_id": str(incident.incident_id),
        "table": incident.table,
        "severity": incident.severity.value,
        "kind": "pr" if resolution is ResolutionType.PR else "issue",
        "resolution": resolution.value,
        "fingerprint": incident.fingerprint,
        "root_cause": root_cause.model_dump(mode="json") if root_cause else None,
        "impact": impact.model_dump(mode="json") if impact else None,
        "attempts": len(state.get("attempts", [])),
        "verification": (
            state["attempts"][-1].model_dump(mode="json") if state.get("attempts") else None
        ),
        "prompt": "Approve or reject with a note; nothing is published before this decision.",
    }
    if resolution is ResolutionType.PR and state.get("fix_plan") is not None:
        plan = state["fix_plan"]
        if plan is not None:
            try:
                diff = full_patch(build_file_patches(plan.edits))
            except Exception as exc:  # the preview must still explain why it is absent
                diff = f"diff unavailable: {type(exc).__name__}: {exc}"
            payload["diff"] = redact_secrets(diff[:MAX_APPROVAL_DIFF_CHARS], secrets=secrets)
            payload["plan"] = plan.model_dump(mode="json")
    draft_issue = state.get("issue")
    if draft_issue is not None:
        payload["issue"] = draft_issue.model_dump(mode="json")
    return payload


def _decision_from_resume(value: Any) -> ApprovalDecision:
    """Validate `Command(resume=...)` into the typed approval model."""
    if isinstance(value, ApprovalDecision):
        return value
    if not isinstance(value, dict):
        raise WorkflowError(
            "approval resume must be a dict with decision, approver and optional note"
        )
    decision = value.get("decision")
    if decision not in {"approve", "reject"}:
        raise WorkflowError("approval decision must be 'approve' or 'reject'")
    approver = str(value.get("approver") or "").strip()
    if not approver:
        raise WorkflowError("approval needs a non-empty approver")
    note = str(value.get("note") or "").strip()
    try:
        return ApprovalDecision(
            approver=approver,
            timestamp=_now(),
            decision=decision,
            note=note,
        )
    except ValidationError as exc:
        raise WorkflowError(f"invalid approval decision: {exc}") from exc


def approval_gate_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """`interrupt` for a human decision; explicit auto-approval never defaults on."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        sink = deps.sink("approval_gate", incident.incident_id)
        resolution = state.get("resolution_type") or ResolutionType.ISSUE
        if bool(state.get("auto_approve")):
            decision = ApprovalDecision(
                approver="auto",
                timestamp=_now(),
                decision="approve",
                auto=True,
            )
            sink.record(
                kind="system",
                name="auto_approved",
                input={"incident_id": str(incident.incident_id)},
                output={"label": decision.label},
            )
        else:
            # Reached again on resume; only stamp the wait once so the timeline
            # shows a single transition into `awaiting_approval`.
            current = deps.journal.get_incident(incident.incident_id)
            if current is None or current.status is not IncidentStatus.AWAITING_APPROVAL:
                deps.journal.set_status(
                    incident.incident_id,
                    IncidentStatus.AWAITING_APPROVAL,
                    reason="waiting for a human decision",
                )
            resume_value = interrupt(_approval_payload(state, settings_secrets(deps.settings)))
            decision = _decision_from_resume(resume_value)
            sink.record(
                kind="system",
                name="approval_recorded",
                input={"incident_id": str(incident.incident_id)},
                output={"decision": decision.decision, "approver": decision.approver},
            )

        payload = _resolution_payload(state, resolution)
        payload["approval"] = decision.model_dump(mode="json")
        deps.journal.save_resolution(incident.incident_id, payload)
        if decision.decision == "reject":
            deps.journal.set_status(
                incident.incident_id,
                IncidentStatus.REJECTED,
                reason=f"rejected by {decision.approver}: {decision.note}",
            )
            return {
                "approval": decision,
                "completed": True,
                "incident": incident.model_copy(update={"status": IncidentStatus.REJECTED}),
            }
        return {
            "approval": decision,
            "completed": False,
            "incident": incident.model_copy(update={"status": IncidentStatus.AWAITING_APPROVAL}),
        }

    return node


def publish_node(deps: WorkflowDeps) -> Callable[[WorkflowState], dict[str, Any]]:
    """Apply the approved publication through the existing publisher."""

    def node(state: WorkflowState) -> dict[str, Any]:
        incident = state["incident"]
        approval = state.get("approval")
        if approval is None or approval.decision != "approve":
            # The graph cannot route here without an approval; this is a
            # defence-in-depth check before an external write.
            raise WorkflowError("publish ran without an approve decision")
        resolution = state.get("resolution_type") or ResolutionType.ISSUE
        sink = deps.sink("publish", incident.incident_id)
        if resolution is ResolutionType.PR:
            plan = state.get("fix_plan")
            attempts = state.get("attempts", [])
            if plan is None or not attempts or not attempts[-1].passed:
                raise WorkflowError("publish ran for a PR without a verified fix")
            result = deps.publisher.create_pr(plan, incident, attempts[-1], approval)
        elif resolution is ResolutionType.ISSUE:
            issue = state.get("issue")
            if issue is None:
                raise WorkflowError("publish ran for an issue that was never built")
            result = deps.publisher.create_issue(issue, incident, approval)
        else:
            raise WorkflowError(f"cannot publish resolution {resolution.value}")

        payload = _resolution_payload(state, resolution)
        payload["approval"] = approval.model_dump(mode="json")
        payload["publication"] = result.model_dump(mode="json")
        deps.journal.save_resolution(incident.incident_id, payload)
        _record_transition(deps, sink, incident, IncidentStatus.RESOLVED, "publication approved")
        sink.record(
            kind="system",
            name="published",
            input={"incident_id": str(incident.incident_id)},
            output={"kind": result.kind.value, "url": result.url, "reused": result.reused},
        )
        started_at = state.get("budget", WorkflowBudget(started_at=_now())).started_at
        return {
            "publication": result,
            "completed": True,
            "incident": incident.model_copy(update={"status": IncidentStatus.RESOLVED}),
            "budget": _budget_snapshot(deps, started_at),
        }

    return node


__all__ = [
    "WorkflowDeps",
    "approval_gate_node",
    "assess_impact_node",
    "build_issue_node",
    "investigate_node",
    "plan_fix_node",
    "plan_resolution_node",
    "publish_node",
    "verify_fix_node",
]
