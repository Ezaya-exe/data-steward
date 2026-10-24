"""Contracts page: list every YAML contract, its status and the exact file text.

Why the YAML and not a parsed form: the contract file is the reviewed artifact
(§7.1), so a reviewer must see the same bytes that checks load. `to_yaml`
re-renders the validated model deterministically, which is what the generator
writes to disk.
"""

from __future__ import annotations

import common
import streamlit as st

from dqs.errors import DqsError

common.configure_page("Contracts")
settings, database, engine = common.sidebar_context()
service = common.get_service()

st.title("Contracts")
st.caption(f"Directory: contracts/ · database: {database}")

try:
    contracts = service.list_contracts(common.CONTRACTS_DIR)
except DqsError as exc:
    st.error(f"Could not load contracts: {common.error_text(exc)}")
    contracts = []

if not contracts:
    st.info(
        "No contracts found. Generate them with `dqs contracts generate` "
        "after a baseline has been loaded."
    )
    st.stop()

approved, draft = service.contract_counts(contracts)
approved_col, draft_col, total_col = st.columns(3)
approved_col.metric("Approved", approved)
draft_col.metric("Draft", draft)
total_col.metric("Total", len(contracts))

st.dataframe(
    [
        {
            "Table": contract.table,
            "Layer": contract.layer,
            "Status": contract.status,
            "Generated": common.format_ts(contract.generated_at),
        }
        for contract in contracts
    ],
    hide_index=True,
    width="stretch",
)

st.divider()
st.subheader("Contract YAML")
tables = [contract.table for contract in contracts]
picked = st.selectbox("Table", tables, key="contract_table")
selected = next(contract for contract in contracts if contract.table == picked)
st.text(f"Status: {selected.status} · layer: {selected.layer}")
st.code(selected.yaml, language="yaml")
