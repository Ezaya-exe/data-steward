"""Unit tests for the hardened fix-patch allowlist and exact-match application."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from dqs.errors import FixPlanError
from dqs.lineage.graph import LineageGraph
from dqs.models.enums import RootCauseCategory
from dqs.models.fix import Edit, FixPlan
from dqs.verify import patch

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manifest_fixture.json"
PROJECT = Path(__file__).resolve().parents[2] / "warehouse" / "dbt_project"


def _graph() -> LineageGraph:
    manifest = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return LineageGraph.from_manifest(manifest, project_dir=PROJECT)


def _plan(**overrides: object) -> FixPlan:
    data: dict[str, object] = {
        "incident_id": uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "batch_id": "batch_20260131",
        "category": RootCauseCategory.SOURCE_COLUMN_RENAMED,
        "affected_table": "raw.orders",
        "affected_columns": ["customer_id"],
        "summary": "restore customer_id",
        "edits": [Edit(path="models/staging/stg_orders.sql", old="customer_id", new="cust_id")],
        "rationale": "rename",
        "expected_effect": "no nulls",
        "risk": "low",
    }
    data.update(overrides)
    return FixPlan.model_validate(data)


# --- path normalization / allowlist ---------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "models/staging/stg_orders.sql",
        "models\\staging\\stg_orders.sql",
        "warehouse/dbt_project/models/staging/stg_orders.sql",
        "warehouse\\dbt_project\\models\\staging\\stg_orders.sql",
        "models/./staging/stg_orders.sql",
        "models/staging/stg_orders.SQL",
    ],
)
def test_accepts_project_relative_model_paths(raw: str) -> None:
    expected = (
        "models/staging/stg_orders.SQL" if raw.endswith(".SQL") else "models/staging/stg_orders.sql"
    )
    assert patch.normalize_edit_path(raw) == expected


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("models\\..\\macros\\x.sql", "traversal"),
        ("MODELS/../profiles.yml", "traversal"),
        ("models/staging/../../profiles.yml", "traversal"),
        (r"C:\repo\warehouse\dbt_project\models\staging\stg_orders.sql", "absolute"),
        (r"\\server\share\models\staging\x.sql", "absolute"),
        ("/etc/passwd", "absolute"),
        ("models\\staging\\x.sql:stream", "unsafe"),
        ("models/staging/schema.yml", "only .sql"),
        ("models/staging/CON.sql", "reserved"),
        ("models//staging/x.sql", "empty path segment"),
        ("models/staging/x.sql\x00", "NUL"),
        (" models/staging/x.sql", "whitespace"),
        ("staging/x.sql", "under warehouse"),
    ],
)
def test_rejects_traversal_absolute_and_non_sql_paths(raw: str, reason: str) -> None:
    with pytest.raises(FixPlanError, match=reason):
        patch.normalize_edit_path(raw)


def test_allowlist_requires_an_existing_file() -> None:
    edit = Edit(path="models/staging/does_not_exist.sql", old="a", new="b")
    with pytest.raises(FixPlanError, match="does not exist"):
        patch.validate_edit_paths([edit], PROJECT)


def test_allowlist_rejects_an_absolute_path_inside_models() -> None:
    absolute = str(PROJECT / "models" / "staging" / "stg_orders.sql")
    with pytest.raises(FixPlanError, match="absolute"):
        patch.normalize_edit_path(absolute)


def test_allowlist_rejects_symlinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "models" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside.sql"
    outside.write_text("secret", encoding="utf-8")
    link = project / "models" / "staging" / "link.sql"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this Windows session cannot create symlinks")

    with pytest.raises(FixPlanError, match="symlink"):
        patch.validate_edit_paths([Edit(path="models/staging/link.sql", old="a", new="b")], project)


def test_allowlist_rejects_windows_junctions(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("junctions are a Windows feature")
    project = tmp_path / "project"
    (project / "models" / "staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.sql").write_text("secret", encoding="utf-8")
    junction = project / "models" / "staging" / "jump"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.skip(f"cannot create a junction: {completed.stderr or completed.stdout}")

    with pytest.raises(FixPlanError, match="symlink"):
        patch.validate_edit_paths(
            [Edit(path="models/staging/jump/x.sql", old="a", new="b")], project
        )


def test_allowlist_rejects_a_symlink_even_when_it_resolves_inside_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is on the path component, not only on the resolved destination."""
    project = tmp_path / "project"
    target = project / "models" / "staging" / "real.sql"
    _write(target, "select 1")
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        return self.resolve() == target.resolve() or real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with pytest.raises(FixPlanError, match="symlink"):
        patch.validate_edit_paths([Edit(path="models/staging/real.sql", old="a", new="b")], project)


# --- relevance rule -------------------------------------------------------------------


def test_relevance_accepts_a_staging_model_directly_over_the_raw_table() -> None:
    graph = _graph()
    resolved = patch.validate_relevance(["models/staging/stg_orders.sql"], graph, "raw.orders")
    assert resolved == {"models/staging/stg_orders.sql": "model.dq_warehouse.stg_orders"}


def test_relevance_rejects_a_model_over_another_raw_table() -> None:
    with pytest.raises(FixPlanError, match="unrelated"):
        patch.validate_relevance(["models/staging/stg_products.sql"], _graph(), "raw.orders")


def test_relevance_rejects_a_marts_model() -> None:
    with pytest.raises(FixPlanError, match="not a staging model"):
        patch.validate_relevance(["models/marts/fct_orders.sql"], _graph(), "raw.orders")


def test_relevance_rejects_a_file_absent_from_the_manifest() -> None:
    with pytest.raises(FixPlanError, match="no dbt model"):
        patch.validate_relevance(["models/staging/not_a_model.sql"], _graph(), "raw.orders")


def test_relevance_rejects_an_unknown_raw_table() -> None:
    with pytest.raises(FixPlanError, match="cannot resolve"):
        patch.validate_relevance(["models/staging/stg_orders.sql"], _graph(), "raw.nope")


# --- exact-match application ----------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def test_exact_match_replaces_the_only_occurrence(tmp_path: Path) -> None:
    _write(tmp_path / "models" / "staging" / "m.sql", "a\nold\nb\n")
    touched = patch.apply_edits([Edit(path="models/staging/m.sql", old="old", new="new")], tmp_path)
    assert touched == ["models/staging/m.sql"]
    assert (tmp_path / "models" / "staging" / "m.sql").read_text(encoding="utf-8") == "a\nnew\nb\n"


def test_exact_match_rejects_zero_and_multiple_matches(tmp_path: Path) -> None:
    _write(tmp_path / "models" / "staging" / "m.sql", "old old\n")
    with pytest.raises(FixPlanError, match="found 0"):
        patch.apply_edits([Edit(path="models/staging/m.sql", old="absent", new="x")], tmp_path)
    with pytest.raises(FixPlanError, match="found 2"):
        patch.apply_edits([Edit(path="models/staging/m.sql", old="old", new="x")], tmp_path)


def test_apply_edits_is_sequential(tmp_path: Path) -> None:
    _write(tmp_path / "models" / "staging" / "m.sql", "a b\n")
    patch.apply_edits(
        [
            Edit(path="models/staging/m.sql", old="a", new="A"),
            Edit(path="models/staging/m.sql", old="b", new="B"),
        ],
        tmp_path,
    )
    assert (tmp_path / "models" / "staging" / "m.sql").read_text(encoding="utf-8") == "A B\n"


def test_apply_edits_preserves_crlf_outside_the_replacement(tmp_path: Path) -> None:
    path = tmp_path / "models" / "staging" / "m.sql"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first\r\nold\r\nlast\r\n")
    patch.apply_edits([Edit(path="models/staging/m.sql", old="old", new="new")], tmp_path)
    assert path.read_bytes() == b"first\r\nnew\r\nlast\r\n"


# --- temp copy and schema naming ------------------------------------------------------


def test_copy_project_excludes_build_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write(source / "models" / "staging" / "m.sql", "select 1")
    _write(source / "target" / "manifest.json", "{}")
    _write(source / "logs" / "dbt.log", "noise")
    _write(source / "dbt_packages" / "pkg" / "x.sql", "noise")
    _write(source / "profiles.yml", "dq_warehouse: {}")

    destination = tmp_path / "copy"
    patch.copy_project(source, destination)
    assert (destination / "models" / "staging" / "m.sql").exists()
    assert (destination / "profiles.yml").exists()
    assert not (destination / "target").exists()
    assert not (destination / "logs").exists()
    assert not (destination / "dbt_packages").exists()


def test_sandbox_schema_is_incident_derived_and_valid() -> None:
    assert patch.sandbox_schema_name(_plan()) == "dqs_sandbox_11111111"
    assert patch.is_sandbox_schema("dqs_sandbox_11111111")
    assert patch.is_sandbox_schema("dqs_sandbox_11111111_staging")
    assert not patch.is_sandbox_schema("staging")
    assert not patch.is_sandbox_schema("dqs_sandbox_")


def test_sandbox_schema_without_incident_is_deterministic_per_plan() -> None:
    plan = _plan(incident_id=None)
    first = patch.sandbox_schema_name(plan)
    assert first == patch.sandbox_schema_name(plan)
    assert patch.is_sandbox_schema(first)
