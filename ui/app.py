"""Incidents page: the triage table with status/severity/table/search filters.

Why a separate page: the detail payload is large and the list is what an
operator scans first. The page never writes; `incident_detail.py` owns the one
human decision.
"""

from __future__ import annotations

import common
import streamlit as st

from dqs.errors import DqsError

common.configure_page("Incidents")
settings, database, engine = common.sidebar_context()
service = common.get_service()

st.title("Incidents")
st.caption(f"Database: {database} · newest updated first")

try:
    rows = service.list_incident_rows(engine)
except DqsError as exc:
    st.error(f"Could not load incidents: {common.error_text(exc)}")
    rows = []

if not rows:
    st.info(
        "No incidents in this database yet. Load a batch and run `dqs check batch` "
        "(or `dqs demo` on the demo warehouse) to create one."
    )
else:
    statuses = sorted({row.status for row in rows})
    severities = sorted({row.severity for row in rows})
    first, second, third, fourth = st.columns([1, 1, 1, 1.6])
    selected_statuses = first.multiselect("Status", statuses, key="filter_status")
    selected_severities = second.multiselect("Severity", severities, key="filter_severity")
    table_term = third.text_input("Table contains", key="filter_table")
    search_term = fourth.text_input("Search id, fingerprint or summary", key="filter_search")

    filtered = service.filter_incident_rows(
        rows,
        statuses=selected_statuses,
        severities=selected_severities,
        table=table_term,
        search=search_term,
    )
    st.caption(f"{len(filtered)} of {len(rows)} incidents")

    if not filtered:
        st.info("No incident matches these filters.")
    else:
        st.dataframe(
            common.incident_rows_frame(filtered),
            hide_index=True,
            width="stretch",
        )
        labels = {common.incident_label(row): str(row.incident_id) for row in filtered}
        picked = st.selectbox("Select an incident", list(labels), key="incident_pick")
        if st.button("Open incident detail", key="open_detail"):
            st.session_state["selected_incident"] = labels[picked]
            st.switch_page("pages/incident_detail.py")

st.divider()
st.caption(
    "Approval actions live on the incident detail page. This console is read-only "
    "except for approve/reject."
)
