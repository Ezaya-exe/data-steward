"""Incident model — grouped violations with lifecycle metadata (§4.3, §7.3).

Why: the monitor groups the violations of one batch+table into one incident
(severity = max) and de-duplicates *across batches* by fingerprint, so the
incident — not the violation — is what an investigator (Phase 4) and the UI
operate on. The `batches` property exposes the de-dup evidence directly: the
violations carry their producing `batch_id`, so the batch list grows as the
same fault is seen on consecutive days.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from dqs.models.enums import IncidentStatus, Severity
from dqs.models.violation import Violation


class Incident(BaseModel):
    """One grouped, fingerprinted set of violations (§4.3 `dq.incidents`)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    fingerprint: str
    batch_id: str | None = None  # most recent producing batch
    table: str | None = None
    status: IncidentStatus
    severity: Severity
    violations: list[Violation]
    # Later phases fill these in; typed as JSON blobs until the models exist
    # (RootCause in Phase 4, ImpactReport in Phase 3, resolution in Phase 6).
    root_cause: dict[str, Any] | None = None
    impact: dict[str, Any] | None = None
    resolution: dict[str, Any] | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def batches(self) -> list[str]:
        """Every batch represented in `violations`, sorted and de-duplicated."""
        return sorted({v.batch_id for v in self.violations if v.batch_id is not None})
