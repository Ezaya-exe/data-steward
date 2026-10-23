"""Unit tests for the cached-baseline reset (Phase 8a).

These never touch Postgres: the hash invalidation rule, the database-name
guardrails and the demo's clone path are pure decisions. The end-to-end timing
and byte-identical proof live in `tests/integration/test_fast_reset.py`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from dqs import fast_reset
from dqs.config import Settings
from dqs.errors import DatabaseError

SETTINGS = Settings(_env_file=None)


def _tree(root: Path) -> dict[str, Path]:
    """A miniature repo layout with every file the hash must cover."""
    generator = root / "src" / "dqs" / "sim" / "generator.py"
    contracts = root / "contracts"
    models = root / "warehouse" / "dbt_project" / "models"
    macros = root / "warehouse" / "dbt_project" / "macros"
    project = root / "warehouse" / "dbt_project" / "dbt_project.yml"
    generator.parent.mkdir(parents=True)
    contracts.mkdir(parents=True)
    (models / "staging").mkdir(parents=True)
    macros.mkdir(parents=True)
    generator.write_text("print('gen')\n", encoding="utf-8")
    (contracts / "orders.yml").write_text("table: orders\n", encoding="utf-8")
    (models / "staging" / "stg_orders.sql").write_text("select 1\n", encoding="utf-8")
    (macros / "cents.sql").write_text("{% macro cents() %}{% endmacro %}\n", encoding="utf-8")
    project.write_text("name: warehouse\n", encoding="utf-8")
    return {
        "GENERATOR_PATH": generator,
        "CONTRACTS_DIR": contracts,
        "DBT_MODELS_DIR": models,
        "DBT_MACROS_DIR": macros,
        "DBT_PROJECT_FILE": project,
        "REPO_ROOT": root,
    }


@pytest.fixture
def mini_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    tree = _tree(tmp_path / "repo")
    for name, value in tree.items():
        monkeypatch.setattr(fast_reset, name, value)
    return tree


def test_content_hash_is_stable_and_changes_with_any_covered_file(
    mini_repo: dict[str, Path],
) -> None:
    first = fast_reset.content_hash()
    assert first == fast_reset.content_hash()
    assert len(first) == 64

    (mini_repo["DBT_MODELS_DIR"] / "staging" / "stg_orders.sql").write_text(
        "select 2\n", encoding="utf-8"
    )
    assert fast_reset.content_hash() != first

    second = fast_reset.content_hash()
    (mini_repo["CONTRACTS_DIR"] / "orders.yml").write_text("table: customers\n", encoding="utf-8")
    assert fast_reset.content_hash() != second


def test_content_hash_sees_a_renamed_model(mini_repo: dict[str, Path]) -> None:
    before = fast_reset.content_hash()
    source = mini_repo["DBT_MODELS_DIR"] / "staging" / "stg_orders.sql"
    source.rename(source.with_name("stg_orders_renamed.sql"))
    assert fast_reset.content_hash() != before


@pytest.mark.parametrize(
    "name",
    ["warehouse", "postgres", "template0", "template1", "Dqs_demo", "public"],
)
def test_unsafe_database_names_are_refused(name: str) -> None:
    with pytest.raises(DatabaseError):
        fast_reset.drop_database(SETTINGS, name)


def test_template_is_protected_unless_a_rebuild_explicitly_allows_it() -> None:
    with pytest.raises(DatabaseError, match="protected"):
        fast_reset._validate_database_name(fast_reset.TEMPLATE_DB)
    fast_reset._validate_database_name(fast_reset.TEMPLATE_DB, allow_template=True)


def test_only_the_project_prefix_is_an_allowed_target() -> None:
    fast_reset._validate_database_name("dqs_demo")
    fast_reset._validate_database_name("dqs_bench_20260101_abcdef")
    with pytest.raises(DatabaseError, match="must match"):
        fast_reset._validate_database_name("warehouse_backup")


def test_clone_refuses_same_source_and_target() -> None:
    with pytest.raises(DatabaseError, match="must differ"):
        fast_reset.clone_database(SETTINGS, "dqs_same", "dqs_same")


def test_clone_refuses_the_warehouse_as_a_target() -> None:
    with pytest.raises(DatabaseError, match="protected|must match"):
        fast_reset.clone_database(SETTINGS, "dqs_template", "warehouse")


def test_demo_always_clones_the_cached_template(monkeypatch: pytest.MonkeyPatch) -> None:
    from dqs import demo

    calls: list[str] = []

    def fake_clone(settings: Settings, target: str) -> None:
        _ = settings
        calls.append(target)

    monkeypatch.setattr(demo.fast_reset, "clone_template", fake_clone)
    assert demo.ensure_demo_database(SETTINGS) is True
    assert demo.ensure_demo_database(SETTINGS, reset=True) is True
    # The demo never reuses a dirty database: each call is a fresh clone.
    # clone_template's source is fast_reset.TEMPLATE_DB, never the configured
    # warehouse (Settings only supplies the admin connection).
    assert calls == [demo.DEMO_DB, demo.DEMO_DB]


def test_seed_baseline_loads_every_day_then_builds_dbt_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 8a: 30 loads + ONE build, not 30 build/load cycles (DECISIONS 120)."""
    from dqs.sim import pipeline

    loaded: list[object] = []
    builds: list[dict[str, object]] = []

    monkeypatch.setattr(
        pipeline.dbmod, "make_rw_engine", lambda _settings: SimpleNamespace(dispose=lambda: None)
    )
    monkeypatch.setattr(
        pipeline.generator,
        "tables_for_date",
        lambda business_date, _seed: {"orders": business_date},
    )

    def fake_load(engine: object, business_date: object, batch: object) -> dict[str, int]:
        _ = engine, batch
        loaded.append(business_date)
        return {"orders": 1}

    def fake_build(_settings: Settings, **kwargs: object) -> tuple[int, str]:
        builds.append(kwargs)
        return 0, "ok"

    monkeypatch.setattr(pipeline.loader, "load_batch", fake_load)
    monkeypatch.setattr(pipeline.dbt_run, "run_dbt_build", fake_build)

    results = pipeline.seed_baseline(SETTINGS, 3, 42, run_dbt=True)

    assert len(loaded) == 3
    assert len(builds) == 1
    assert builds[0]["batch_id"] == pipeline.loader.batch_id_for_date(
        pipeline.BASELINE_START + dt.timedelta(days=2)
    )
    assert [result["dbt_exit"] for result in results] == [None, None, 0]
