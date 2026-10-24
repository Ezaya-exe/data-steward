"""Incident detail: the complete approval payload, plus the approve/reject gate.

Why this page is the only writer: §13 says the human decides after seeing the
root cause, impact, diff and verification report. Every section below reads a
typed view model from `dqs.services`; the page itself contains no SQL and no
workflow wiring. Data and model text is rendered with `st.text` / `st.code` /
`st.dataframe` so hostile strings cannot become HTML.
"""

from __future__ import annotations

import json
import uuid

import common
import streamlit as st

from dqs import services
from dqs.errors import DqsError
from dqs.models.enums import IncidentStatus

common.configure_page("Incident detail")
settings, database, engine = common.sidebar_context()
service = common.get_service()

st.title("Incident detail")
message = st.session_state.pop("decision_message", None)
if message:
    st.success(message)

try:
    rows = service.list_incident_rows(engine)
except DqsError as exc:
    st.error(f"Could not load incidents: {common.error_text(exc)}")
    rows = []

if not rows:
    st.info("No incidents in this database, so there is no detail to show.")
    st.stop()

labels = {common.incident_label(row): str(row.incident_id) for row in rows}
stored = st.session_state.get("selected_incident")
default_index = 0
if stored:
    default_index = next(
        (index for index, row in enumerate(rows) if str(row.incident_id) == stored), 0
    )
picked = st.selectbox("Incident", list(labels), index=default_index, key="detail_incident")
incident_id = uuid.UUID(labels[picked])

try:
    detail = service.incident_detail(engine, incident_id)
except DqsError as exc:
    st.error(f"Could not load incident detail: {common.error_text(exc)}")
    st.stop()
if detail is None:
    st.warning(f"Incident {incident_id} no longer exists.")
    st.stop()

incident = detail.incident
for warning in detail.warnings:
    st.warning(warning)

status_col, severity_col, table_col, created_col = st.columns(4)
status_col.metric("Status", incident.status.value)
severity_col.metric("Severity", incident.severity.value)
table_col.metric("Table", incident.table or "(batch-level)")
created_col.metric("Created", common.format_ts(incident.created_at))
st.text(f"Incident id: {incident.incident_id}")
st.text(f"Fingerprint: {incident.fingerprint}")
st.text(f"Batches: {', '.join(incident.batches) or 'none'}")

st.divider()
st.subheader("Violations")
if not incident.violations:
    st.info("No violations stored on this incident.")
else:
    st.dataframe(
        [
            {
                "Check": violation.check,
                "Column": violation.column or "",
                "Severity": violation.severity.value,
                "Message": violation.message,
                "Observed": json.dumps(violation.observed, sort_keys=True, default=str),
                "Sample": " | ".join(violation.sample[:5]),
            }
            for violation in incident.violations
        ],
        hide_index=True,
        width="stretch",
    )

st.divider()
st.subheader("Agent timeline")
if not detail.steps:
    st.info("No agent steps recorded yet (the investigation may not have started).")
else:
    st.dataframe(
        [
            {
                "#": step.seq,
                "Node": step.node,
                "Kind": step.kind,
                "Step": step.name,
                "Args": step.args_preview,
                "Result": step.result_preview,
                "Latency (ms)": step.latency_ms,
                "Tokens in": step.tokens_in,
                "Tokens out": step.tokens_out,
            }
            for step in detail.steps
        ],
        hide_index=True,
        width="stretch",
    )

st.divider()
st.subheader("Root cause")
if detail.root_cause is None:
    st.info("No root cause recorded for this incident yet.")
else:
    root = detail.root_cause
    st.text(f"Category: {root.category.value}")
    st.text(f"Confidence: {root.confidence:.2f} — model-reported, uncalibrated")
    st.text("Summary:")
    st.text(root.summary)
    if root.affected_table or root.affected_columns:
        columns = ", ".join(root.affected_columns) or "none"
        st.text(f"Affected: {root.affected_table or '(table not given)'} · columns: {columns}")
    st.text(f"Suggested resolution: {root.suggested_resolution.value}")
    if root.evidence:
        st.text("Evidence (each item cites the tool that produced it):")
        for item in root.evidence:
            st.text(f"[{item.tool}] {item.finding}")
    if root.hypotheses_rejected:
        st.text("Hypotheses rejected:")
        for hypothesis in root.hypotheses_rejected:
            st.text(f"- {hypothesis}")

st.divider()
st.subheader("Impact")
if detail.impact is None:
    st.info("No impact report recorded for this incident yet.")
else:
    impact = detail.impact
    st.text(f"Scope: {impact.scope.value}")
    st.text(f"Blast radius score: {impact.blast_radius_score:.2f}")
    if impact.summary:
        st.text(impact.summary)
    if impact.affected_models:
        st.text("Affected models:")
        st.dataframe(
            [
                {
                    "Model": model.name,
                    "Layer": model.layer,
                    "Hops": model.distance,
                    "Column lineage": model.referencing_column,
                }
                for model in impact.affected_models
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.text("No downstream models affected.")
    if impact.affected_exposures:
        st.text("Affected exposures (owners):")
        st.dataframe(
            [
                {
                    "Exposure": exposure.name,
                    "Owner": exposure.owner,
                    "Maturity": exposure.maturity,
                }
                for exposure in impact.affected_exposures
            ],
            hide_index=True,
            width="stretch",
        )

st.divider()
st.subheader("Fix attempts")
if not detail.attempts:
    st.info("No fix attempts recorded yet.")
for attempt in detail.attempts:
    if attempt.verification_passed is None:
        verdict = "unknown"
    else:
        verdict = "passed" if attempt.verification_passed else "failed"
    with st.expander(
        f"Attempt {attempt.attempt_no} — verification {verdict}",
        expanded=attempt is detail.attempts[-1],
    ):
        st.text(f"Plan: {attempt.summary or '(no summary)'}")
        st.text(f"Category: {attempt.category or '(unknown)'}")
        st.text(f"Target files: {', '.join(attempt.target_files) or '(none)'}")
        st.text(f"Expected effect: {attempt.expected_effect or '(not recorded)'}")
        st.text(f"Risk: {attempt.risk or '(not recorded)'}")
        if attempt.failed_criteria:
            st.text("Failed criteria: " + ", ".join(attempt.failed_criteria))
        if attempt.checklist:
            st.dataframe(
                [
                    {
                        "Criterion": item.name,
                        "Result": "PASS" if item.passed else "FAIL",
                        "Detail": item.detail,
                        "Observed": item.observed,
                    }
                    for item in attempt.checklist
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("No verification criteria recorded for this attempt.")

st.subheader("Diff viewer")
if not detail.attempts:
    st.info("No diff to show without a fix attempt.")
else:
    diffs = {f"Attempt {attempt.attempt_no}": attempt for attempt in detail.attempts}
    chosen = st.selectbox(
        "Diff from attempt",
        list(diffs),
        index=len(diffs) - 1,
        key="diff_attempt",
    )
    selected_attempt = diffs[chosen]
    if selected_attempt.diff_error:
        st.warning(selected_attempt.diff_error)
    elif selected_attempt.diff:
        st.code(selected_attempt.diff, language="diff")
    else:
        st.info("This attempt has no rendered diff.")

st.divider()
st.subheader("Publications")
if not detail.publications:
    st.info("No publication recorded. A dry-run approval creates a local preview, not a PR.")
else:
    st.dataframe(
        [
            {
                "Kind": publication.kind,
                "Mode": publication.mode,
                "Status": publication.status,
                "Created": common.format_ts(publication.created_at),
                "URL or preview path": publication.url,
            }
            for publication in detail.publications
        ],
        hide_index=True,
        width="stretch",
    )
    for index, publication in enumerate(detail.publications):
        if publication.url.startswith(("https://", "http://")):
            st.link_button(
                f"Open {publication.kind} ({publication.mode})",
                publication.url,
                key=f"publication_link_{index}",
            )

st.divider()
st.subheader("Human approval")
mode = services.effective_publish_mode(settings)
common.publish_banner(mode)
if incident.status is not IncidentStatus.AWAITING_APPROVAL:
    st.info(
        f"Approval actions are disabled: incident status is {incident.status.value}. "
        "They become available only when the workflow is awaiting_approval."
    )
else:
    live = mode == "live"
    if live:
        confirmed = st.checkbox(
            "I understand this will write to GitHub (live mode)", key="live_confirm"
        )
    else:
        confirmed = True
    approver = st.text_input("Approver", value="ui-operator", key="approver")
    note = st.text_area("Note (required to reject)", key="decision_note")
    approve_col, reject_col = st.columns(2)
    approve = approve_col.button(
        "Approve and resume",
        key="approve",
        disabled=not confirmed or not approver.strip(),
    )
    reject = reject_col.button(
        "Reject and resume",
        key="reject",
        disabled=not confirmed or not approver.strip() or not note.strip(),
    )
    if approve or reject:
        decision = "approve" if approve else "reject"
        try:
            result = service.decide_incident(
                settings,
                incident_id,
                decision=decision,
                approver=approver,
                note=note,
                database=database,
            )
        except DqsError as exc:
            st.error(f"Could not resume the workflow: {common.error_text(exc)}")
        else:
            st.session_state["decision_message"] = result.message
            st.rerun()
