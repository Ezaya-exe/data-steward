"""Unit tests for the publisher approval gate and idempotency (Phase 6a).

These are the non-negotiable safety tests: no approval -> no publication,
failed verification -> refusal, and an existing publication is returned without
touching the backend again.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError
from tests.helpers.publish import (
    INCIDENT_ID,
    make_approval,
    make_incident,
    make_plan,
    make_verification,
)

from dqs.errors import PublicationError
from dqs.publish.base import ApprovalDecision, PublicationKind
from dqs.publish.dry_run import DryRunPublisher
from dqs.publish.store import MemoryPublicationStore


def _publisher(tmp_path) -> tuple[DryRunPublisher, MemoryPublicationStore]:
    store = MemoryPublicationStore()
    return (
        DryRunPublisher(store, output_dir=tmp_path, issue_output_dir=tmp_path),
        store,
    )


def test_create_pr_without_an_approval_argument_raises_type_error(tmp_path) -> None:
    publisher, _ = _publisher(tmp_path)

    with pytest.raises(TypeError):
        publisher.create_pr(  # type: ignore[call-arg]
            make_plan(), make_incident(), make_verification()
        )


def test_create_pr_with_a_plain_dict_is_refused(tmp_path) -> None:
    publisher, _ = _publisher(tmp_path)

    with pytest.raises(PublicationError, match="ApprovalDecision"):
        publisher.create_pr(
            make_plan(),
            make_incident(),
            make_verification(),
            {"approver": "x", "timestamp": "2026-02-01T12:00:00Z", "decision": "approve"},  # type: ignore[arg-type]
        )


def test_approval_decision_requires_approve_and_a_timezone(tmp_path) -> None:
    with pytest.raises(ValidationError, match="note"):
        make_approval("reject")
    with pytest.raises(ValidationError, match="timezone-aware"):
        ApprovalDecision(
            approver="x",
            timestamp=dt.datetime(2026, 2, 1, 12, 0),
            decision="approve",
        )


def test_a_rejection_is_typed_but_the_publisher_refuses_it(tmp_path) -> None:
    rejection = ApprovalDecision(
        approver="shravan",
        timestamp=dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.UTC),
        decision="reject",
        note="not this batch",
    )
    publisher, store = _publisher(tmp_path)

    with pytest.raises(PublicationError, match="refused"):
        publisher.create_pr(make_plan(), make_incident(), make_verification(), rejection)

    assert store.rows == []
    assert not list(tmp_path.iterdir())


def test_auto_approval_can_only_approve() -> None:
    with pytest.raises(ValidationError, match="auto approval"):
        ApprovalDecision(
            approver="auto",
            timestamp=dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.UTC),
            decision="reject",
            note="why",
            auto=True,
        )


def test_create_pr_refuses_a_failed_verification(tmp_path) -> None:
    publisher, store = _publisher(tmp_path)

    with pytest.raises(PublicationError, match="did not pass"):
        publisher.create_pr(
            make_plan(), make_incident(), make_verification(passed=False), make_approval()
        )

    assert store.rows == []
    assert not list(tmp_path.iterdir())


def test_create_pr_writes_once_and_returns_the_stored_row(tmp_path) -> None:
    publisher, store = _publisher(tmp_path)
    plan, incident, verification = make_plan(), make_incident(), make_verification()

    first = publisher.create_pr(plan, incident, verification, make_approval())
    md = tmp_path / f"{INCIDENT_ID.hex[:8]}.md"
    patch = tmp_path / f"{INCIDENT_ID.hex[:8]}.patch"
    assert first.mode.value == "dry_run"
    assert first.kind is PublicationKind.PR
    assert not first.reused
    assert md.is_file() and patch.is_file()
    assert first.url.startswith("file:///")
    assert len(store.rows) == 1

    before = md.read_bytes()
    second = publisher.create_pr(plan, incident, verification, make_approval())

    assert second.reused
    assert second.url == first.url
    assert len(store.rows) == 1
    assert md.read_bytes() == before


def test_a_pending_row_is_completed_by_the_next_dry_run_attempt(tmp_path) -> None:
    """Crash window: begin() wrote the marker but the process died before done.

    The retry must render the artifact again, complete the same row and never
    create a second publication key.
    """
    publisher, store = _publisher(tmp_path)
    store.begin(INCIDENT_ID, PublicationKind.PR, publisher.mode)

    result = publisher.create_pr(make_plan(), make_incident(), make_verification(), make_approval())

    assert not result.reused
    assert len(store.rows) == 1
    assert not store.has_pending(INCIDENT_ID, PublicationKind.PR)
    assert (tmp_path / f"{INCIDENT_ID.hex[:8]}.md").is_file()


def test_create_pr_refuses_a_plan_from_another_incident(tmp_path) -> None:
    publisher, store = _publisher(tmp_path)
    plan = make_plan().model_copy(update={"incident_id": uuid.uuid4()})

    with pytest.raises(PublicationError, match="belongs to incident"):
        publisher.create_pr(plan, make_incident(), make_verification(), make_approval())

    assert store.rows == []


def test_create_issue_requires_approval_and_is_idempotent(tmp_path) -> None:
    publisher, store = _publisher(tmp_path)
    incident = make_incident()
    body = "# [data-quality] an issue\n\nhuman action required\n"

    with pytest.raises(PublicationError, match="ApprovalDecision"):
        publisher.create_issue(body, incident, "approve")  # type: ignore[arg-type]

    first = publisher.create_issue(body, incident, make_approval())
    second = publisher.create_issue(body, incident, make_approval())

    assert first.kind is PublicationKind.ISSUE
    assert first.url.startswith("file:///")
    assert second.reused
    assert len(store.rows) == 1
    assert (tmp_path / f"{incident.incident_id.hex[:8]}.md").is_file()


def test_create_issue_refuses_an_empty_body(tmp_path) -> None:
    publisher, store = _publisher(tmp_path)

    with pytest.raises(PublicationError, match="empty issue"):
        publisher.create_issue("   ", make_incident(), make_approval())

    assert store.rows == []
