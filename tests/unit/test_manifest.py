"""Unit tests for manifest loading/staleness rules (DECISIONS 57).

No dbt involved: hand-written manifests on a tmp project dir.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from dqs.errors import ManifestRegenerationError
from dqs.lineage import manifest as manifest_mod
from dqs.lineage.manifest import dbt_error_excerpt, is_stale


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


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["dbt", "compile"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _fake_dbt(monkeypatch: pytest.MonkeyPatch, results: list[subprocess.CompletedProcess[str]]):
    """Replace the dbt child with a scripted stream of CompletedProcess results."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return results.pop(0)

    monkeypatch.setattr(manifest_mod, "dbt_executable", lambda: Path("fake-dbt"))
    monkeypatch.setattr(manifest_mod.subprocess, "run", fake_run)
    return calls


def test_dbt_error_excerpt_keeps_the_tail_only() -> None:
    output = "\n".join([f"header {index}" for index in range(50)] + ["the real error", ""])
    excerpt = dbt_error_excerpt(output)
    assert excerpt.splitlines()[-1] == "the real error"
    assert len(excerpt.splitlines()) == 30
    assert "header 20" not in excerpt
    assert "header 21" in excerpt


def test_regenerate_reports_the_last_30_lines_and_exit_code_from_a_fake_dbt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Dev run 3: a crashed dbt child printed a long header and no error text.

    The retry gets the same scripted failure; the exception must carry both the
    exit code and the tail (where a normal dbt error would be), and the retry
    must be logged.
    """
    long_header = "\n".join(f"Running with dbt=1.12.6 step {index}" for index in range(60))
    output = f"{long_header}\nCompilation Error: stg_orders.sql is broken\n"
    calls = _fake_dbt(monkeypatch, [_completed(1, stdout=output), _completed(1, stdout=output)])

    with caplog.at_level(logging.WARNING), pytest.raises(ManifestRegenerationError) as excinfo:
        manifest_mod.regenerate(tmp_path)

    message = str(excinfo.value)
    assert len(calls) == 2  # one retry, not an endless loop
    assert "exit code 1" in message
    assert "after 2 attempts" in message
    assert "Compilation Error: stg_orders.sql is broken" in message
    assert "Running with dbt=1.12.6 step 59" in message
    assert "Running with dbt=1.12.6 step 30" not in message  # tail is 30 lines
    assert "retrying once" in caplog.text


def test_regenerate_succeeds_on_the_single_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _fake_dbt(
        monkeypatch,
        [_completed(1, stdout="transient failure"), _completed(0, stdout="Done.")],
    )
    with caplog.at_level(logging.WARNING):
        manifest_mod.regenerate(tmp_path)
    assert len(calls) == 2
    assert "retrying once" in caplog.text


def test_regenerate_does_not_retry_a_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_dbt(monkeypatch, [_completed(0, stdout="Done.")])
    manifest_mod.regenerate(tmp_path)
    assert len(calls) == 1
