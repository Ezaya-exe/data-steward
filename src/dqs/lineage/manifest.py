"""dbt manifest loading and staleness handling (ARCHITECTURE §9).

Why here: the lineage graph consumes ``warehouse/dbt_project/target/manifest.json``,
but that file is (1) gitignored, (2) silently overwritten by the next dbt
invocation, and (3) only complete — with ``compiled_code`` on every model —
after ``dbt compile``. This module is the single point that finds, validates
and, when missing/stale, regenerates it.

Regeneration rule (DECISIONS 57): a manifest is stale when it is absent,
unparseable, lacks ``nodes``, or its file mtime is older than the newest
``models/**`` file (models, schema.yml, exposures.yml — anything that changes
the DAG). ``dbt compile`` is used because it refreshes both the manifest and
the compiled SQL the column-level heuristic reads. On dbt failure the error is
surfaced loudly: a silently wrong DAG is worse than none for impact analysis.

Unit tests never come here — they feed a checked-in fixture straight to
``LineageGraph.from_manifest`` (DECISIONS 59).
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

from dqs.errors import ConfigError, LineageError

logger = logging.getLogger(__name__)

PROJECT_DIR: Final = Path(__file__).resolve().parents[2] / "warehouse" / "dbt_project"


def manifest_path(project_dir: Path = PROJECT_DIR) -> Path:
    """Where the parsed manifest for ``project_dir`` lives."""
    return project_dir / "target" / "manifest.json"


def dbt_executable() -> Path:
    """Console script installed next to the current interpreter (no PATH lookup)."""
    suffix = ".exe" if sys.platform == "win32" else ""
    path = Path(sys.executable).with_name(f"dbt{suffix}")
    if not path.exists():
        raise ConfigError(f"dbt executable not found at {path}; install dbt-core in the venv")
    return path


def _newest_models_mtime(project_dir: Path) -> float | None:
    inputs = [
        p.stat().st_mtime
        for p in (project_dir / "models").rglob("*")
        if p.is_file()
    ]
    return max(inputs) if inputs else None


def is_stale(path: Path, project_dir: Path) -> bool:
    """True when the manifest is missing/invalid/older than the model sources."""
    if not path.exists():
        return True
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or not loaded.get("nodes"):
            return True
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("manifest unreadable (%s) — treating as stale", exc)
        return True
    stamp = path.stat().st_mtime
    newest = _newest_models_mtime(project_dir)
    return newest is not None and stamp < newest


def regenerate(project_dir: Path = PROJECT_DIR) -> None:
    """Run ``dbt compile`` so a fresh manifest with compiled SQL is written.

    Raises LineageError with the dbt excerpt — the caller (CLI) must say
    clearly that the DAG could not be rebuilt rather than approving a wrong
    impact report.
    """
    cmd = [
        str(dbt_executable()),
        "compile",
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
    ]
    logger.info("regenerating manifest: %s", " ".join(cmd))
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        output = completed.stdout + "\n" + completed.stderr
        excerpt = output[-1500:].strip()
        raise LineageError(f"dbt compile failed while regenerating the manifest: {excerpt}")


def load_manifest(
    project_dir: Path = PROJECT_DIR, regenerate_when_stale: bool = True
) -> dict[str, Any]:
    """Return the parsed manifest dict, regenerating it when missing/stale."""
    path = manifest_path(project_dir)
    if regenerate_when_stale and is_stale(path, project_dir):
        logger.info("manifest missing or stale at %s — regenerating", path)
        regenerate(project_dir)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LineageError(
            f"no dbt manifest at {path}; run 'dqs dbt build' (or dqs db init + dbt compile) first"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LineageError(f"manifest at {path} is unreadable: {exc}") from exc
    if not isinstance(loaded, dict) or not loaded.get("nodes"):
        raise LineageError(f"manifest at {path} contains no nodes; run 'dqs dbt build'")
    return loaded
