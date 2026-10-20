"""Deterministic issue writer for incidents that must not become a PR (§10.6).

Why this is not an agent: an issue is an artifact a human reads and acts on.
Every sentence here comes either from a stored, typed fact (incident, root
cause, impact, verification attempts) or from a fixed per-category playbook.
An LLM writing the issue could invent evidence or hide a failed fix; a template
cannot. The only free-form fields it renders are the ones earlier nodes already
produced and stored.

The builder is also the honest fallback for exhausted fix attempts: when the
fixer used every attempt, the issue must show each attempt and why verification
rejected it, not a summary that implies nothing was tried.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from dqs.agents.fixer import FixAttempt
from dqs.errors import IssueBuildError
from dqs.models.enums import ResolutionType, RootCauseCategory
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause

MAX_TEXT = 4000
MAX_EXCERPT = 1500

# Static playbook. Keys are the closed categories; a new category must add an
# entry here, and the table test fails until it does.
NEXT_STEPS: Final[dict[RootCauseCategory, tuple[str, ...]]] = {
    RootCauseCategory.SOURCE_COLUMN_RENAMED: (
        "Confirm the old -> new column mapping with the source owner before editing SQL.",
        "Update the staging model to read the new source column and alias it to the "
        "contract column name; keep every other output column unchanged.",
        "Rebuild the affected models for the full raw history and re-run the checks.",
    ),
    RootCauseCategory.SOURCE_COLUMN_REMOVED: (
        "Ask the source owner whether the column was removed on purpose or dropped by "
        "a faulty extract.",
        "Restore the source column, or agree a contract change before changing downstream "
        "models; do not silently drop the downstream column.",
        "Re-run the batch and the contract checks once the producer has responded.",
    ),
    RootCauseCategory.SOURCE_TYPE_CHANGED: (
        "Inspect sample values in the new type and identify the exact type change.",
        "Add an explicit cast in the staging model that preserves the published logical "
        "type, and handle values that cannot be converted.",
        "Rebuild the affected models and compare profiles with a clean baseline batch.",
    ),
    RootCauseCategory.UPSTREAM_NULLS_INTRODUCED: (
        "Trace the nulls to the producing system or extract and fix the cause there; a "
        "staging-model workaround would hide the defect.",
        "If the column is genuinely nullable, propose an explicit contract change rather "
        "than filtering rows or substituting values to satisfy the check.",
        "Backfill the affected batch after the producer is fixed and re-run the checks.",
    ),
    RootCauseCategory.REFERENTIAL_INTEGRITY_BROKEN: (
        "Identify which child rows reference missing parent keys and when they arrived.",
        "Repair the load order or backfill the missing parents; do not delete orphan rows "
        "to make the relationship test pass.",
        "Re-run the relationship checks against the repaired batch.",
    ),
    RootCauseCategory.DUPLICATE_RECORDS: (
        "Confirm which columns form the business key and inspect the duplicate copies.",
        "Deduplicate deterministically in the staging model, keeping one row per key, and "
        "add or keep the uniqueness test.",
        "Verify that the row count matches the expected distinct-key count before closing.",
    ),
    RootCauseCategory.UNIT_OR_SCALE_CHANGE: (
        "Identify the measure and the unit or scale change by comparing with a clean batch.",
        "Apply the unit conversion explicitly in the model that owns the measure and "
        "document the conversion.",
        "Re-run the range and mean checks against the agreed baseline.",
    ),
    RootCauseCategory.LATE_OR_MISSING_BATCH: (
        "Check the scheduler, extract logs and arrival metadata for the batch.",
        "Re-run or backfill the missing batch; add monitoring if the delay was not alerted.",
        "Confirm the freshness and volume checks pass after the backfill.",
    ),
    RootCauseCategory.SOURCE_COLUMN_ADDED: (
        "Confirm the new source column is intentional and who owns it.",
        "Decide whether to expose it downstream; if so, update the contract and staging "
        "model together.",
        "No urgent fix is required while the addition stays outside the contract.",
    ),
    RootCauseCategory.UNKNOWN: (
        "Gather more evidence: compare profiles with a clean batch and inspect the dbt run "
        "log and recent source changes.",
        "Involve the table owner or the producing team; do not guess a category to force a "
        "code change.",
        "Re-investigate when the source behaviour is understood.",
    ),
}


class IssueDraft(BaseModel):
    """The rendered issue handed to the publisher (§12) or shown in the UI."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    category: RootCauseCategory
    resolution: ResolutionType
    title: str = Field(min_length=1)
    markdown: str = Field(min_length=1)


def build_issue(
    incident: Incident,
    root_cause: RootCause,
    impact: ImpactReport,
    *,
    resolution: ResolutionType,
    attempts: Sequence[FixAttempt] = (),
    artifacts: Mapping[str, str] | None = None,
) -> IssueDraft:
    """Render one incident as deterministic Markdown.

    `resolution` is required rather than re-derived: the caller already routed
    the incident, and the issue must state the decision that was actually made.
    The three inputs must refer to the same incident; a mismatch is a wiring bug,
    not something to paper over.
    """
    if impact.incident_id != incident.incident_id:
        raise IssueBuildError("impact report belongs to a different incident")
    if resolution is ResolutionType.PR:
        raise IssueBuildError("a PR-routed incident must be fixed, not filed as an issue")

    title = _title(incident, root_cause)
    lines: list[str] = [f"# {title}", ""]
    lines.extend(_incident_summary(incident))
    lines.extend(_root_cause_section(root_cause))
    lines.extend(_impact_section(impact))
    lines.extend(_attempts_section(attempts))
    lines.extend(_next_steps_section(root_cause.category))
    lines.extend(_artifacts_section(incident, artifacts))
    lines.extend(
        [
            "---",
            "",
            f"_Generated deterministically by dqs for incident `{incident.incident_id}`. "
            "Human review is required before any follow-up is published._",
            "",
        ]
    )
    return IssueDraft(
        incident_id=incident.incident_id,
        category=root_cause.category,
        resolution=resolution,
        title=title,
        markdown="\n".join(lines),
    )


def _title(incident: Incident, root_cause: RootCause) -> str:
    table = root_cause.affected_table or incident.table or "unknown table"
    short = str(incident.incident_id).split("-")[0]
    return f"[data-quality] {root_cause.category.value} on {table} ({short})"


def _incident_summary(incident: Incident) -> list[str]:
    lines = [
        "## Incident",
        "",
        f"- **Severity:** {incident.severity.value}",
        f"- **Status:** {incident.status.value}",
        f"- **Table:** `{incident.table or 'unknown'}`",
        f"- **Batches:** {_inline_list(incident.batches)}",
        f"- **Fingerprint:** `{incident.fingerprint}`",
        f"- **Detected:** {incident.created_at.isoformat()}",
        f"- **Last seen:** {incident.updated_at.isoformat()}",
        f"- **Violations:** {len(incident.violations)}",
        "",
    ]
    if incident.violations:
        lines.extend(["### Violations", ""])
        for violation in incident.violations:
            column = f" column `{violation.column}`" if violation.column else ""
            batch = f" in `{violation.batch_id}`" if violation.batch_id else ""
            lines.append(
                f"- `{violation.check}` ({violation.severity.value}) on "
                f"`{violation.table or 'unknown table'}`{column}{batch}: "
                f"{_one_line(violation.message, 500)}"
            )
        lines.append("")
    return lines


def _root_cause_section(root_cause: RootCause) -> list[str]:
    lines = [
        "## Root cause",
        "",
        f"- **Category:** `{root_cause.category.value}`",
        f"- **Confidence:** {root_cause.confidence:.2f}",
        f"- **Affected table:** `{root_cause.affected_table or 'not narrowed'}`",
        f"- **Affected columns:** {_inline_list(root_cause.affected_columns)}",
        "",
        _one_line(root_cause.summary, MAX_TEXT),
        "",
    ]
    if root_cause.evidence:
        lines.extend(["### Evidence", ""])
        for item in root_cause.evidence:
            lines.append(f"- `{item.tool}`: {_one_line(item.finding, 1000)}")
        lines.append("")
    if root_cause.hypotheses_rejected:
        lines.extend(["### Hypotheses rejected", ""])
        for hypothesis in root_cause.hypotheses_rejected:
            lines.append(f"- {_one_line(hypothesis, 1000)}")
        lines.append("")
    return lines


def _impact_section(impact: ImpactReport) -> list[str]:
    lines = [
        "## Impact",
        "",
        f"- **Scope:** {impact.scope.value}",
        f"- **Source table:** `{impact.source_table or 'warehouse-wide'}`",
        f"- **Column:** {f'`{impact.column}`' if impact.column else 'not narrowed'}",
        f"- **Blast radius score:** {impact.blast_radius_score}",
        "",
    ]
    if impact.summary:
        lines.extend([_one_line(impact.summary, MAX_TEXT), ""])
    if impact.affected_models:
        lines.extend(
            [
                "### Affected models",
                "",
                "| model | layer | distance | column lineage |",
                "|---|---|---|---|",
            ]
        )
        for model in impact.affected_models:
            lines.append(
                f"| `{model.name}` | {model.layer} | {model.distance} | "
                f"{'yes' if model.referencing_column else 'no'} |"
            )
        lines.append("")
    if impact.affected_exposures:
        lines.extend(
            [
                "### Affected exposures",
                "",
                "| exposure | owner | maturity |",
                "|---|---|---|",
            ]
        )
        for exposure in impact.affected_exposures:
            lines.append(
                f"| `{exposure.name}` | {exposure.owner or 'unowned'} | {exposure.maturity} |"
            )
        lines.append("")
    if impact.failing_tests:
        lines.extend(["### Failing dbt nodes (stored run record)", ""])
        for node in impact.failing_tests:
            lines.append(
                f"- `{node.name}` ({node.status})"
                f"{f' in `{node.batch_id}`' if node.batch_id else ''}"
            )
        lines.append("")
    return lines


def _attempts_section(attempts: Sequence[FixAttempt]) -> list[str]:
    lines = ["## Fix attempts", ""]
    if not attempts:
        lines.extend(
            [
                "No fix was attempted. Routing sent this incident straight to an issue.",
                "",
            ]
        )
        return lines
    for attempt in attempts:
        summary = attempt.plan.summary if attempt.plan is not None else "no valid plan produced"
        lines.extend(
            [
                f"### Attempt {attempt.attempt_no}: "
                f"{'PASSED' if attempt.verification.passed else 'FAILED'}",
                "",
                f"- **Plan:** {_one_line(summary, 1000)}",
                f"- **Failed criteria:** {_inline_list(attempt.verification.failed_criteria)}",
                f"- **dbt exit code:** {attempt.verification.dbt_exit_code}",
            ]
        )
        for criterion in attempt.verification.criteria:
            verdict = "PASS" if criterion.passed else "FAIL"
            lines.append(f"- {verdict} `{criterion.name}`: {_one_line(criterion.detail, 1000)}")
        if attempt.verification.dbt_log_excerpt:
            lines.extend(
                [
                    "",
                    "Redacted dbt excerpt:",
                    "",
                    "```text",
                    _excerpt(attempt.verification.dbt_log_excerpt),
                    "```",
                ]
            )
        lines.append("")
    return lines


def _next_steps_section(category: RootCauseCategory) -> list[str]:
    lines = ["## Suggested next steps", ""]
    lines.extend(f"- {step}" for step in NEXT_STEPS[category])
    lines.append("")
    return lines


def _artifacts_section(incident: Incident, artifacts: Mapping[str, str] | None) -> list[str]:
    lines = [
        "## Artifacts",
        "",
        f"- Incident record: `dq.incidents` / `{incident.incident_id}`",
        "- Agent timeline: `dq.agent_steps` (nodes `investigate` and `plan_fix`)",
        "- Verification attempts: `dq.fix_attempts`",
    ]
    for label, target in sorted((artifacts or {}).items()):
        safe_label = _one_line(label, 200).replace("[", "(").replace("]", ")")
        safe_target = (
            _one_line(target, 1000).replace(" ", "%20").replace("(", "%28").replace(")", "%29")
        )
        lines.append(f"- [{safe_label}]({safe_target})")
    lines.append("")
    return lines


def _inline_list(values: Sequence[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) if values else "none"


def _one_line(value: str, limit: int) -> str:
    """Collapse whitespace so hostile text cannot break the Markdown layout."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "..."


def _excerpt(value: str) -> str:
    # Keep the fence intact even if hostile dbt output contains backticks.
    text = value.replace("```", "'''")
    if len(text) <= MAX_EXCERPT:
        return text
    return text[-MAX_EXCERPT:]


__all__ = ["IssueBuildError", "IssueDraft", "NEXT_STEPS", "build_issue"]
