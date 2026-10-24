"""Shared Streamlit chrome: page config, sidebar database selector, safe formatting.

Why a module and not copy-pasted code in five scripts: every page must resolve
the *same* database selection and the same service layer, and all data-derived
text must be rendered through one small set of helpers (plain text, bounded
previews). Keeping that here also gives tests a single injection seam: a page
reads `get_service()` / `get_settings()`, which fall back to the real service
module and `.env` unless the AppTest session has replaced them.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import streamlit as st
from sqlalchemy import Engine

from dqs import services
from dqs.config import Settings, load_settings
from dqs.errors import DqsError
from dqs.services import DatabaseChoice, IncidentRow

SERVICE_STATE_KEY = "_dqs_service"
SETTINGS_STATE_KEY = "_dqs_settings"

# Anchored to the repository root (`ui/common.py` -> parents[1]); the UI never
# guesses paths from the process working directory.
REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "contracts"
BENCHMARK_RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"


def configure_page(title: str) -> None:
    """Set page config (must run before any other Streamlit command)."""
    st.set_page_config(
        page_title=f"DQS · {title}",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def get_service() -> Any:
    """The real `dqs.services` module, or the test double injected by AppTest."""
    injected = st.session_state.get(SERVICE_STATE_KEY)
    return services if injected is None else injected


def get_settings() -> Settings:
    """Settings from `.env`, or the explicit test settings injected by AppTest."""
    injected = st.session_state.get(SETTINGS_STATE_KEY)
    return load_settings() if injected is None else injected


@st.cache_resource(show_spinner=False)
def _cached_engine(database: str) -> Engine:
    """One pooled read-only engine per database for the life of the server."""
    return services.engine_for(load_settings(), database)


def sidebar_context() -> tuple[Settings, str, Engine]:
    """Render the sidebar and return (settings, database, read-only engine)."""
    settings = get_settings()
    service = get_service()

    st.sidebar.title("Data Quality Steward")
    st.sidebar.caption("Human review console")
    st.sidebar.divider()

    try:
        choices: list[DatabaseChoice] = service.database_choices(settings)
    except DqsError as exc:
        st.sidebar.error(f"Could not list databases: {exc}")
        choices = [DatabaseChoice(name="warehouse", is_default=True)]
    if not choices:
        choices = [DatabaseChoice(name="warehouse", is_default=True)]

    names = [choice.name for choice in choices]
    default_index = next((index for index, choice in enumerate(choices) if choice.is_default), 0)
    database = st.sidebar.selectbox(
        "Database",
        names,
        index=default_index,
        key="dqs_database",
        help="dqs_demo is the demo warehouse; benchmark scenario databases are hidden.",
    )

    if service is services:
        engine = _cached_engine(database)
    else:
        engine = service.engine_for(settings, database)

    st.sidebar.text(f"Publish mode: {services.effective_publish_mode(settings)}")
    st.sidebar.caption("Read-only console; only approve/reject can write.")
    return settings, database, engine


def publish_banner(mode: str) -> None:
    """The prominent effective-mode banner shown on the approval payload."""
    if mode == "live":
        st.warning(
            "Effective publish mode: LIVE. Approving opens a real GitHub PR (or issue) "
            "on the configured repository. Nothing is merged and no default branch is touched."
        )
    else:
        st.info(
            "Effective publish mode: dry_run. Approving writes a local PR preview under "
            "out/pr_previews/ and never calls GitHub."
        )


def format_ts(value: dt.datetime | None) -> str:
    """UTC timestamp for display (plain text; empty for a missing value)."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")


def incident_label(row: IncidentRow) -> str:
    """A compact stable label for the incident picker."""
    table = row.table or "(batch-level)"
    return f"{str(row.incident_id)[:8]} · {table} · {row.status} · {row.severity}"


def incident_rows_frame(rows: list[IncidentRow]) -> list[dict[str, str]]:
    """Plain dict rows for `st.dataframe` (no model objects cross the boundary)."""
    return [
        {
            "Incident": str(row.incident_id),
            "Status": row.status,
            "Severity": row.severity,
            "Table": row.table or "(batch-level)",
            "Created": format_ts(row.created_at),
            "Updated": format_ts(row.updated_at),
            "Summary": row.summary,
        }
        for row in rows
    ]


def error_text(exc: BaseException) -> str:
    """Text for `st.error`; database errors never contain the password."""
    return str(exc) or exc.__class__.__name__
