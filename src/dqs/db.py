"""Database access layer.

Isolates warehouse side effects (engine creation, DDL, ping) behind small
functions that accept a `Settings` and can be swapped in tests. Two roles are
used (AGENTS.md §7.2): `dqs` owns schemas and writes; `dqs_ro` is SELECT-only
and will be the only role behind agent SQL tools from Phase 4.

`init_database` replays idempotent SQL files (roles, schemas, dq tables) and
is therefore safe to run any number of times; `reset_database` drops the
steward schemas (constant names, never user input) and re-initializes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from dqs.config import Settings
from dqs.errors import DatabaseError

logger = logging.getLogger(__name__)

# SCHEMAS/tables here are compile-time constants; identifiers are never taken
# from user input, so no allowlist is needed (AGENTS.md §6 allows constants).
SCHEMAS: Final = ("raw", "staging", "marts", "dq")
SQL_DIR: Final = Path(__file__).resolve().parents[2] / "sql"
INIT_SQL_FILES: Final = ("000_roles.sql", "001_init.sql")


def make_rw_engine(settings: Settings) -> Engine:
    """Engine for the owner role (`dqs`): app writes + dbt."""
    return create_engine(settings.rw_database_url, pool_pre_ping=True)


def make_ro_engine(settings: Settings) -> Engine:
    """Engine for the read-only role (`dqs_ro`): agent tools (Phase 4+)."""
    return create_engine(settings.ro_database_url, pool_pre_ping=True)


def _engine_url(engine: Engine) -> URL:
    return engine.url


def server_version(engine: Engine) -> str:
    """`SELECT version()` text, so ping shows which server/role answered."""
    try:
        with engine.connect() as conn:
            return str(conn.execute(text("SELECT version()")).scalar_one())
    except SQLAlchemyError as exc:
        host = _engine_url(engine).host
        raise DatabaseError(f"cannot query server at {host}: {exc}") from exc


def init_database(settings: Settings, sql_dir: Path | None = None) -> None:
    """Create roles, schemas and dq tables by replaying idempotent SQL files.

    Runs each file as one multi-statement round trip; every statement is
    `IF NOT EXISTS`/grant/DO-block, so calling this twice is a no-op the
    second time (Phase 0 acceptance).
    """
    base = sql_dir or SQL_DIR
    engine = make_rw_engine(settings)
    try:
        with engine.connect() as conn:
            for name in INIT_SQL_FILES:
                path = base / name
                sql = path.read_text(encoding="utf-8")
                logger.info("init: replaying %s", path)
                conn.exec_driver_sql(sql)
            conn.commit()
    except (OSError, SQLAlchemyError) as exc:
        raise DatabaseError(f"db init failed: {exc}") from exc
    finally:
        engine.dispose()


def reset_database(settings: Settings) -> None:
    """Drop raw/staging/marts/dq with their objects, then re-init.

    Destructive by definition — this is the documented meaning of
    `dqs db reset` (docs/PHASES.md Phase 1 uses it for clean re-seeds).
    """
    engine = make_rw_engine(settings)
    try:
        with engine.connect() as conn:
            for schema in SCHEMAS:
                conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"db reset failed: {exc}") from exc
    finally:
        engine.dispose()
    init_database(settings)


def ping(settings: Settings) -> dict[str, str]:
    """Server version for both roles; expected in acceptance output."""
    out: dict[str, str] = {}
    for role, engine in (("rw", make_rw_engine(settings)), ("ro", make_ro_engine(settings))):
        try:
            out[role] = server_version(engine)
        finally:
            engine.dispose()
    return out
