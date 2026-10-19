"""Unit tests for incident fingerprinting and grouping (docs/ARCHITECTURE.md §4.3).

The persistence side (SQL) is exercised by the integration tests; here the pure
logic is pinned: the fingerprint identity, the (batch, table) grouping with
max-severity, the representative choice that keeps a fault's fingerprint stable
across days, and the de-dup merge that grows the incident's batch list.
"""

from __future__ import annotations

import datetime as dt
import uuid

from dqs.incidents import _merge, build_drafts, fingerprint
from dqs.models.enums import IncidentStatus, Severity
from dqs.models.incident import Incident
from dqs.models.violation import Violation


def violation(
    *,
    check: str = "schema.missing_column",
    severity: Severity = Severity.BREAKING,
    table: str | None = "raw.orders",
    column: str | None = "customer_id",
    batch_id: str | None = "batch_20260131",
    message: str = "m",
) -> Violation:
    return Violation(
        check=check,
        severity=severity,
        table=table,
        column=column,
        batch_id=batch_id,
        message=message,
    )


def test_fingerprint_is_stable_and_names_table_check_column() -> None:
    assert fingerprint("raw.orders", "schema.missing_column", "customer_id") == fingerprint(
        "raw.orders", "schema.missing_column", "customer_id"
    )
    assert fingerprint("raw.orders", "schema.missing_column", "customer_id") != fingerprint(
        "raw.orders", "schema.missing_column", "cust_id"
    )
    assert fingerprint(None, "monitor.missing_batch", None) == fingerprint(
        None, "monitor.missing_batch", None
    )


def test_build_drafts_groups_one_batch_table_and_takes_max_severity() -> None:
    violations = [
        violation(column="customer_id", severity=Severity.BREAKING),
        violation(
            check="schema.unexpected_column",
            column="cust_id",
            severity=Severity.WARNING,
        ),
    ]
    drafts = build_drafts(violations)
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.severity is Severity.BREAKING
    # representative = highest severity -> the missing-column violation
    assert draft.fingerprint == fingerprint("raw.orders", "schema.missing_column", "customer_id")


def test_same_fault_in_two_batches_yields_the_same_fingerprint() -> None:
    first = build_drafts([violation(batch_id="batch_20260131")])[0]
    second = build_drafts([violation(batch_id="batch_20260201")])[0]
    assert first.fingerprint == second.fingerprint
    assert first.batch_id != second.batch_id


def test_drafts_are_built_for_different_tables_separately() -> None:
    drafts = build_drafts(
        [
            violation(table="raw.orders", column="customer_id"),
            violation(
                table="raw.payments",
                column="method",
                check="schema.missing_column",
            ),
        ]
    )
    assert len(drafts) == 2
    assert {d.table for d in drafts} == {"raw.orders", "raw.payments"}


def test_merge_de_duplicates_a_repeat_and_appends_the_next_batch() -> None:
    existing = _incident([violation(batch_id="batch_20260131", message="missing")])
    # A re-run of the same day must not duplicate the violation...
    repeat = build_drafts([violation(batch_id="batch_20260131", message="missing")])[0]
    assert len(_merge(existing, repeat)) == 1
    # ...but the next day's batch is appended, growing the incident's batch list.
    next_day = build_drafts([violation(batch_id="batch_20260201", message="missing")])[0]
    merged = _merge(existing, next_day)
    assert {v.batch_id for v in merged} == {"batch_20260131", "batch_20260201"}
    assert len(merged) == 2


def _incident(violations: list[Violation]) -> Incident:
    now = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    return Incident(
        incident_id=uuid.uuid4(),
        fingerprint=fingerprint("raw.orders", "schema.missing_column", "customer_id"),
        batch_id="batch_20260131",
        table="raw.orders",
        status=IncidentStatus.OPEN,
        severity=Severity.BREAKING,
        violations=violations,
        created_at=now,
        updated_at=now,
    )


def test_incident_batches_property_is_sorted_and_deduplicated() -> None:
    incident = _incident(
        [
            violation(batch_id="batch_20260201"),
            violation(batch_id="batch_20260131"),
            violation(batch_id="batch_20260201"),
        ]
    )
    assert incident.batches == ["batch_20260131", "batch_20260201"]
