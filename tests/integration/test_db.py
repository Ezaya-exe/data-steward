"""Integration tests: db layer + `dqs_ro` guardrails (Phase 0 acceptance).

Needs Docker Postgres (`dqs dev up`); skipped otherwise (see tests/conftest.py).
The ro-role tests are the documented guardrail seed: the read-only role can
read `dq.*` and must fail on INSERT and CREATE — anything the agent could do
through it later must be write-blocked by the database itself, not by our code.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from dqs import db as dbmod
from dqs.config import Settings

pytestmark = pytest.mark.integration


def test_ping_returns_server_version_for_both_roles(settings: Settings) -> None:
    versions = dbmod.ping(settings)
    assert "PostgreSQL" in versions["rw"]
    assert "PostgreSQL" in versions["ro"]


def test_ro_role_can_select_dq_tables(ro_engine: Engine) -> None:
    with ro_engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM dq.batches")).scalar_one()
    assert count >= 0


def test_ro_role_cannot_insert(ro_engine: Engine) -> None:
    with ro_engine.connect() as conn:
        with pytest.raises(ProgrammingError):
            conn.exec_driver_sql(
                "INSERT INTO dq.batches (batch_id, business_date, arrived_at) "
                "VALUES ('probe_ro_denied', '2026-01-01', now())"
            )


def test_ro_role_cannot_create_tables(ro_engine: Engine) -> None:
    with ro_engine.connect() as conn:
        with pytest.raises(ProgrammingError):
            conn.exec_driver_sql("CREATE TABLE dq.probe_ro_denied (id integer)")


def test_init_is_idempotent(settings: Settings) -> None:
    # one run may legitimately create objects; the second must be a clean no-op
    dbmod.init_database(settings)
    dbmod.init_database(settings)
