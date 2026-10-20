"""dbt evidence tools: the stored run record and a model's SQL source.

Why: Phase-3 review says the investigator should *lead* with
`dq.pipeline_runs.failed_nodes` (structured, batch-stamped, parsed at build
time), not scrape today's live log. `get_pipeline_run` therefore reads the
stored record only. `get_model_sql` returns the checked-in source SQL (never
compiled artifacts), which is the file a fix plan would edit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dqs.dbt_run import latest_run
from dqs.errors import ToolError
from dqs.tools.base import ToolContext, data_tool, validate_identifier

LOG_EXCERPT_CHARS = 2000


@data_tool
def get_pipeline_run(ctx: ToolContext, batch_id: str) -> dict[str, Any]:
    """Newest recorded dbt run for a batch: status, failed nodes, log excerpt."""
    record = latest_run(ctx.engine, batch_id)
    if record is None:
        return {
            "batch_id": batch_id,
            "found": False,
            "message": "no dq.pipeline_runs row exists for this batch",
        }
    return {
        "batch_id": record.batch_id,
        "found": True,
        "run_id": record.run_id,
        "command": record.command,
        "status": record.status,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "run_results_parsed_at": (
            record.run_results_parsed_at.isoformat() if record.run_results_parsed_at else None
        ),
        "failed_nodes": [
            {
                "name": node.name,
                "status": node.status,
                "message": node.message,
                "batch_id": node.batch_id,
            }
            for node in record.failed_nodes
        ],
        "log_excerpt": record.log_excerpt[-LOG_EXCERPT_CHARS:],
    }


def _model_files(project_dir: Path, name: str) -> list[Path]:
    models = project_dir / "models"
    if not models.is_dir():
        raise ToolError(f"dbt models directory not found at {models}")
    return sorted(path for path in models.rglob(f"{name}.sql") if path.is_file())


@data_tool
def get_model_sql(ctx: ToolContext, model_name: str) -> dict[str, Any]:
    """Return one dbt model's source SQL (by bare model name, e.g. stg_orders)."""
    name = validate_identifier(model_name, kind="model")
    matches = _model_files(ctx.project_dir, name)
    if not matches:
        raise ToolError(f"no dbt model named {name!r} under {ctx.project_dir / 'models'}")
    if len(matches) > 1:
        raise ToolError(
            f"model name {name!r} is ambiguous: "
            + ", ".join(str(path.relative_to(ctx.project_dir)) for path in matches)
        )
    path = matches[0]
    try:
        sql = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"could not read {path}: {exc}") from exc
    return {
        "model": name,
        "path": str(path.relative_to(ctx.project_dir)).replace("\\", "/"),
        "sql": sql,
    }
