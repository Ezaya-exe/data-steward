"""Durable incident artifacts for the workflow (status, root cause, impact).

Why a protocol: the graph must persist lifecycle changes, but unit tests should
not need Postgres. `DbJournal` is the production implementation over
`dq.incidents`; `MemoryJournal` is a real in-memory implementation with the same
contract, so the graph code has no test-only branches.

Status timestamps live in `dq.incidents.updated_at`. The resolution JSONB is
also used for a short `status_reason`, which is how a rejection note or a
failure reason survives without a new table.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Protocol

from sqlalchemy import Engine

from dqs import incidents as incidents_mod
from dqs.models.enums import IncidentStatus
from dqs.models.incident import Incident


class WorkflowJournal(Protocol):
    """The persistence surface the workflow nodes are allowed to use."""

    def get_incident(self, incident_id: uuid.UUID) -> Incident | None: ...

    def set_status(
        self, incident_id: uuid.UUID, status: IncidentStatus, reason: str | None = None
    ) -> None: ...

    def save_root_cause(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None: ...

    def save_impact(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None: ...

    def save_resolution(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None: ...


class DbJournal:
    """`dq.incidents` plus the Phase-3/4 artifact columns."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get_incident(self, incident_id: uuid.UUID) -> Incident | None:
        return incidents_mod.get_incident(self.engine, incident_id)

    def set_status(
        self, incident_id: uuid.UUID, status: IncidentStatus, reason: str | None = None
    ) -> None:
        incidents_mod.set_status(self.engine, incident_id, status, reason=reason)

    def save_root_cause(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None:
        incidents_mod.save_root_cause(self.engine, incident_id, payload)

    def save_impact(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None:
        incidents_mod.save_impact(self.engine, incident_id, payload)

    def save_resolution(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None:
        incidents_mod.save_resolution(self.engine, incident_id, payload)


class MemoryJournal:
    """An honest in-memory double for graph tests (no network, no Docker)."""

    def __init__(self, incident: Incident) -> None:
        self.incident = incident
        self.root_cause: dict[str, Any] | None = incident.root_cause
        self.impact: dict[str, Any] | None = incident.impact
        self.resolution: dict[str, Any] | None = incident.resolution
        self.transitions: list[tuple[IncidentStatus, str | None, dt.datetime]] = []

    def get_incident(self, incident_id: uuid.UUID) -> Incident | None:
        if incident_id != self.incident.incident_id:
            return None
        return self.incident.model_copy(update={"status": self.incident.status})

    def set_status(
        self, incident_id: uuid.UUID, status: IncidentStatus, reason: str | None = None
    ) -> None:
        if incident_id != self.incident.incident_id:
            raise KeyError(incident_id)
        self.incident = self.incident.model_copy(update={"status": status})
        if reason:
            self.resolution = {**(self.resolution or {}), "status_reason": reason}
        self.transitions.append((status, reason, dt.datetime.now(dt.UTC)))

    def save_root_cause(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None:
        if incident_id != self.incident.incident_id:
            raise KeyError(incident_id)
        self.root_cause = payload

    def save_impact(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None:
        if incident_id != self.incident.incident_id:
            raise KeyError(incident_id)
        self.impact = payload

    def save_resolution(self, incident_id: uuid.UUID, payload: dict[str, Any]) -> None:
        if incident_id != self.incident.incident_id:
            raise KeyError(incident_id)
        # The resolution JSONB is an audit trail: later nodes add approval and
        # publication without dropping the routing reason built earlier.
        self.resolution = {**(self.resolution or {}), **payload}


__all__ = ["DbJournal", "MemoryJournal", "WorkflowJournal"]
