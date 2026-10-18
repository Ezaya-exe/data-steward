"""Shared pytest fixtures.

Integration fixtures probe Postgres with one cheap query and skip the whole
module when it is unreachable (per owner decision: DB-dependent acceptance
stays PENDING rather than being satisfied with a substitute DB).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs import db as dbmod
from dqs.config import Settings, load_settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session")
def rw_engine(settings: Settings) -> Iterator[Engine]:
    """`dqs` read/write engine, or skip when Postgres is down."""
    engine = _engine_or_skip(dbmod.make_rw_engine(settings))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def ro_engine(settings: Settings) -> Iterator[Engine]:
    """`dqs_ro` read-only engine, or skip when Postgres is down."""
    engine = _engine_or_skip(dbmod.make_ro_engine(settings))
    try:
        yield engine
    finally:
        engine.dispose()


def _engine_or_skip(engine: Engine) -> Engine:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        engine.dispose()
        pytest.skip(f"Postgres not reachable: {exc}")
    return engine
