"""Persistence for `dq.publications` and the latest verified fix attempt.

Why a store abstraction: idempotency must be testable without Postgres, and the
publisher must never consult a live GitHub API just to decide whether it has
already published. `DbPublicationStore` is the production implementation;
`MemoryPublicationStore` is the unit-test double.

Phase 6b makes the lifecycle explicit: the publisher inserts a `pending` row
*before* the external call and flips it to `done` afterwards. A crash in
between leaves a durable marker, so a retry knows to look for the artifact
(the deterministic branch/issue marker) instead of creating a second one.
`sql/003_publication_status.sql` adds `status` and a unique index on
`(incident_id, kind)`; the application still treats a pending row as "not
published yet".
"""

from __future__ import annotations

import uuid
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs.errors import DatabaseError, PublicationError
from dqs.models.fix import FixPlan, VerificationResult
from dqs.publish.base import PublicationKind, PublicationMode, PublicationResult


class PublicationStore(Protocol):
    """The lifecycle the publisher needs; all four operations are idempotent."""

    def find(self, incident_id: uuid.UUID, kind: PublicationKind) -> PublicationResult | None:
        """A completed (`status='done'`) publication, if any."""
        ...

    def has_pending(self, incident_id: uuid.UUID, kind: PublicationKind) -> bool:
        """True when a previous attempt crashed before marking the row done."""
        ...

    def begin(self, incident_id: uuid.UUID, kind: PublicationKind, mode: PublicationMode) -> None:
        """Insert the pending marker before the external write."""
        ...

    def complete(self, result: PublicationResult) -> None:
        """Mark the pending marker done and store the final URL/mode."""
        ...

    def record(self, result: PublicationResult) -> None:
        """Insert/replace a finished row (backfill and test helper path)."""
        ...


class MemoryPublicationStore:
    """In-memory `dq.publications` for tests; mirrors the unique key."""

    def __init__(self) -> None:
        # Public list because existing tests inspect insertion order.
        self.rows: list[PublicationResult] = []
        self.pending: set[tuple[uuid.UUID, PublicationKind]] = set()

    def find(self, incident_id: uuid.UUID, kind: PublicationKind) -> PublicationResult | None:
        for row in reversed(self.rows):
            if row.incident_id == incident_id and row.kind is kind:
                return row
        return None

    def has_pending(self, incident_id: uuid.UUID, kind: PublicationKind) -> bool:
        return (incident_id, kind) in self.pending

    def begin(self, incident_id: uuid.UUID, kind: PublicationKind, mode: PublicationMode) -> None:
        # Mimic the unique index: once a row exists (pending or done) there is
        # never a second key for the same incident+kind.
        self.pending.add((incident_id, kind))

    def complete(self, result: PublicationResult) -> None:
        self.pending.discard((result.incident_id, result.kind))
        self._replace(result)

    def record(self, result: PublicationResult) -> None:
        self.pending.discard((result.incident_id, result.kind))
        self._replace(result)

    def _replace(self, result: PublicationResult) -> None:
        for index, row in enumerate(self.rows):
            if row.incident_id == result.incident_id and row.kind is result.kind:
                self.rows[index] = result
                return
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
                        "WHERE incident_id = :i AND kind = :k AND status = 'done' "
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

    def has_pending(self, incident_id: uuid.UUID, kind: PublicationKind) -> bool:
        try:
            with self.engine.connect() as conn:
                return (
                    conn.execute(
                        text(
                            "SELECT 1 FROM dq.publications "
                            "WHERE incident_id = :i AND kind = :k AND status = 'pending'"
                        ),
                        {"i": incident_id, "k": str(kind)},
                    ).first()
                    is not None
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not read dq.publications: {exc}") from exc

    def begin(self, incident_id: uuid.UUID, kind: PublicationKind, mode: PublicationMode) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO dq.publications (incident_id, kind, mode, url, status) "
                        "VALUES (:i, :k, :m, NULL, 'pending') "
                        "ON CONFLICT (incident_id, kind) DO NOTHING"
                    ),
                    {"i": incident_id, "k": str(kind), "m": str(mode)},
                )
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not begin the publication: {exc}") from exc

    def complete(self, result: PublicationResult) -> None:
        try:
            with self.engine.begin() as conn:
                updated = conn.execute(
                    text(
                        "UPDATE dq.publications "
                        "SET mode = :m, url = :u, status = 'done', created_at = now() "
                        "WHERE incident_id = :i AND kind = :k"
                    ),
                    {
                        "i": result.incident_id,
                        "k": str(result.kind),
                        "m": str(result.mode),
                        "u": result.url,
                    },
                ).rowcount
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not complete the publication: {exc}") from exc
        if not updated:
            # begin() is the durable marker; completing without it means the
            # caller bypassed the lifecycle.
            raise DatabaseError("no pending publication row to complete")

    def record(self, result: PublicationResult) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO dq.publications (incident_id, kind, mode, url, status) "
                        "VALUES (:i, :k, :m, :u, 'done') "
                        "ON CONFLICT (incident_id, kind) DO UPDATE "
                        "SET mode = EXCLUDED.mode, url = EXCLUDED.url, status = 'done', "
                        "created_at = now()"
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


class StoredFixAttempt(BaseModel):
    """One `dq.fix_attempts` row as read by the preview, UI and runner paths.

    `plan` is None when the attempt never produced a plan (an invalid, empty or
    truncated fixer reply). That is a recorded outcome, not a publishing error.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=False)

    attempt_no: int
    plan: FixPlan | None = None
    verification: VerificationResult


def load_latest_fix_attempt(engine: Engine, incident_id: uuid.UUID) -> StoredFixAttempt | None:
    """Read the newest `dq.fix_attempts` row for an incident.

    Returns None when no attempt exists. A row with `plan IS NULL` is a normal
    outcome, not an error: the model produced no usable plan, the attempt is
    still counted, and the caller (issue, UI, benchmark runner) must render or
    record it without pretending a plan exists. Rows that *do* carry a plan and
    still fail validation are genuine corruption and raise.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT attempt_no, plan, verification FROM dq.fix_attempts "
                    "WHERE incident_id = :i "
                    "ORDER BY attempt_no DESC, attempt_id DESC LIMIT 1"
                ),
                {"i": incident_id},
            ).first()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not read dq.fix_attempts: {exc}") from exc
    if row is None:
        return None
    attempt_no, plan_payload, verification_payload = row
    try:
        verification = VerificationResult.model_validate(verification_payload)
    except ValidationError as exc:
        raise PublicationError(f"the latest fix attempt does not validate: {exc}") from exc
    plan: FixPlan | None = None
    if plan_payload is not None:
        try:
            plan = FixPlan.model_validate(plan_payload)
        except ValidationError as exc:
            raise PublicationError(f"the latest fix attempt does not validate: {exc}") from exc
    return StoredFixAttempt(attempt_no=cast(int, attempt_no), plan=plan, verification=verification)


__all__ = [
    "DbPublicationStore",
    "MemoryPublicationStore",
    "PublicationStore",
    "StoredFixAttempt",
    "load_latest_fix_attempt",
]
