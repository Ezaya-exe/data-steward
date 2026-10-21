"""Persistence for `dq.publications` and the latest verified fix attempt.

Why a store abstraction: idempotency must be testable without Postgres, and the
publisher must never consult a live GitHub API just to decide whether it has
already published. `DbPublicationStore` is the production implementation;
`MemoryPublicationStore` is the unit-test double.

`dq.publications` has no primary key or unique constraint by spec (§4.3), so
lookups take the newest matching row and writes are plain inserts. Single-writer
use is assumed (DECISIONS 102).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs.errors import DatabaseError, PublicationError
from dqs.models.fix import FixPlan, VerificationResult
from dqs.publish.base import PublicationKind, PublicationMode, PublicationResult


class PublicationStore(Protocol):
    """The two operations the publisher needs; both are idempotency-relevant."""

    def find(self, incident_id: uuid.UUID, kind: PublicationKind) -> PublicationResult | None: ...

    def record(self, result: PublicationResult) -> None: ...


class MemoryPublicationStore:
    """In-memory `dq.publications` for tests; preserves insertion order."""

    def __init__(self) -> None:
        self.rows: list[PublicationResult] = []

    def find(self, incident_id: uuid.UUID, kind: PublicationKind) -> PublicationResult | None:
        for row in reversed(self.rows):
            if row.incident_id == incident_id and row.kind is kind:
                return row
        return None

    def record(self, result: PublicationResult) -> None:
        self.rows.append(result)


class DbPublicationStore:
    """Postgres-backed store writing `dq.publications` on the r/w engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def find(self, incident_id: uuid.UUID, kind: PublicationKind) -> PublicationResult | None:
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT incident_id, kind, mode, url FROM dq.publications "
                        "WHERE incident_id = :i AND kind = :k "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                    {"i": incident_id, "k": str(kind)},
                ).first()
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not read dq.publications: {exc}") from exc
        if row is None:
            return None
        try:
            return PublicationResult(
                incident_id=row[0],
                kind=PublicationKind(row[1]),
                mode=PublicationMode(row[2]),
                url=row[3],
                reused=True,
            )
        except (ValueError, ValidationError) as exc:
            raise DatabaseError(f"stored publication row is invalid: {exc}") from exc

    def record(self, result: PublicationResult) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO dq.publications (incident_id, kind, mode, url) "
                        "VALUES (:i, :k, :m, :u)"
                    ),
                    {
                        "i": result.incident_id,
                        "k": str(result.kind),
                        "m": str(result.mode),
                        "u": result.url,
                    },
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not record the publication: {exc}") from exc


def load_latest_fix_attempt(
    engine: Engine, incident_id: uuid.UUID
) -> tuple[FixPlan, VerificationResult] | None:
    """Read the newest `dq.fix_attempts` row for an incident.

    Returns None when no attempt exists. A row that is not a valid
    plan+verification pair is an error, not something to paper over: the
    preview would otherwise show a diff that was never verified.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT plan, verification FROM dq.fix_attempts "
                    "WHERE incident_id = :i "
                    "ORDER BY attempt_no DESC, attempt_id DESC LIMIT 1"
                ),
                {"i": incident_id},
            ).first()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not read dq.fix_attempts: {exc}") from exc
    if row is None:
        return None
    plan_payload, verification_payload = row
    if plan_payload is None:
        raise PublicationError(
            "the latest fix attempt has no valid plan (it was rejected before planning)"
        )
    try:
        plan = FixPlan.model_validate(plan_payload)
        verification = VerificationResult.model_validate(verification_payload)
    except ValidationError as exc:
        raise PublicationError(f"the latest fix attempt does not validate: {exc}") from exc
    return plan, verification


__all__ = [
    "DbPublicationStore",
    "MemoryPublicationStore",
    "PublicationStore",
    "load_latest_fix_attempt",
]
