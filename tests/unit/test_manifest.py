"""Unit tests for manifest loading/staleness rules (DECISIONS 57).

No dbt involved: hand-written manifests on a tmp project dir.
"""

from __future__ import annotations

import json
from pathlib import Path

from dqs.lineage.manifest import is_stale


def _project(tmp_path: Path, body: dict) -> Path:
    (tmp_path / "models").mkdir(parents=True)
    (tmp_path / "models" / "model.sql").write_text("select 1", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir(parents=True)
    path = target / "manifest.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


COMPLETE = {
    "nodes": {
        "model.dqs.stg_a": {
            "resource_type": "model",
            "compiled_code": "select 1 as a",
        }
    }
}

AGON = {"nodes": {}}


def test_missing_manifest_is_stale(tmp_path: Path) -> None:
    assert is_stale(tmp_path / "target" / "manifest.json", tmp_path)


def test_unparseable_manifest_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{ torn", encoding="utf-8")
    assert is_stale(path, tmp_path)


def test_empty_manifest_is_stale(tmp_path: Path) -> None:
    path = _project(tmp_path, AGON)
    assert is_stale(path, tmp_path.parent or tmp_path)


def test_complete_manifest_newer_than_sources_is_fresh(tmp_path: Path) -> None:
    path = _project(tmp_path, COMPLETE)
    assert not is_stale(path, tmp_path)


def test_manifest_older_than_model_sources_is_stale(tmp_path: Path) -> None:
    path = _project(tmp_path, COMPLETE)
    # rewrite a model file so its mtime beats the manifest's (explicit times;
    # filesystem mtime granularity can otherwise swallow the ordering)
    model_sql = tmp_path / "models" / "model.sql"
    model_sql.write_text("select 2", encoding="utf-8")
    import os

    os.utime(path, (1, 1))
    os.utime(model_sql, (2, 2))
    assert is_stale(path, tmp_path)


def test_model_without_compiled_code_is_stale(tmp_path: Path) -> None:
    torn = {
        "nodes": {
            "model.dqs.stg_a": {"resource_type": "model", "compiled_code": "select 1"},
            "model.dqs.fct_b": {"resource_type": "model"},  # skipped by a failed build
        }
    }
    path = _project(tmp_path, torn)
    assert is_stale(path, tmp_path)
