"""dbt subprocess wrapper (``dbt build``) with run recording in ``dq.pipeline_runs``.

Why here: agents/CLI never execute raw dbt through an agent; this is the one
place dbt is invoked, always with explicit ``--project-dir/--profiles-dir/
--target`` (no cwd/P dependency), and every run — success or failure — is
recorded with a log excerpt so the investigator (Phase 4) sees what a human
engineer on-call would see first.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Final

from pydantic import ValidationError
from sqlalchemy import Row, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from dqs import db as dbmod
from dqs.config import Settings
from dqs.errors import ConfigError, DatabaseError
from dqs.models.pipeline import NodeResult, PipelineRunRecord

logger = logging.getLogger(__name__)

PROJECT_DIR: Final = Path(__file__).resolve().parents[2] / "warehouse" / "dbt_project"
LOG_EXCERPT_BYTES: Final = 4096
RUN_RESULTS_PATH: Final = PROJECT_DIR / "target" / "run_results.json"


def dbt_executable() -> Path:
    """Console script installed next to the current interpreter (no PATH lookup)."""
    suffix = ".exe" if sys.platform == "win32" else ""
    path = Path(sys.executable).with_name(f"dbt{suffix}")
    if not path.exists():
        raise ConfigError(f"dbt executable not found at {path}; install dbt-core in the venv")
    return path


def parse_run_results(
    run_results_path: Path = RUN_RESULTS_PATH, batch_id: str | None = None
) -> list[NodeResult]:
    """Extract failed/errored nodes from a dbt ``run_results.json`` (pure).

    Called **immediately after** each dbt build (DECISIONS 55): the live file is
    overwritten by the next build, so the only trustworthy form is the payload
    persisted with the run. Elements whose resource_type is not a node under
    test-execution (test/model/seed/snapshot) or whose status is not
    ``fail``/``error`` are skipped — successful nodes are not interesting for
    impact and keep the payload small.
    """
    try:
        payload = json.loads(run_results_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        # A dbt build that crashed hard may leave a torn or empty file; report
        # the reason once instead of raising — the run status itself is the
        # primary fact and is recorded regardless.
        logger.warning("run_results.json unreadable (%s); storing no failed nodes", exc)
        return []
    manifest = payload.get("results", [])
    nodes: list[NodeResult] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).lower()
        if status not in ("fail", "error"):
            continue
        unique_id = str(entry.get("unique_id", ""))
        if not unique_id:
            continue
        nodes.append(
            NodeResult(
                unique_id=unique_id,
                # dbt's own name field when present (tests' unique_ids end in a
                # content hash, which would otherwise become the name)
                name=str(entry.get("name") or unique_id.split(".")[-1]),
                status=status,
                message=str(entry.get("message", "") or "")[:1000],
                batch_id=batch_id,
            )
        )
    nodes.sort(key=lambda n: (n.unique_id, n.message))
    return nodes


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

    # Persist the structured failure record *with* the run row (DECISIONS 55):
    # reading the live run_results.json later would race the next build.
    failed_nodes = parse_run_results(batch_id=batch_id)
    parsed_at = dt.datetime.now(dt.UTC)

    engine = dbmod.make_rw_engine(settings)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dq.pipeline_runs (batch_id, command, status, started_at, "
                    "finished_at, log_excerpt, failed_nodes, run_results_parsed_at) "
                    "VALUES (:b, :cmd, :status, :s, :f, :log, CAST(:nodes AS jsonb), :parsed)"
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
                    "nodes": json.dumps(
                        [n.model_dump(mode="json") for n in failed_nodes]
                    ),
                    "parsed": parsed_at,
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


_FAILED_NODES_SQL: Final = (
    "SELECT run_id, batch_id, command, status, started_at, finished_at, log_excerpt, "
    "run_results_parsed_at, failed_nodes FROM dq.pipeline_runs "
    "WHERE failed_nodes IS NOT NULL AND failed_nodes != CAST('null' AS jsonb) "
)


def _row_to_record(row: Row[Any]) -> PipelineRunRecord | None:
    vals = tuple(row)
    raw_nodes = vals[8]
    nodes: list[NodeResult] = []
    if isinstance(raw_nodes, list):
        for raw in raw_nodes:
            try:
                nodes.append(NodeResult.model_validate(raw))
            except ValidationError as exc:
                # One malformed entry must not erase the rest (guardrail §7.8:
                # records are data; log and continue).
                logger.warning("skipping malformed failed_nodes entry: %s", exc)
    if not nodes:
        return None
    return PipelineRunRecord(
        run_id=vals[0],
        batch_id=vals[1],
        command=vals[2],
        status=vals[3],
        started_at=vals[4],
        finished_at=vals[5],
        log_excerpt=vals[6] or "",
        run_results_parsed_at=vals[7],
        failed_nodes=nodes,
    )


def latest_run_for_batch(engine: Engine, batch_id: str | None = None) -> PipelineRunRecord | None:
    """Newest parsed failing-node record, scoped to ``batch_id`` when given.

    The impact report reads ONLY this stored record (DECISIONS 55) — it never
    opens target/run_results.json, which a later build could have replaced.
    Returns None when no run carries a parsed failure payload.
    """
    sql = _FAILED_NODES_SQL
    params: dict[str, object] = {}
    if batch_id is not None:
        sql += " AND batch_id = :b"
        params["b"] = batch_id
    sql += " ORDER BY run_id DESC LIMIT 1"
    with engine.connect() as conn:
        row = conn.execute(text(sql), params).fetchone()
    return None if row is None else _row_to_record(row)
