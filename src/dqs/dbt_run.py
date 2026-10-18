"""dbt subprocess wrapper (``dbt build``) with run recording in ``dq.pipeline_runs``.

Why here: agents/CLI never execute raw dbt through an agent; this is the one
place dbt is invoked, always with explicit ``--project-dir/--profiles-dir/
--target`` (no cwd/P dependency), and every run — success or failure — is
recorded with a log excerpt so the investigator (Phase 4) sees what a human
engineer on-call would see first.
"""

from __future__ import annotations

import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from dqs import db as dbmod
from dqs.config import Settings
from dqs.errors import ConfigError, DatabaseError

logger = logging.getLogger(__name__)

PROJECT_DIR: Final = Path(__file__).resolve().parents[2] / "warehouse" / "dbt_project"
LOG_EXCERPT_BYTES: Final = 4096


def dbt_executable() -> Path:
    """Console script installed next to the current interpreter (no PATH lookup)."""
    suffix = ".exe" if sys.platform == "win32" else ""
    path = Path(sys.executable).with_name(f"dbt{suffix}")
    if not path.exists():
        raise ConfigError(f"dbt executable not found at {path}; install dbt-core in the venv")
    return path


def run_dbt_build(
    settings: Settings,
    target: str = "dev",
    select: str | None = None,
    batch_id: str | None = None,
) -> tuple[int, str]:
    """Run ``dbt build`` and record it in dq.pipeline_runs; return (exit_code, output).

    Failing dbt builds are recorded as ``failed`` runs (not raised) because a
    failed build is a data point — exactly what the simulator wants to capture.
    """
    cmd = [
        str(dbt_executable()),
        "build",
        "--target",
        target,
        "--project-dir",
        str(PROJECT_DIR),
        "--profiles-dir",
        str(PROJECT_DIR),
    ]
    if select:
        cmd += ["--select", select]
    logger.info("run dbt: %s", " ".join(cmd))
    started = dt.datetime.now(dt.UTC)
    completed = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True, text=True)
    finished = dt.datetime.now(dt.UTC)
    output = (
        completed.stdout if completed.stderr == "" else completed.stdout + "\n" + completed.stderr
    )
    if not output.strip():
        output = completed.stderr or completed.stdout or "(no output)"

    engine = dbmod.make_rw_engine(settings)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dq.pipeline_runs (batch_id, command, status, started_at, "
                    "finished_at, log_excerpt) VALUES (:b, :cmd, :status, :s, :f, :log)"
                ),
                {
                    "b": batch_id,
                    "cmd": "dbt build --target "
                    + target
                    + (" --select " + select if select else ""),
                    "status": "success" if completed.returncode == 0 else "failed",
                    "s": started,
                    "f": finished,
                    "log": output[-LOG_EXCERPT_BYTES:],
                },
            )
    except SQLAlchemyError as exc:
        # the dbt run itself succeeded/failed before this; never hide it
        raise DatabaseError(f"could not record pipeline run: {exc}") from exc
    finally:
        engine.dispose()
    return completed.returncode, output


def last_runs(engine: Engine, limit: int = 5) -> list[tuple[str, str, str]]:
    """Most recent (batch_id, status, command) rows, for quick CLI summaries."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT batch_id, status, command FROM dq.pipeline_runs "
                "ORDER BY run_id DESC LIMIT :n"
            ),
            {"n": limit},
        ).fetchall()
    return [(str(b), str(s), str(c)) for b, s, c in rows]
