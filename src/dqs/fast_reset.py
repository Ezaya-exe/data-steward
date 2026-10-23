"""Fast, cached warehouse reset for the demo and the benchmark (Phase 8a).

The 30-day clean baseline is identical for every benchmark scenario, so building
it from scratch per scenario would dominate the benchmark's wall time. Instead
this module builds it **once** into `dqs_template` (bulk COPY + one full dbt
build), records a content hash with it, and every consumer clones that database
with `CREATE DATABASE ... TEMPLATE`.

The cache is invalidated automatically when the generator, the committed
contracts or the dbt models change: the hash covers exactly those files and is
stored inside the template (`dq.template_meta`). A mismatch rebuilds the
template; a consumer never has to remember to pass `--force`.

Safety: every database name is validated against `^dqs_[a-z0-9_]+$` and the
dev warehouse (`Settings.pg_database`), `warehouse`, `postgres` and the
built-in templates are refused. The warehouse is never read, written, dropped
or used as a clone source.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs import db as dbmod
from dqs import dbt_run
from dqs.config import Settings
from dqs.errors import DatabaseError
from dqs.sim import generator, loader

TEMPLATE_DB: Final = "dqs_template"
BENCH_DB_PREFIX: Final = "dqs_bench_"
DB_NAME_RE: Final = re.compile(r"^dqs_[a-z0-9_]+$")
PROTECTED_DATABASES: Final = frozenset(
    {"warehouse", "postgres", "template0", "template1", "dqs_template"}
)
BASELINE_DAYS: Final = 30
SEED: Final = 42

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
GENERATOR_PATH: Final = REPO_ROOT / "src" / "dqs" / "sim" / "generator.py"
CONTRACTS_DIR: Final = REPO_ROOT / "contracts"
DBT_PROJECT_DIR: Final = REPO_ROOT / "warehouse" / "dbt_project"
DBT_MODELS_DIR: Final = DBT_PROJECT_DIR / "models"
DBT_MACROS_DIR: Final = DBT_PROJECT_DIR / "macros"
DBT_PROJECT_FILE: Final = DBT_PROJECT_DIR / "dbt_project.yml"

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class TemplateStatus:
    """What `ensure_template` found or built (for reports and tests)."""

    database: str
    created: bool
    content_hash: str
    seed: int
    baseline_days: int
    built_at: dt.datetime
    build_seconds: float
    load_seconds: float
    dbt_seconds: float
    row_counts: dict[str, int]


def content_hash() -> str:
    """Hash the files whose change must invalidate the cached template.

    Covers the deterministic generator, every committed contract YAML, every
    dbt model file, the dbt project config and the project macros. Paths are
    hashed with their repository-relative POSIX name so a rename counts as a
    change too.
    """
    paths: list[Path] = [GENERATOR_PATH]
    paths.extend(sorted(CONTRACTS_DIR.glob("*.yml")))
    paths.extend(sorted(DBT_MODELS_DIR.rglob("*")))
    paths.extend(sorted(DBT_MACROS_DIR.rglob("*")))
    if DBT_PROJECT_FILE.exists():
        paths.append(DBT_PROJECT_FILE)
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _validate_database_name(name: str, *, allow_template: bool = False) -> None:
    if name in PROTECTED_DATABASES and not (allow_template and name == TEMPLATE_DB):
        raise DatabaseError(f"refusing to touch protected database {name!r}")
    if not DB_NAME_RE.fullmatch(name):
        raise DatabaseError(
            f"refusing database name {name!r}: must match {DB_NAME_RE.pattern} "
            "(the warehouse is never a valid target)"
        )


def admin_engine(settings: Settings) -> Engine:
    """AUTOCOMMIT connection to `postgres` for CREATE/DROP DATABASE."""
    return create_engine(
        settings.rw_database_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )


def _database_exists(admin: Engine, name: str) -> bool:
    with admin.connect() as conn:
        return bool(
            conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
        )


def list_steward_databases(settings: Settings, prefix: str = "dqs_") -> list[str]:
    """All databases owned by this project (never the warehouse)."""
    admin = admin_engine(settings)
    try:
        with admin.connect() as conn:
            rows = conn.execute(
                text("SELECT datname FROM pg_database WHERE datname LIKE :p ORDER BY datname"),
                {"p": f"{prefix}%"},
            ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        admin.dispose()


def drop_database(settings: Settings, name: str) -> bool:
    """Drop one steward database; returns True when it existed."""
    _validate_database_name(name, allow_template=True)
    if name == settings.pg_database:
        raise DatabaseError(f"refusing to drop the configured warehouse database {name!r}")
    admin = admin_engine(settings)
    try:
        existed = _database_exists(admin, name)
        if existed:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        return existed
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not drop database {name!r}: {exc}") from exc
    finally:
        admin.dispose()


def clone_database(settings: Settings, source: str, target: str) -> None:
    """Create `target` as an exact `TEMPLATE` clone of `source` (dropping it first)."""
    _validate_database_name(source, allow_template=True)
    _validate_database_name(target)
    if source == target:
        raise DatabaseError("source and target database must differ")
    if target == settings.pg_database:
        raise DatabaseError(f"refusing to clone over the warehouse database {target!r}")
    admin = admin_engine(settings)
    try:
        if not _database_exists(admin, source):
            raise DatabaseError(
                f"template database {source!r} does not exist; run `dqs demo` first"
            )
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{target}" TEMPLATE "{source}"')
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not clone {source!r} to {target!r}: {exc}") from exc
    finally:
        admin.dispose()


def template_exists(settings: Settings) -> bool:
    """Read-only: is the cached `dqs_template` database present?"""
    admin = admin_engine(settings)
    try:
        return _database_exists(admin, TEMPLATE_DB)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not inspect template database: {exc}") from exc
    finally:
        admin.dispose()


def template_meta(settings: Settings) -> dict[str, Any] | None:
    """Read-only: the template's `dq.template_meta` row, or None when absent."""
    engine = dbmod.make_rw_engine(settings)
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT content_hash, seed, baseline_days, built_at, build_seconds, "
                        "load_seconds, dbt_seconds, row_counts "
                        "FROM dq.template_meta WHERE id = 1"
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        result = dict(row)
        if isinstance(result.get("row_counts"), str):
            result["row_counts"] = json.loads(result["row_counts"])
        return result
    except SQLAlchemyError:
        return None
    finally:
        engine.dispose()


def _write_template_meta(
    settings: Settings,
    *,
    digest: str,
    seed: int,
    days: int,
    built_at: dt.datetime,
    build_seconds: float,
    load_seconds: float,
    dbt_seconds: float,
    row_counts: dict[str, int],
) -> None:
    engine = dbmod.make_rw_engine(settings)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS dq.template_meta (
                    id integer PRIMARY KEY CHECK (id = 1),
                    content_hash text NOT NULL,
                    seed integer NOT NULL,
                    baseline_days integer NOT NULL,
                    built_at timestamptz NOT NULL,
                    build_seconds double precision NOT NULL,
                    load_seconds double precision NOT NULL,
                    dbt_seconds double precision NOT NULL,
                    row_counts jsonb NOT NULL
                )
                """
            )
            conn.execute(
                text(
                    "INSERT INTO dq.template_meta "
                    "(id, content_hash, seed, baseline_days, built_at, build_seconds, "
                    "load_seconds, dbt_seconds, row_counts) "
                    "VALUES (1, :h, :s, :d, :b, :bs, :ls, :ds, CAST(:rows AS jsonb)) "
                    "ON CONFLICT (id) DO UPDATE SET content_hash = EXCLUDED.content_hash, "
                    "seed = EXCLUDED.seed, baseline_days = EXCLUDED.baseline_days, "
                    "built_at = EXCLUDED.built_at, build_seconds = EXCLUDED.build_seconds, "
                    "load_seconds = EXCLUDED.load_seconds, dbt_seconds = EXCLUDED.dbt_seconds, "
                    "row_counts = EXCLUDED.row_counts"
                ),
                {
                    "h": digest,
                    "s": seed,
                    "d": days,
                    "b": built_at,
                    "bs": build_seconds,
                    "ls": load_seconds,
                    "ds": dbt_seconds,
                    "rows": json.dumps(row_counts, sort_keys=True),
                },
            )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not store template metadata: {exc}") from exc
    finally:
        engine.dispose()


def _seed_template(
    settings: Settings, days: int, seed: int
) -> tuple[float, float, float, dict[str, int]]:
    """Load all baseline days (bulk COPY) then run exactly one dbt build."""
    engine = dbmod.make_rw_engine(settings)
    row_counts: dict[str, int] = {}
    started = time.perf_counter()
    try:
        for index in range(days):
            business_date = generator.BASELINE_START_DATE + dt.timedelta(days=index)
            batch = generator.tables_for_date(business_date, seed)
            counts = loader.load_batch(engine, business_date, batch)
            for table, count in counts.items():
                row_counts[table] = row_counts.get(table, 0) + int(count)
    finally:
        engine.dispose()
    load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    exit_code, _output = dbt_run.run_dbt_build(
        settings,
        target="dev",
        batch_id=loader.batch_id_for_date(
            generator.BASELINE_START_DATE + dt.timedelta(days=max(0, days - 1))
        ),
    )
    dbt_seconds = time.perf_counter() - started
    if exit_code != 0:
        raise DatabaseError(f"baseline dbt build failed with exit {exit_code}")
    build_seconds = load_seconds + dbt_seconds
    return build_seconds, load_seconds, dbt_seconds, row_counts


def ensure_template(
    settings: Settings,
    *,
    days: int = BASELINE_DAYS,
    seed: int = SEED,
    force: bool = False,
    progress: ProgressCallback | None = None,
) -> TemplateStatus:
    """Return the cached `dqs_template`, rebuilding it when its hash/seed changed."""
    notify = progress or (lambda _message: None)
    digest = content_hash()
    admin = admin_engine(settings)
    try:
        exists = _database_exists(admin, TEMPLATE_DB)
    finally:
        admin.dispose()

    if exists and not force:
        template_settings = settings.model_copy(update={"pg_database": TEMPLATE_DB})
        meta = template_meta(template_settings)
        if (
            meta is not None
            and meta.get("content_hash") == digest
            and int(meta.get("seed", -1)) == seed
            and int(meta.get("baseline_days", -1)) == days
        ):
            notify(f"template {TEMPLATE_DB} is current (hash {digest[:12]})")
            return TemplateStatus(
                database=TEMPLATE_DB,
                created=False,
                content_hash=digest,
                seed=int(meta["seed"]),
                baseline_days=int(meta["baseline_days"]),
                built_at=meta["built_at"],
                build_seconds=float(meta["build_seconds"]),
                load_seconds=float(meta["load_seconds"]),
                dbt_seconds=float(meta["dbt_seconds"]),
                row_counts={str(k): int(v) for k, v in dict(meta["row_counts"]).items()},
            )

    notify(f"building template {TEMPLATE_DB} (hash {digest[:12]}, {days} days)")
    drop_database(settings, TEMPLATE_DB)
    admin = admin_engine(settings)
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{TEMPLATE_DB}" TEMPLATE template0')
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not create template database: {exc}") from exc
    finally:
        admin.dispose()

    template_settings = settings.model_copy(update={"pg_database": TEMPLATE_DB})
    dbmod.init_database(template_settings)
    build_seconds, load_seconds, dbt_seconds, row_counts = _seed_template(
        template_settings, days, seed
    )
    built_at = dt.datetime.now(dt.UTC)
    _write_template_meta(
        template_settings,
        digest=digest,
        seed=seed,
        days=days,
        built_at=built_at,
        build_seconds=build_seconds,
        load_seconds=load_seconds,
        dbt_seconds=dbt_seconds,
        row_counts=row_counts,
    )
    notify(
        f"template built in {build_seconds:.1f}s (load {load_seconds:.1f}s, dbt {dbt_seconds:.1f}s)"
    )
    return TemplateStatus(
        database=TEMPLATE_DB,
        created=True,
        content_hash=digest,
        seed=seed,
        baseline_days=days,
        built_at=built_at,
        build_seconds=build_seconds,
        load_seconds=load_seconds,
        dbt_seconds=dbt_seconds,
        row_counts=row_counts,
    )


def clone_template(settings: Settings, target: str) -> None:
    """Clone the cached baseline into `target` (bench/demo database)."""
    ensure_template(settings)
    clone_database(settings, TEMPLATE_DB, target)


def cleanup_stale_bench_databases(
    settings: Settings, *, keep: Iterator[str] | None = None
) -> list[str]:
    """Drop leftover `dqs_bench_*` databases; returns the names dropped."""
    keep_set = set(keep or ())
    dropped: list[str] = []
    for name in list_steward_databases(settings, prefix=BENCH_DB_PREFIX):
        if name in keep_set:
            continue
        if drop_database(settings, name):
            dropped.append(name)
    return dropped


__all__ = [
    "BASELINE_DAYS",
    "BENCH_DB_PREFIX",
    "SEED",
    "TEMPLATE_DB",
    "TemplateStatus",
    "admin_engine",
    "cleanup_stale_bench_databases",
    "clone_database",
    "clone_template",
    "content_hash",
    "drop_database",
    "ensure_template",
    "list_steward_databases",
    "template_exists",
    "template_meta",
]
