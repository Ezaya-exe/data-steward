"""Incidents: group violations, fingerprint, de-duplicate, persist (§4.3, §7.3).

Why here: detection produces a flat list of violations, but the unit of work an
investigator (Phase 4) and the UI act on is the **incident**. Two rules from the
design drive this module:

- **Grouping** — violations from one batch+table become one incident whose
  severity is the maximum of its violations (§7.3); the fingerprint is taken from
  a deterministic representative violation, so the same fault on consecutive
  days maps to the same incident.
- **De-dup** — fingerprint = `sha1(table | check_type | column)` (§4.3). If an
  *open* incident already carries that fingerprint, the new batch is appended to
  it (its batch list grows) instead of a second incident being created.

Persistence is the only side effect and is kept to two small helpers so the
grouping logic stays pure and unit-testable without Postgres.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Engine, Row, text

from dqs.models.enums import IncidentStatus, Severity
from dqs.models.incident import Incident
from dqs.models.violation import Violation

# An incident in the "open family" is one that detection may still update; the
# terminal states are left alone (a resolved fault that recurs starts fresh).
OPEN_STATUSES: tuple[IncidentStatus, ...] = (
    IncidentStatus.OPEN,
    IncidentStatus.INVESTIGATING,
    IncidentStatus.AWAITING_APPROVAL,
)
# One column list for every incident read; Phase 3 adds the three jsonb
# lifecycle blobs (root_cause/impact/resolution) so a stored impact report
# survives `get_incident` (§4.3 writes them, readers must carry them).
_INCIDENT_COLUMNS: Final = (
    "incident_id, fingerprint, batch_id, table_name, status, severity, "
    "violations, root_cause, impact, resolution, created_at, updated_at"
)

_SEVERITY_RANK: dict[Severity, int] = {Severity.WARNING: 0, Severity.BREAKING: 1}


def fingerprint(table: str | None, check: str, column: str | None) -> str:
    """`sha1(table | check_type | column)` — the identity of a fault (§4.3)."""
    key = f"{table or ''}|{check}|{column or ''}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _representative(violations: list[Violation]) -> Violation:
    """The violation that names the incident: max severity, then a stable sort.

    Deterministic even when several checks fire together (e.g. F1's missing +
    unexpected column), so the incident's fingerprint is stable across days.
    """
    return sorted(
        violations,
        key=lambda v: (
            -_SEVERITY_RANK[v.severity],
            v.check,
            v.column or "",
            v.message,
        ),
    )[0]


@dataclass(frozen=True)
class IncidentDraft:
    """One batch+table group, ready to be inserted or merged (pure)."""

    fingerprint: str
    table: str | None
    batch_id: str | None
    severity: Severity
    violations: list[Violation]


@dataclass(frozen=True)
class PersistResult:
    """Which incidents were created and which were updated (for the CLI)."""

    created: list[uuid.UUID]
    updated: list[uuid.UUID]


def build_drafts(violations: list[Violation]) -> list[IncidentDraft]:
    """Group violations by (batch_id, table) into deterministic incident drafts."""
    groups: dict[tuple[str | None, str | None], list[Violation]] = {}
    for violation in violations:
        groups.setdefault((violation.batch_id, violation.table), []).append(violation)
    drafts: list[IncidentDraft] = []
    for (batch_id, table), group in groups.items():
        representative = _representative(group)
        drafts.append(
            IncidentDraft(
                fingerprint=fingerprint(
                    representative.table, representative.check, representative.column
                ),
                table=table,
                batch_id=batch_id,
                severity=representative.severity,
                violations=_sorted_violations(group),
            )
        )
    drafts.sort(key=lambda d: (d.batch_id or "", d.table or "", d.fingerprint))
    return drafts


def _sorted_violations(violations: list[Violation]) -> list[Violation]:
    return sorted(
        violations,
        key=lambda v: (
            -_SEVERITY_RANK[v.severity],
            v.check,
            v.column or "",
            v.message,
        ),
    )


def _merge(incident: Incident, draft: IncidentDraft) -> list[Violation]:
    """Existing violations plus the new batch's, de-duplicated by identity."""
    seen = {(v.batch_id, v.check, v.column, v.message) for v in incident.violations}
    merged = list(incident.violations)
    for violation in draft.violations:
        key = (violation.batch_id, violation.check, violation.column, violation.message)
        if key not in seen:
            seen.add(key)
            merged.append(violation)
    return _sorted_violations(merged)


# --- persistence ---------------------------------------------------------------------


def persist_incidents(engine: Engine, drafts: list[IncidentDraft]) -> PersistResult:
    """Create/update incidents for `drafts`, de-duplicating against open ones."""
    created: list[uuid.UUID] = []
    updated: list[uuid.UUID] = []
    open_by_fingerprint = {incident.fingerprint: incident for incident in _open_incidents(engine)}
    for draft in drafts:
        existing = open_by_fingerprint.get(draft.fingerprint)
        if existing is None:
            incident = _insert(engine, draft)
            created.append(incident.incident_id)
        else:
            incident = _update(engine, existing, draft)
            updated.append(incident.incident_id)
        open_by_fingerprint[draft.fingerprint] = incident
    return PersistResult(created=created, updated=updated)


def _insert(engine: Engine, draft: IncidentDraft) -> Incident:
    incident_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    payload = json.dumps([v.model_dump(mode="json") for v in draft.violations])
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO dq.incidents (incident_id, fingerprint, batch_id, table_name, "
                "status, severity, violations, created_at, updated_at) VALUES "
                "(:id, :fp, :b, :t, :status, :sev, CAST(:v AS jsonb), :now, :now)"
            ),
            {
                "id": incident_id,
                "fp": draft.fingerprint,
                "b": draft.batch_id,
                "t": draft.table,
                "status": IncidentStatus.OPEN.value,
                "sev": draft.severity.value,
                "v": payload,
                "now": now,
            },
        )
    return Incident(
        incident_id=incident_id,
        fingerprint=draft.fingerprint,
        batch_id=draft.batch_id,
        table=draft.table,
        status=IncidentStatus.OPEN,
        severity=draft.severity,
        violations=draft.violations,
        created_at=now,
        updated_at=now,
    )


def _update(engine: Engine, existing: Incident, draft: IncidentDraft) -> Incident:
    violations = _merge(existing, draft)
    severity = (
        Severity.BREAKING
        if _SEVERITY_RANK[draft.severity] > _SEVERITY_RANK[existing.severity]
        else existing.severity
    )
    batch_id = draft.batch_id or existing.batch_id
    now = dt.datetime.now(dt.UTC)
    payload = json.dumps([v.model_dump(mode="json") for v in violations])
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dq.incidents SET violations = CAST(:v AS jsonb), batch_id = :b, "
                "table_name = :t, severity = :sev, updated_at = :now WHERE incident_id = :id"
            ),
            {
                "v": payload,
                "b": batch_id,
                "t": existing.table or draft.table,
                "sev": severity.value,
                "now": now,
                "id": existing.incident_id,
            },
        )
    return existing.model_copy(
        update={
            "violations": violations,
            "severity": severity,
            "batch_id": batch_id,
            "table": existing.table or draft.table,
            "updated_at": now,
        }
    )


def _open_incidents(engine: Engine) -> list[Incident]:
    statuses = [status.value for status in OPEN_STATUSES]
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_INCIDENT_COLUMNS} FROM dq.incidents WHERE status = ANY(:statuses)"),
            {"statuses": statuses},
        ).fetchall()
    return [_row_to_incident(row) for row in rows]


def list_incidents(engine: Engine, status: str | None = None, limit: int = 200) -> list[Incident]:
    """All incidents, newest-updated first (optionally filtered by status)."""
    sql = f"SELECT {_INCIDENT_COLUMNS} FROM dq.incidents"
    params: dict[str, object] = {"limit": limit}
    if status is not None:
        sql += " WHERE status = :status"
        params["status"] = status
    sql += " ORDER BY updated_at DESC, incident_id LIMIT :limit"
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_incident(row) for row in rows]


def get_incident(engine: Engine, incident_id: uuid.UUID) -> Incident | None:
    """One incident by id, or None when it does not exist."""
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {_INCIDENT_COLUMNS} FROM dq.incidents WHERE incident_id = :id"),  # noqa: S608 — constant column list
            {"id": incident_id},
        ).fetchone()
    return None if row is None else _row_to_incident(row)


def save_impact(engine: Engine, incident_id: uuid.UUID, impact: dict[str, Any]) -> None:
    """Persist an ImpactReport (already serialized) into `dq.incidents.impact`.

    The §4.3 column exists for exactly this; storing the report makes the
    assessment survive long after the manifest/run record changed underneath.
    """
    now = dt.datetime.now(dt.UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dq.incidents SET impact = CAST(:i AS jsonb), updated_at = :now "
                "WHERE incident_id = :id"
            ),
            {"i": json.dumps(impact), "now": now, "id": incident_id},
        )


def save_root_cause(engine: Engine, incident_id: uuid.UUID, root_cause: dict[str, Any]) -> None:
    """Persist a RootCause (already serialized) into `dq.incidents.root_cause`.

    Only the investigator's structured output is stored; the incident status is
    left alone because lifecycle transitions belong to the workflow (Phase 6).
    """
    now = dt.datetime.now(dt.UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dq.incidents SET root_cause = CAST(:r AS jsonb), updated_at = :now "
                "WHERE incident_id = :id"
            ),
            {"r": json.dumps(root_cause), "now": now, "id": incident_id},
        )


def save_resolution(engine: Engine, incident_id: uuid.UUID, resolution: dict[str, Any]) -> None:
    """Persist the routing decision and its outcome into `dq.incidents.resolution`.

    The column is the workflow's audit trail: which resolution type was chosen,
    why, and (after publishing) the authentication/URL of the artifact. The
    status transition timestamp is `updated_at`.
    """
    now = dt.datetime.now(dt.UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE dq.incidents SET resolution = "
                "COALESCE(resolution, '{}'::jsonb) || CAST(:r AS jsonb), updated_at = :now "
                "WHERE incident_id = :id"
            ),
            {"r": json.dumps(resolution), "now": now, "id": incident_id},
        )


def set_status(
    engine: Engine,
    incident_id: uuid.UUID,
    status: IncidentStatus,
    *,
    reason: str | None = None,
) -> None:
    """Move one incident to `status` and stamp `updated_at`.

    The workflow is the only caller: every node that changes lifecycle state
    goes through here so the status and its timestamp cannot drift apart. When
    a reason is supplied it is appended to the resolution JSONB under
    `status_reason` (serialized as a small object, never free-form SQL).
    """
    now = dt.datetime.now(dt.UTC)
    with engine.begin() as conn:
        if reason:
            conn.execute(
                text(
                    "UPDATE dq.incidents SET status = :s, "
                    "resolution = jsonb_set(COALESCE(resolution, '{}'::jsonb), "
                    "'{status_reason}', to_jsonb(CAST(:r AS text)), true), "
                    "updated_at = :now WHERE incident_id = :id"
                ),
                {"s": status.value, "r": reason, "now": now, "id": incident_id},
            )
        else:
            conn.execute(
                text(
                    "UPDATE dq.incidents SET status = :s, updated_at = :now WHERE incident_id = :id"
                ),
                {"s": status.value, "now": now, "id": incident_id},
            )


def _row_to_incident(row: Row[Any]) -> Incident:
    (
        incident_id,
        fp,
        batch_id,
        table,
        status,
        severity,
        violations,
        root_cause,
        impact,
        resolution,
        created_at,
        updated_at,
    ) = tuple(row)
    return Incident(
        incident_id=incident_id,
        fingerprint=fp,
        batch_id=batch_id,
        table=table,
        status=IncidentStatus(status),
        severity=Severity(severity),
        violations=[Violation.model_validate(v) for v in violations],
        root_cause=root_cause,
        impact=impact,
        resolution=resolution,
        created_at=created_at,
        updated_at=updated_at,
    )
