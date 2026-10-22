"""Typed state for the incident LangGraph workflow (ARCHITECTURE §10.1).

Why a TypedDict and not a bag of dicts: the checkpoint is durable JSON that
must round-trip through SQLite, while every value crossing a node boundary is
already a validated Pydantic model. TypedDict gives LangGraph a channel schema
without inventing a second serialization layer.

The state deliberately contains **no** engine, client, publisher or filesystem
handle. Those are injected into the nodes at compile time, so a checkpoint can
never leak a connection (or a secret) into `data/checkpoints.sqlite`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from dqs.agents.fixer import FixAttempt, PlanAttempt
from dqs.agents.issue_builder import IssueDraft
from dqs.models.enums import IncidentStatus, ResolutionType
from dqs.models.fix import FixPlan, VerificationResult
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause
from dqs.publish.base import ApprovalDecision, PublicationResult


class WorkflowBudget(BaseModel):
    """A serializable snapshot of the one budget shared by every node.

    `started_at` is wall-clock UTC for the report; the enforcement clock is the
    tracker's monotonic clock, because wall time can jump and must never make a
    run look shorter than it was.
    """

    model_config = ConfigDict(extra="forbid")

    tool_calls_used: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    started_at: dt.datetime
    elapsed_seconds: float = Field(default=0.0, ge=0)
    max_tool_calls: int = Field(default=0, ge=0)
    max_tokens: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=0, ge=0)


class WorkflowState(TypedDict, total=False):
    """Every channel the graph reads or writes (§10.1).

    `attempts` is the documented list of verification results; `fix_attempts`
    keeps the paired plan for the issue template. `pending_plan` is the one
    message passed from `plan_fix` to `verify_fix`, which is what lets the
    checkpointer stop between planning and a long sandbox build.
    """

    incident_id: uuid.UUID
    incident: Incident
    root_cause: RootCause | None
    impact: ImpactReport | None
    resolution_type: ResolutionType | None
    fix_plan: FixPlan | None
    attempts: list[VerificationResult]
    fix_attempts: list[FixAttempt]
    pending_plan: PlanAttempt | None
    attempt_no: int
    approval: ApprovalDecision | None
    publication: PublicationResult | None
    issue: IssueDraft | None
    candidate_models: list[str]
    budget: WorkflowBudget
    errors: list[str]
    auto_approve: bool
    verification_passed: bool
    budget_exceeded: bool
    completed: bool


class WorkflowOutcome(BaseModel):
    """What a CLI command or a test needs after invoke/resume returns."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    status: IncidentStatus
    interrupted: bool = False
    completed: bool = False
    resolution_type: ResolutionType | None = None
    publication: PublicationResult | None = None
    approval_request: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    checkpoint_path: str
    state: dict[str, Any] = Field(default_factory=dict)


__all__ = ["WorkflowBudget", "WorkflowOutcome", "WorkflowState"]
