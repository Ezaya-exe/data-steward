"""Publisher interface and the human approval gate (docs/ARCHITECTURE.md §12).

Why: publishing is the only action that writes outside dqs. The interface makes
that action impossible to take without an explicit, typed `ApprovalDecision`
(`decision="approve"`, who, when) and a passing `VerificationResult`. Both
checks are runtime checks, not only type hints, because a caller can always
pass a dict from JSON.

Idempotency is part of the interface: before any subclass may call a remote
API, the base class looks up `dq.publications` for the same incident+kind and
returns the stored result. `dq.publications` has no primary key by spec
(DECISIONS 102), so uniqueness is enforced here in application code.
"""

from __future__ import annotations

import datetime as dt
import uuid
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dqs.agents.issue_builder import IssueDraft
from dqs.errors import PublicationError
from dqs.models.fix import FixPlan, VerificationResult
from dqs.models.incident import Incident

if TYPE_CHECKING:
    from dqs.publish.store import PublicationStore


class ApprovalDecision(BaseModel):
    """A human's explicit approval; the only key that unlocks publishing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approver: str = Field(min_length=1, max_length=200)
    timestamp: dt.datetime
    decision: Literal["approve"]

    @field_validator("timestamp")
    @classmethod
    def _must_be_timezone_aware(cls, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval timestamp must be timezone-aware (use UTC)")
        return value.astimezone(dt.UTC)


class PublicationKind(StrEnum):
    """The two publishable artifact kinds (§12)."""

    PR = "pr"
    ISSUE = "issue"


class PublicationMode(StrEnum):
    """`dry_run` writes local previews; `live` writes to GitHub."""

    DRY_RUN = "dry_run"
    LIVE = "live"


class PublicationResult(BaseModel):
    """One row of `dq.publications`, plus in-process idempotency metadata."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    kind: PublicationKind
    mode: PublicationMode
    url: str = Field(min_length=1)
    # True when this call returned an existing publication instead of
    # publishing again. Not persisted (the table has no such column).
    reused: bool = False


class Publisher(ABC):
    """Base publisher: gate, validate, de-duplicate, then delegate the I/O."""

    mode: PublicationMode

    def __init__(self, store: PublicationStore) -> None:
        self._store = store

    def create_pr(
        self,
        plan: FixPlan,
        incident: Incident,
        verification: VerificationResult,
        approval: ApprovalDecision,
    ) -> PublicationResult:
        """Publish (or return) the PR for one verified fix."""
        _require_approval(approval)
        if not isinstance(verification, VerificationResult) or not verification.passed:
            raise PublicationError(
                "refusing to publish a PR: verification did not pass (all criteria must be PASS)"
            )
        if plan.incident_id is not None and plan.incident_id != incident.incident_id:
            raise PublicationError(
                f"fix plan belongs to incident {plan.incident_id}, not {incident.incident_id}"
            )

        existing = self._store.find(incident.incident_id, PublicationKind.PR)
        if existing is not None:
            return existing.model_copy(update={"reused": True})

        result = self._create_pr(plan, incident, verification)
        self._store.record(result)
        return result

    def create_issue(
        self,
        issue_md: IssueDraft | str,
        incident: Incident,
        approval: ApprovalDecision,
    ) -> PublicationResult:
        """Publish (or return) the issue for one incident."""
        _require_approval(approval)
        content = issue_md.markdown if isinstance(issue_md, IssueDraft) else issue_md
        if not isinstance(content, str) or not content.strip():
            raise PublicationError("refusing to publish an empty issue body")
        if isinstance(issue_md, IssueDraft) and issue_md.incident_id != incident.incident_id:
            raise PublicationError(
                f"issue belongs to incident {issue_md.incident_id}, not {incident.incident_id}"
            )

        existing = self._store.find(incident.incident_id, PublicationKind.ISSUE)
        if existing is not None:
            return existing.model_copy(update={"reused": True})

        result = self._create_issue(issue_md, incident)
        self._store.record(result)
        return result

    @abstractmethod
    def _create_pr(
        self, plan: FixPlan, incident: Incident, verification: VerificationResult
    ) -> PublicationResult:
        """Perform the mode-specific PR write (called only after the gate)."""

    @abstractmethod
    def _create_issue(self, issue_md: IssueDraft | str, incident: Incident) -> PublicationResult:
        """Perform the mode-specific issue write (called only after the gate)."""


def _require_approval(approval: object) -> None:
    if not isinstance(approval, ApprovalDecision):
        raise PublicationError(
            "publishing requires an ApprovalDecision with decision='approve' "
            "(approver and timestamp)"
        )


__all__ = [
    "ApprovalDecision",
    "PublicationKind",
    "PublicationMode",
    "PublicationResult",
    "Publisher",
]
