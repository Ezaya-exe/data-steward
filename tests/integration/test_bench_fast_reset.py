"""End-to-end fast-reset and fake-LLM benchmark tests (Phase 8a).

Requires Docker Postgres (skipped otherwise). The checksum constants below were
captured from the pre-refactor baseline (docs/DECISIONS.md); a mismatch means
the bulk-COPY/one-build reset no longer reproduces the same warehouse bytes.
Every test database uses the `dqs_bench_it_` prefix and is dropped afterwards.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, text

from dqs import db as dbmod
from dqs import fast_reset
from dqs.agents.llm import BudgetTracker, FakeChatModel, LLMClient
from dqs.bench import runner
from dqs.config import Settings
from dqs.errors import DatabaseError

pytestmark = pytest.mark.integration

# Pre-refactor baseline (see docs/DECISIONS.md, Phase 8a).
BASELINE_CHECKSUMS = {
    "raw.customers": "22781d264caf7c6b2fb5e5cf427d4912",
    "raw.products": "fbdd1383f8e20e336c341bf86e61b79a",
    "raw.orders": "1f4abad9be03cc7cab7d9af87f854a3b",
    "raw.order_items": "0a972794e4aa53f1d12283e596708b5e",
    "raw.payments": "b40cc4223918972b22acef2d3b2d5c68",
    "dq.batches": "c7374560250fa01cebd89de46e83a0e9",
    "dq.batch_tables": "ccf230d7cffbc56229b12c394b91571a",
}
BASELINE_SET_HASH = "2e3a468c308189aaf220ff23b024e9c3b04e841b6030ca2a1e5706d4984a08d9"
CHECKSUM_TABLES = tuple(BASELINE_CHECKSUMS)


def _admin(settings: Settings) -> Engine:
    return sa.create_engine(
        settings.rw_database_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )


def _database_exists(settings: Settings, name: str) -> bool:
    admin = _admin(settings)
    try:
        with admin.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                ).scalar()
            )
    finally:
        admin.dispose()


def _checksums(engine: Engine, tables: tuple[str, ...] = CHECKSUM_TABLES) -> dict[str, str]:
    out: dict[str, str] = {}
    with engine.connect() as conn:
        for table in tables:
            value = conn.execute(
                text(
                    f"SELECT md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) "
                    f"FROM {table} t"
                )
            ).scalar_one()
            out[table] = str(value)
    return out


def _database_checksums(settings: Settings, database: str) -> dict[str, str]:
    db_settings = settings.model_copy(update={"pg_database": database})
    engine = dbmod.make_rw_engine(db_settings)
    try:
        return _checksums(engine)
    finally:
        engine.dispose()


def _set_hash(checksums: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(checksums, sort_keys=True).encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture(scope="module")
def test_names() -> dict[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return {
        "template": f"dqs_bench_it_template_{suffix}",
        "clone": f"dqs_bench_it_clone_{suffix}",
        "results": f"dqs_bench_it_results_{suffix}",
    }


@pytest.fixture(scope="module", autouse=True)
def cleanup_and_warehouse_guard(settings: Settings, test_names: dict[str, str]) -> Iterator[None]:
    """Never leave test DBs behind, and prove the warehouse was untouched."""
    before = _warehouse_checksums(settings)
    yield
    leftovers = fast_reset.list_steward_databases(settings, prefix="dqs_bench_")
    for name in leftovers:
        fast_reset.drop_database(settings, name)
    assert fast_reset.list_steward_databases(settings, prefix="dqs_bench_") == []
    if before is not None:
        assert _warehouse_checksums(settings) == before, "the warehouse was modified by a test"


def _warehouse_checksums(settings: Settings) -> dict[str, str] | None:
    engine = dbmod.make_rw_engine(settings)
    try:
        with engine.connect() as conn:
            existing = {
                str(row[0])
                for row in conn.execute(
                    text(
                        "SELECT table_schema || '.' || table_name FROM information_schema.tables "
                        "WHERE table_schema IN ('raw', 'dq')"
                    )
                )
            }
        tables = tuple(table for table in CHECKSUM_TABLES if table in existing)
        return _checksums(engine, tables) if tables else None
    except sa.exc.SQLAlchemyError:
        return None
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def template(
    settings: Settings,
    test_names: dict[str, str],
) -> Iterator[fast_reset.TemplateStatus]:
    """Build the real template once; this is the timing/identity evidence."""
    original = fast_reset.TEMPLATE_DB
    fast_reset.TEMPLATE_DB = test_names["template"]
    messages: list[str] = []
    status = fast_reset.ensure_template(settings, force=True, progress=messages.append)
    try:
        yield status
    finally:
        fast_reset.drop_database(settings, test_names["template"])
        fast_reset.TEMPLATE_DB = original


def test_template_is_byte_identical_to_the_captured_baseline(
    settings: Settings,
    test_names: dict[str, str],
    template: fast_reset.TemplateStatus,
) -> None:
    assert template.created is True
    assert template.baseline_days == fast_reset.BASELINE_DAYS
    assert template.seed == fast_reset.SEED
    assert template.content_hash == fast_reset.content_hash()
    assert template.row_counts and all(count > 0 for count in template.row_counts.values())
    assert template.build_seconds >= template.load_seconds + template.dbt_seconds

    checksums = _database_checksums(settings, test_names["template"])
    assert checksums == BASELINE_CHECKSUMS
    assert _set_hash(checksums) == BASELINE_SET_HASH

    meta = fast_reset._template_meta(
        settings.model_copy(update={"pg_database": test_names["template"]})
    )
    assert meta is not None
    assert meta["content_hash"] == fast_reset.content_hash()
    assert meta["seed"] == fast_reset.SEED
    assert meta["baseline_days"] == fast_reset.BASELINE_DAYS
    assert float(meta["load_seconds"]) >= 0 and float(meta["dbt_seconds"]) >= 0
    print(
        f"template timing: total={template.build_seconds:.2f}s "
        f"load={template.load_seconds:.2f}s dbt={template.dbt_seconds:.2f}s "
        f"rows={template.row_counts}"
    )


def test_clone_is_an_exact_copy_of_the_template(
    settings: Settings,
    test_names: dict[str, str],
    template: fast_reset.TemplateStatus,
) -> None:
    fast_reset.clone_database(settings, test_names["template"], test_names["clone"])
    try:
        assert _database_checksums(settings, test_names["clone"]) == BASELINE_CHECKSUMS
        assert _database_exists(settings, test_names["clone"])
    finally:
        fast_reset.drop_database(settings, test_names["clone"])
    assert not _database_exists(settings, test_names["clone"])


def test_reused_template_is_not_rebuilt(
    settings: Settings,
    template: fast_reset.TemplateStatus,
) -> None:
    again = fast_reset.ensure_template(settings)
    assert again.created is False
    assert again.built_at == template.built_at
    assert again.content_hash == template.content_hash


def test_changed_content_hash_forces_a_rebuild(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    template: fast_reset.TemplateStatus,
) -> None:
    monkeypatch.setattr(fast_reset, "content_hash", lambda: "f" * 64)
    rebuilt = fast_reset.ensure_template(settings)
    assert rebuilt.created is True
    assert rebuilt.content_hash == "f" * 64
    assert rebuilt.built_at >= template.built_at
    assert _database_checksums(settings, fast_reset.TEMPLATE_DB) == BASELINE_CHECKSUMS


def test_warehouse_is_never_a_clone_source_or_target(
    settings: Settings,
    template: fast_reset.TemplateStatus,
) -> None:
    _ = template  # keeps the test template name patched and alive
    with pytest.raises(DatabaseError):
        fast_reset.clone_database(settings, "warehouse", "dqs_bench_it_forbidden")
    with pytest.raises(DatabaseError):
        fast_reset.clone_database(settings, "dqs_template", "warehouse")
    with pytest.raises(DatabaseError):
        fast_reset.drop_database(settings, "warehouse")


def _fake_client_factory(
    settings: Settings, model: str, provider: str, max_wait: float
) -> LLMClient:
    """A deterministic fake model; wiring only, never a benchmark number."""
    return LLMClient(
        FakeChatModel(),
        provider="fake",
        model=model,
        budget=BudgetTracker.from_settings(settings),
        max_retries=0,
        max_retry_wait_seconds=max_wait,
        secret_values=[],
    )


def test_three_fake_llm_scenarios_run_end_to_end_and_leave_no_databases(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runner, "CHECKPOINT_PATH", tmp_path / "checkpoints.sqlite")
    ids = ["f1-orders-status", "f4-orders-status-20", "f6-orders-03"]
    options = runner.BenchOptions(
        model="fake",
        split="dev",
        ids=ids,
        results_dir=tmp_path,
        pause_seconds=0.0,
    )
    progress: list[str] = []
    run, path = runner.run_benchmark(
        settings,
        options,
        progress=progress.append,
        client_factory=_fake_client_factory,
        sleep=lambda _seconds: None,
    )

    assert [result.scenario_id for result in run.scenarios] == ids
    assert all(result.error is None for result in run.scenarios), [
        (result.scenario_id, result.error) for result in run.scenarios
    ]
    assert all(result.tokens_in > 0 and result.tokens_out > 0 for result in run.scenarios)
    assert all(result.detected is True for result in run.scenarios)
    assert {result.kind for result in run.scenarios} == {
        "column_rename",
        "null_spike",
        "duplicate_rows",
    }
    assert run.metadata.model == "fake"
    assert run.metadata.prompt_hashes["investigator"]
    assert run.metadata.stop_reason is None
    assert path.exists() and path.parent == tmp_path
    assert (tmp_path / "latest.md").read_text(encoding="utf-8").startswith("# Benchmark")
    assert run.metadata.tokens_total == sum(
        result.tokens_in + result.tokens_out for result in run.scenarios
    )
    print(
        "scenario wall_s: "
        + ", ".join(f"{result.scenario_id}={result.wall_s:.1f}s" for result in run.scenarios)
        + f"; total={run.metadata.wall_s_total:.1f}s"
    )

    # Every scenario database was dropped by the runner itself; only the
    # module-scoped test template remains until its fixture tears down.
    leftovers = fast_reset.list_steward_databases(settings, prefix="dqs_bench_")
    assert leftovers == [fast_reset.TEMPLATE_DB]
    assert any("detected=" in line for line in progress)
