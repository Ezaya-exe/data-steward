"""Unit tests for deterministic demo helpers (Phase 6b).

These run without Postgres or an LLM: they lock the default story and the
preview-path selection, which are the parts the acceptance transcript shows.
"""

from __future__ import annotations

import uuid

from dqs.config import Settings
from dqs.demo import DEFAULT_FAULT, DEMO_DB, _demo_settings, _preview_paths
from dqs.models.enums import IncidentStatus, ResolutionType
from dqs.models.state import WorkflowOutcome
from dqs.publish.base import PublicationKind, PublicationMode, PublicationResult
from dqs.publish.dry_run import ISSUE_OUTPUT_DIR, preview_paths
from dqs.sim.faults import parse_fault_specs


def test_default_fault_is_the_documented_column_rename() -> None:
    specs = parse_fault_specs([DEFAULT_FAULT])
    assert len(specs) == 1
    spec = specs[0]
    assert spec.kind.value == "column_rename"
    assert spec.table == "orders"
    assert spec.column == "customer_id"
    assert spec.params == {"new_name": "cust_id"}


def test_pr_previews_use_the_deterministic_incident_paths() -> None:
    incident_id = uuid.uuid4()
    outcome = WorkflowOutcome(
        incident_id=incident_id,
        status=IncidentStatus.RESOLVED,
        resolution_type=ResolutionType.PR,
        publication=PublicationResult(
            incident_id=incident_id,
            kind=PublicationKind.PR,
            mode=PublicationMode.DRY_RUN,
            url="file:///preview.md",
        ),
        checkpoint_path="data/checkpoints.sqlite",
    )
    assert _preview_paths(outcome) == preview_paths(incident_id)


def test_issue_previews_have_no_patch_path() -> None:
    incident_id = uuid.uuid4()
    outcome = WorkflowOutcome(
        incident_id=incident_id,
        status=IncidentStatus.RESOLVED,
        resolution_type=ResolutionType.ISSUE,
        publication=PublicationResult(
            incident_id=incident_id,
            kind=PublicationKind.ISSUE,
            mode=PublicationMode.DRY_RUN,
            url="file:///issue.md",
        ),
        checkpoint_path="data/checkpoints.sqlite",
    )
    md_path, patch_path = _preview_paths(outcome)
    assert md_path == ISSUE_OUTPUT_DIR / f"{incident_id.hex[:8]}.md"
    assert patch_path is None


def test_nothing_published_means_no_preview_paths() -> None:
    outcome = WorkflowOutcome(
        incident_id=uuid.uuid4(),
        status=IncidentStatus.REJECTED,
        checkpoint_path="data/checkpoints.sqlite",
    )
    assert _preview_paths(outcome) == (None, None)


def test_the_demo_forces_its_own_database_and_dry_run_publishing() -> None:
    live = Settings(_env_file=None, github_mode="live")
    effective = _demo_settings(live)
    assert effective.pg_database == DEMO_DB
    assert effective.github_mode == "dry_run"
    assert live.github_mode == "live", "the caller's settings must not be mutated"
