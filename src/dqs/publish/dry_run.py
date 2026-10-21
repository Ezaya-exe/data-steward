"""Dry-run publisher: local PR/issue previews (default mode, §12).

Why: the default must be safe. `GITHUB_MODE=dry_run` writes exactly what live
mode would send — the PR body and a unified patch — into `out/`, records the
publication in `dq.publications` with `mode=dry_run`, and returns a `file://`
URL. No GitHub client is constructed or called in this path.

The same `render_pr_body`/`build_file_patches` code feeds live mode, so a
reviewer is looking at the real body and the real commit content.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from dqs.agents.issue_builder import IssueDraft
from dqs.agents.llm import settings_secrets
from dqs.config import Settings
from dqs.errors import FixPlanError, PublicationError
from dqs.models.fix import FixPlan, VerificationResult
from dqs.models.incident import Incident
from dqs.publish.base import (
    PublicationKind,
    PublicationMode,
    PublicationResult,
    Publisher,
)
from dqs.publish.body import render_pr_body
from dqs.publish.diff import build_file_patches, full_patch
from dqs.publish.sanitize import sanitize_document
from dqs.publish.store import PublicationStore

REPO_ROOT = Path(__file__).resolve().parents[3]
OUTPUT_DIR = REPO_ROOT / "out" / "pr_previews"
ISSUE_OUTPUT_DIR = REPO_ROOT / "out" / "issue_previews"


class DryRunPublisher(Publisher):
    """The default publisher: files on disk, never a network call."""

    mode = PublicationMode.DRY_RUN

    def __init__(
        self,
        store: PublicationStore,
        *,
        output_dir: Path | None = None,
        issue_output_dir: Path | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(store)
        # Resolve the module-level defaults at call time, not import time, so a
        # test (or an operator pointing OUTPUT_DIR elsewhere) can redirect writes.
        self.output_dir = OUTPUT_DIR if output_dir is None else output_dir
        self.issue_output_dir = ISSUE_OUTPUT_DIR if issue_output_dir is None else issue_output_dir
        self.settings = settings

    def _create_pr(
        self, plan: FixPlan, incident: Incident, verification: VerificationResult
    ) -> PublicationResult:
        return _write_pr_preview(
            plan,
            incident,
            verification,
            output_dir=self.output_dir,
            settings=self.settings,
        )

    def _create_issue(self, issue_md: IssueDraft | str, incident: Incident) -> PublicationResult:
        return _write_issue_preview(
            issue_md,
            incident,
            output_dir=self.issue_output_dir,
            settings=self.settings,
        )


def run_dry_run(
    plan: FixPlan,
    incident: Incident,
    verification: VerificationResult,
    *,
    store: PublicationStore,
    output_dir: Path | None = None,
    settings: Settings | None = None,
) -> PublicationResult:
    """Write a PR preview and record it, with the same gate/idempotency rules.

    Used by `dqs publish preview`, which is not a publication decision and
    therefore has no approval payload. It still refuses a failed verification
    and still de-duplicates against `dq.publications`.
    """
    if not isinstance(verification, VerificationResult) or not verification.passed:
        raise PublicationError("refusing to preview: the fix attempt did not pass verification")
    if plan.incident_id is not None and plan.incident_id != incident.incident_id:
        raise PublicationError(
            f"fix plan belongs to incident {plan.incident_id}, not {incident.incident_id}"
        )
    existing = store.find(incident.incident_id, PublicationKind.PR)
    if existing is not None:
        return existing.model_copy(update={"reused": True})

    result = _write_pr_preview(
        plan,
        incident,
        verification,
        output_dir=OUTPUT_DIR if output_dir is None else output_dir,
        settings=settings,
    )
    store.record(result)
    return result


def preview_paths(incident_id: uuid.UUID, *, output_dir: Path | None = None) -> tuple[Path, Path]:
    """The deterministic `.md` and `.patch` paths for an incident preview."""
    target = OUTPUT_DIR if output_dir is None else output_dir
    short = incident_id.hex[:8]
    return target / f"{short}.md", target / f"{short}.patch"


def _write_pr_preview(
    plan: FixPlan,
    incident: Incident,
    verification: VerificationResult,
    *,
    output_dir: Path,
    settings: Settings | None,
) -> PublicationResult:
    try:
        patches = build_file_patches(plan.edits)
    except FixPlanError as exc:
        raise PublicationError(f"cannot render the fix diff: {exc}") from exc

    body = render_pr_body(plan, incident, verification, patches, settings=settings)
    md_path, patch_path = preview_paths(incident.incident_id, output_dir=output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(body, encoding="utf-8", newline="\n")
        patch_path.write_text(full_patch(patches), encoding="utf-8", newline="\n")
    except OSError as exc:
        raise PublicationError(f"could not write the dry-run preview: {exc}") from exc

    return PublicationResult(
        incident_id=incident.incident_id,
        kind=PublicationKind.PR,
        mode=PublicationMode.DRY_RUN,
        url=md_path.resolve().as_uri(),
    )


def _write_issue_preview(
    issue_md: IssueDraft | str,
    incident: Incident,
    *,
    output_dir: Path,
    settings: Settings | None,
) -> PublicationResult:
    content = issue_md.markdown if isinstance(issue_md, IssueDraft) else issue_md
    secrets = settings_secrets(settings) if settings is not None else []
    sanitized = sanitize_document(content, secrets=secrets)
    path = output_dir / f"{incident.incident_id.hex[:8]}.md"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(sanitized, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise PublicationError(f"could not write the dry-run issue preview: {exc}") from exc
    return PublicationResult(
        incident_id=incident.incident_id,
        kind=PublicationKind.ISSUE,
        mode=PublicationMode.DRY_RUN,
        url=path.resolve().as_uri(),
    )


__all__ = [
    "ISSUE_OUTPUT_DIR",
    "OUTPUT_DIR",
    "REPO_ROOT",
    "DryRunPublisher",
    "preview_paths",
    "run_dry_run",
]
