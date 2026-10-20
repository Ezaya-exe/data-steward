"""Real dbt sandbox acceptance for Phase 5a (no LLM anywhere).

The module seeds one raw 30-day baseline template, builds staging/marts once,
then clones it per test. Faults are loaded with `run_dbt=False` so the clone
looks exactly like an on-call engineer's warehouse at the moment a fix is
proposed: raw has the bad batch, dev staging/marts are still the previous
snapshot.

Each verification runs dbt in a temp project against the clone, writes only
`dqs_sandbox_*` schemas, and drops them in `finally`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from tests.helpers import oracle

from dqs import db as dbmod
from dqs import dbt_run
from dqs.config import Settings
from dqs.models.enums import RootCauseCategory
from dqs.models.fix import Edit, FixPlan
from dqs.sim import pipeline
from dqs.sim.faults import parse_fault_specs
from dqs.verify import patch, sandbox

pytestmark = pytest.mark.integration

TEMPLATE_DB = "dqs_verify_template"
SEED = 42
BASELINE_DAYS = 30
FAULT_DATE = dt.date(2026, 1, 31)
PROJECT = Path(__file__).resolve().parents[2] / "warehouse" / "dbt_project"
MANIFEST = PROJECT / "target" / "manifest.json"

F1_SPEC = ["column_rename:orders.customer_id:new_name=cust_id"]
F3_SPEC = ["type_change:orders.total_amount"]
F6_SPEC = ["duplicate_rows:orders:fraction=0.05"]

F1_OLD = "    customer_id::integer          as customer_id,"
F1_GOOD = "    coalesce(customer_id::integer, cust_id::integer) as customer_id,"
F3_OLD = "    total_amount::numeric(12, 2)  as total_amount,"
F3_GOOD = "    regexp_replace(total_amount, '[^0-9.-]', '', 'g')::numeric(12, 2) as total_amount,"
F6_OLD = "from {{ source('raw', 'orders') }}"
F6_GOOD = """from (
    select *,
           row_number() over (
               partition by order_id order by _loaded_at desc, _batch_id desc
           ) as _rn
    from {{ source('raw', 'orders') }}
) ranked
where _rn = 1"""


def _admin(settings: Settings) -> Engine:
    return sa.create_engine(
        settings.rw_database_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )


@pytest.fixture(scope="module")
def template(settings: Settings) -> Iterator[Settings]:
    """Seed raw once, then build staging/marts once (the dev-layer baseline)."""
    admin = _admin(settings)
    try:
        try:
            with admin.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            pytest.skip(f"Postgres not reachable: {exc}")
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{TEMPLATE_DB}"')
        template_settings = settings.model_copy(update={"pg_database": TEMPLATE_DB})
        dbmod.init_database(template_settings)
        pipeline.seed_baseline(template_settings, BASELINE_DAYS, SEED, run_dbt=False)
        code, output = dbt_run.run_dbt_build(template_settings)
        assert code == 0, output[-2000:]
        yield template_settings
    finally:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{TEMPLATE_DB}" WITH (FORCE)')
        admin.dispose()


@pytest.fixture
def clone(settings: Settings, template: Settings) -> Iterator[tuple[Settings, Engine]]:
    name = f"dqs_verify_{uuid.uuid4().hex[:8]}"
    admin = _admin(settings)
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE "{TEMPLATE_DB}"')
        clone_settings = settings.model_copy(update={"pg_database": name})
        engine = dbmod.make_rw_engine(clone_settings)
        try:
            yield clone_settings, engine
        finally:
            engine.dispose()
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


def _plan(
    *,
    category: RootCauseCategory,
    columns: list[str],
    old: str,
    new: str,
    path: str = "models/staging/stg_orders.sql",
    affected_table: str = "raw.orders",
) -> FixPlan:
    return FixPlan(
        incident_id=uuid.uuid4(),
        batch_id="batch_20260131",
        category=category,
        affected_table=affected_table,
        affected_columns=columns,
        summary="test patch",
        edits=[Edit(path=path, old=old, new=new)],
        rationale="acceptance test",
        expected_effect="the targeted check clears",
        risk="test only",
    )


def _load_fault(settings: Settings, specs: list[str]) -> None:
    pipeline.load_day(
        settings,
        FAULT_DATE,
        SEED,
        run_dbt=False,
        specs=parse_fault_specs(specs),
    )


def _sandbox_schemas(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [
            str(row[0])
            for row in conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'dqs_sandbox_%' ORDER BY 1"
                )
            ).fetchall()
        ]


def _dev_counts(engine: Engine) -> dict[str, int]:
    counts: dict[str, int] = {}
    for schema in ("staging", "marts"):
        with engine.connect() as conn:
            names = [
                str(row[0])
                for row in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = :s AND table_type = 'BASE TABLE'"
                    ),
                    {"s": schema},
                ).fetchall()
            ]
            for name in names:
                counts[f"{schema}.{name}"] = int(
                    conn.execute(text(f'SELECT count(*) FROM "{schema}"."{name}"')).scalar_one()
                )
    return counts


def _assert_no_sandbox(engine: Engine) -> None:
    assert _sandbox_schemas(engine) == []


# --- known-good patches ---------------------------------------------------------------


GOOD_CASES = [
    pytest.param(
        F1_SPEC,
        RootCauseCategory.SOURCE_COLUMN_RENAMED,
        ["customer_id"],
        F1_OLD,
        F1_GOOD,
        id="F1_rename",
    ),
    pytest.param(
        F3_SPEC,
        RootCauseCategory.SOURCE_TYPE_CHANGED,
        ["total_amount"],
        F3_OLD,
        F3_GOOD,
        id="F3_type",
    ),
    pytest.param(
        F6_SPEC,
        RootCauseCategory.DUPLICATE_RECORDS,
        ["order_id"],
        F6_OLD,
        F6_GOOD,
        id="F6_duplicate",
    ),
]


@pytest.mark.parametrize(("specs", "category", "columns", "old", "new"), GOOD_CASES)
def test_hand_written_good_patch_passes_and_cleans_up(
    clone: tuple[Settings, Engine],
    specs: list[str],
    category: RootCauseCategory,
    columns: list[str],
    old: str,
    new: str,
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, specs)
    plan = _plan(category=category, columns=columns, old=old, new=new)

    result = sandbox.verify_fix(clone_settings, plan, engine=engine)

    assert result.passed, (result.failed_criteria, result.criteria)
    assert result.failed_criteria == []
    assert all(criterion.passed for criterion in result.criteria)
    assert result.dbt_exit_code == 0
    assert result.duration_s > 0
    with engine.connect() as conn:
        attempts = conn.execute(
            text(
                "SELECT attempt_no, passed FROM dq.fix_attempts "
                "WHERE incident_id = :i ORDER BY attempt_no"
            ),
            {"i": plan.incident_id},
        ).fetchall()
    assert [(row[0], row[1]) for row in attempts] == [(1, True)]
    _assert_no_sandbox(engine)


# --- clean-data oracle (Phase 5b) -------------------------------------------------------


@pytest.mark.parametrize(("specs", "category", "columns", "old", "new"), GOOD_CASES)
def test_oracle_confirms_a_good_patch_matches_clean_generated_data(
    clone: tuple[Settings, Engine],
    specs: list[str],
    category: RootCauseCategory,
    columns: list[str],
    old: str,
    new: str,
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, specs)
    plan = _plan(category=category, columns=columns, old=old, new=new)

    comparison = oracle.compare_patched_staging(
        clone_settings,
        plan,
        engine=engine,
        fault_date=FAULT_DATE,
        seed=SEED,
    )

    assert comparison.build_passed, comparison.detail
    assert comparison.passed, comparison
    assert comparison.rows_actual == comparison.rows_expected
    assert comparison.missing_keys == []
    assert comparison.unexpected_keys == []
    assert comparison.mismatched_cells == []
    _assert_no_sandbox(engine)


def test_oracle_catches_a_verifier_pass_that_writes_wrong_values(
    clone: tuple[Settings, Engine],
) -> None:
    """The verifier checks nulls/rows/contract; only the oracle sees wrong values."""
    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old=F1_OLD,
        new="    coalesce(customer_id::integer, 1) as customer_id,",
    )

    verdict = sandbox.verify_fix(clone_settings, plan, engine=engine)
    assert verdict.passed, (verdict.failed_criteria, verdict.criteria)

    comparison = oracle.compare_patched_staging(
        clone_settings,
        plan,
        engine=engine,
        fault_date=FAULT_DATE,
        seed=SEED,
    )
    assert comparison.build_passed, comparison.detail
    assert comparison.passed is False
    assert comparison.mismatched_cells
    assert any("customer_id" in cell for cell in comparison.mismatched_cells)
    _assert_no_sandbox(engine)


# --- known-bad patches ----------------------------------------------------------------


def test_bad_patch_that_deletes_rows_fails_row_preservation(
    clone: tuple[Settings, Engine],
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F3_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_TYPE_CHANGED,
        columns=["total_amount"],
        old=F6_OLD,
        new=F6_OLD + "\nwhere false",
    )
    result = sandbox.verify_fix(clone_settings, plan, engine=engine)
    assert result.passed is False
    assert "row_preservation" in result.failed_criteria
    detail = {c.name: c.detail for c in result.criteria}
    assert "0 staging rows" in detail["row_preservation"]
    _assert_no_sandbox(engine)


def test_bad_patch_that_renames_an_output_column_fails_contract_conformance(
    clone: tuple[Settings, Engine],
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old=F1_OLD,
        new="    customer_id::integer          as customer_key,",
    )
    result = sandbox.verify_fix(clone_settings, plan, engine=engine)
    assert result.passed is False
    assert "contract_conformance" in result.failed_criteria
    detail = {c.name: c.detail for c in result.criteria}
    assert "customer_id" in detail["contract_conformance"]
    assert "customer_key" in detail["contract_conformance"]
    _assert_no_sandbox(engine)


def test_bad_patch_with_a_syntax_error_fails_dbt_build_with_an_excerpt(
    clone: tuple[Settings, Engine],
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old="select",
        new="seleckt",
    )
    result = sandbox.verify_fix(clone_settings, plan, engine=engine)
    assert result.passed is False
    assert "dbt_build" in result.failed_criteria
    assert {
        "contract_conformance",
        "row_preservation",
        "targeted_check",
    } <= set(result.failed_criteria)
    assert result.dbt_exit_code not in (None, 0)
    assert result.dbt_log_excerpt
    assert "Error" in result.dbt_log_excerpt or "error" in result.dbt_log_excerpt
    detail = {c.name: c.detail for c in result.criteria}
    assert "no staging table" in detail["targeted_check"]
    _assert_no_sandbox(engine)


def test_bad_filter_that_hides_bad_rows_fails_the_targeted_check(
    clone: tuple[Settings, Engine],
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F3_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_TYPE_CHANGED,
        columns=["total_amount"],
        old=F6_OLD,
        new=F6_OLD + "\nwhere total_amount ~ '^[0-9]+([.][0-9]+)?$'",
    )
    result = sandbox.verify_fix(clone_settings, plan, engine=engine)
    assert result.passed is False
    assert "targeted_check" in result.failed_criteria
    detail = {c.name: c.detail for c in result.criteria}
    assert "no rows" in detail["targeted_check"]
    _assert_no_sandbox(engine)


@pytest.mark.parametrize(
    ("path", "old", "new", "criterion", "message"),
    [
        ("models/staging/schema.yml", "models:", "models: #", "allowlist", ".sql"),
        ("models/../macros/x.sql", "a", "b", "allowlist", "traversal"),
        ("macros/x.sql", "a", "b", "allowlist", "under warehouse"),
        ("models/staging/stg_products.sql", "price", "price::numeric", "relevance", "unrelated"),
        ("models/staging/stg_orders.sql", "NO_SUCH_TEXT", "x", "exact_match", "found 0"),
        ("models/staging/stg_orders.sql", "order_id", "order_id_x", "exact_match", "found"),
    ],
)
def test_bad_paths_relevance_and_exact_match_are_rejected_before_dbt(
    clone: tuple[Settings, Engine],
    path: str,
    old: str,
    new: str,
    criterion: str,
    message: str,
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old=old,
        new=new,
        path=path,
    )
    result = sandbox.verify_fix(clone_settings, plan, engine=engine, persist=False)
    assert result.passed is False
    assert criterion in result.failed_criteria
    detail = {c.name: c.detail for c in result.criteria}
    assert message in detail[criterion]
    assert result.dbt_exit_code is None  # dbt never ran
    _assert_no_sandbox(engine)


def test_oversized_patch_is_rejected_by_the_model() -> None:
    with pytest.raises(ValidationError, match="plan limit"):
        _plan(
            category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
            columns=["customer_id"],
            old=F1_OLD,
            new="x" * 5000,
        )


def test_cleanup_happens_when_the_build_is_killed(
    clone: tuple[Settings, Engine],
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old=F1_OLD,
        new=F1_GOOD,
    )
    schema = patch.sandbox_schema_name(plan)

    def killed(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        # Simulate a run that created its schema and then hung until the timeout.
        with engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}_staging"')
        raise subprocess.TimeoutExpired(cmd, 1)

    result = sandbox.verify_fix(
        clone_settings,
        plan,
        engine=engine,
        runner=killed,
        timeout_s=1,
        persist=False,
    )
    assert result.passed is False
    assert "dbt_build" in result.failed_criteria
    _assert_no_sandbox(engine)


# --- isolation guarantees -------------------------------------------------------------


def test_verification_does_not_touch_the_real_project_or_dev_layers(
    clone: tuple[Settings, Engine],
) -> None:
    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)

    manifest_before = (
        hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.exists() else None
    )
    counts_before = _dev_counts(engine)

    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old=F1_OLD,
        new=F1_GOOD,
    )
    result = sandbox.verify_fix(clone_settings, plan, engine=engine)
    assert result.passed, (result.failed_criteria, result.criteria)

    manifest_after = (
        hashlib.sha256(MANIFEST.read_bytes()).hexdigest() if MANIFEST.exists() else None
    )
    assert manifest_after == manifest_before
    assert _dev_counts(engine) == counts_before
    _assert_no_sandbox(engine)


def test_cleanup_command_drops_a_stale_sandbox_schema(
    clone: tuple[Settings, Engine],
) -> None:
    _, engine = clone
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS "dqs_sandbox_deadbeef_staging"')
    assert _sandbox_schemas(engine) == ["dqs_sandbox_deadbeef_staging"]
    dropped = sandbox.cleanup_stale_sandboxes(engine)
    assert dropped == ["dqs_sandbox_deadbeef_staging"]
    _assert_no_sandbox(engine)


# --- CLI --------------------------------------------------------------------------------


def test_cli_verify_and_cleanup(
    clone: tuple[Settings, Engine], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from dqs.cli import app as dqs_app

    clone_settings, engine = clone
    _load_fault(clone_settings, F1_SPEC)
    plan = _plan(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        columns=["customer_id"],
        old=F1_OLD,
        new=F1_GOOD,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")

    monkeypatch.setattr("dqs.cli.load_settings", lambda: clone_settings)
    result = CliRunner().invoke(dqs_app, ["verify", str(plan_path)])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    _assert_no_sandbox(engine)

    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE SCHEMA IF NOT EXISTS "dqs_sandbox_cafebabe_staging"')
    cleanup = CliRunner().invoke(dqs_app, ["verify", "cleanup"])
    assert cleanup.exit_code == 0, cleanup.output
    assert "dqs_sandbox_cafebabe_staging" in cleanup.output
    _assert_no_sandbox(engine)


def test_verify_cli_rejects_an_invalid_plan(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from dqs.cli import app as dqs_app

    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"summary": "missing everything"}), encoding="utf-8")
    result = CliRunner().invoke(dqs_app, ["verify", str(path)])
    assert result.exit_code != 0
    assert "invalid fix plan" in result.output
