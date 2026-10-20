"""Sandboxed dbt verification runner (docs/ARCHITECTURE.md §11 steps 2–6).

Why a temp copy instead of the real project: dbt writes `target/`,
`partial_parse.msgpack`, `run_results.json` and logs next to the project. A
verification run must not be able to corrupt the manifest that lineage/impact
read (the Phase-3 "torn manifest" lesson), so every run gets a fresh copy with
`--target-path` and `--log-path` inside a temporary directory.

Why a minimal environment: the dbt child only needs Postgres connection
settings and basic OS variables. Inheriting the process environment would
hand it `LLM_API_KEY`/`GITHUB_TOKEN` even though it has no business seeing
them. The child is started with `shell=False`, a timeout, and an explicit env.

Sandbox schemas are always dropped in `finally`; `cleanup_stale` handles runs
whose process died before cleanup. Verification is deterministic: no LLM is
involved anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs.agents.llm import redact_secrets, settings_secrets
from dqs.config import Settings
from dqs.contracts import store as contracts_store
from dqs.db import make_rw_engine
from dqs.dbt_run import dbt_executable
from dqs.errors import DatabaseError, FixPlanError, LineageError, SandboxError
from dqs.lineage import manifest as manifest_module
from dqs.lineage.graph import LineageGraph
from dqs.models.contract import Contract
from dqs.models.enums import Layer
from dqs.models.fix import MAX_LOG_EXCERPT_CHARS, CriterionResult, FixPlan, VerificationResult
from dqs.verify import criteria as criteria_module
from dqs.verify import patch

logger = logging.getLogger(__name__)

PROJECT_DIR: Final = patch.PROJECT_DIR
CONTRACTS_DIR: Final = Path(__file__).resolve().parents[3] / "contracts"
RUN_RESULTS: Final = Path("target") / "run_results.json"
LOG_DIR: Final = "logs"
SANDBOX_TARGET: Final = "sandbox"
# The only variables a dbt child may inherit. PG* is then overwritten from
# Settings, so a stray shell PGDATABASE cannot point verification at another DB.
_BASE_ENV_ALLOWLIST: Final = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
    }
)
Runner = Callable[..., Any]


@dataclass(frozen=True)
class BuildOutcome:
    """Raw dbt result plus the facts criteria need to interpret it."""

    exit_code: int | None
    timed_out: bool
    output: str
    run_results_present: bool
    failed_nodes: list[str] = field(default_factory=list)
    passed: bool = False
    detail: str = ""


def sandbox_env(
    settings: Settings, base_schema: str, source: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Minimal child environment: OS basics + explicit Postgres/sandbox values.

    API keys and tokens are never copied, even when present in the parent
    process. `PGPASSWORD` is required for dbt and is the documented DB secret.
    """
    origin = dict(os.environ if source is None else source)
    normalized = {key.upper(): value for key, value in origin.items()}
    env = {
        key: normalized[key] for key in _BASE_ENV_ALLOWLIST if key in normalized and normalized[key]
    }
    env.update(
        {
            "PGHOST": settings.pg_host,
            "PGPORT": str(settings.pg_port),
            "PGUSER": settings.pg_user,
            "PGPASSWORD": settings.pg_password,
            "PGDATABASE": settings.pg_database,
            "DQS_SANDBOX_SCHEMA": base_schema,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def _cap_bytes(text_value: str, limit: int = MAX_LOG_EXCERPT_CHARS) -> str:
    raw = text_value.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return text_value
    return raw[-limit:].decode("utf-8", errors="ignore")


def redacted_excerpt(output: str, settings: Settings) -> str:
    """Last 4 KB of dbt output with known secrets removed."""
    clean = redact_secrets(output, secrets=settings_secrets(settings))
    return _cap_bytes(clean)


def dbt_command(
    project_dir: Path,
    models: Sequence[str],
    target_path: Path,
    log_path: Path,
) -> list[str]:
    """`dbt build` command with isolated target/log paths.

    The selector is `staging.*+` rather than `<changed>+`; see the comment
    below for why the narrower selector is not self-contained.
    """
    selectors: list[str] = []
    if models:
        # `staging.*+` is a deliberate superset of `<changed>+`: downstream
        # marts reference *sibling* staging models, and relationship tests live
        # on those siblings. Selecting only the changed branch leaves those
        # relations missing in the sandbox, which would fail the build for a
        # reason unrelated to the fix. Building all staging models + their
        # tests + downstream marts keeps the sandbox self-contained.
        selectors.append("staging.*+")
    cmd = [
        str(dbt_executable()),
        "build",
        "--target",
        SANDBOX_TARGET,
        "--project-dir",
        str(project_dir),
        "--profiles-dir",
        str(project_dir),
        "--target-path",
        str(target_path),
        "--log-path",
        str(log_path),
        "--no-use-colors",
        "--fail-fast",
        "--select",
        *selectors,
    ]
    return cmd


def _parse_run_results(path: Path) -> tuple[bool, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("sandbox run_results unreadable: %s", exc)
        return False, []
    results = payload.get("results")
    if not isinstance(results, list):
        return False, []
    failed = [
        str(entry.get("unique_id") or entry.get("name") or "unknown")
        for entry in results
        if isinstance(entry, dict) and str(entry.get("status", "")).lower() in {"fail", "error"}
    ]
    return True, sorted(failed)


def run_dbt_build(
    project_dir: Path,
    models: Sequence[str],
    base_schema: str,
    settings: Settings,
    *,
    timeout_s: int,
    runner: Runner = subprocess.run,
) -> BuildOutcome:
    """Run the sandbox build once; never raises for a failing/hung dbt run."""
    if not models:
        return BuildOutcome(exit_code=None, timed_out=False, output="", run_results_present=False)
    target_path = project_dir / "target"
    log_path = project_dir / LOG_DIR
    target_path.mkdir(parents=True, exist_ok=True)
    log_path.mkdir(parents=True, exist_ok=True)
    cmd = dbt_command(project_dir, models, target_path, log_path)
    env = sandbox_env(settings, base_schema)
    logger.info("sandbox dbt: %s", " ".join(cmd))

    try:
        completed = runner(
            cmd,
            cwd=str(project_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = _decode_timeout(exc)
        return BuildOutcome(
            exit_code=None,
            timed_out=True,
            output=output,
            run_results_present=(target_path / "run_results.json").exists(),
            detail=f"dbt build exceeded the {timeout_s}s verification timeout",
        )
    except OSError as exc:
        raise SandboxError(f"could not start dbt: {exc}") from exc

    output = (completed.stdout or "") + ("\n" + completed.stderr if completed.stderr else "")
    if not output.strip():
        output = "(dbt produced no output)"
    present, failed = _parse_run_results(target_path / "run_results.json")
    exit_code = int(completed.returncode)
    if exit_code != 0:
        detail = f"dbt build exited {exit_code}"
        if failed:
            detail += f"; failing nodes: {', '.join(failed[:5])}"
    elif not present:
        detail = "dbt build exited 0 but run_results.json is missing"
    elif failed:
        detail = f"dbt build exited 0 but run_results reports failures: {', '.join(failed[:5])}"
    else:
        detail = "dbt build and its tests passed"
    passed = exit_code == 0 and present and not failed
    return BuildOutcome(
        exit_code=exit_code,
        timed_out=False,
        output=output,
        run_results_present=present,
        failed_nodes=failed,
        passed=passed,
        detail=detail,
    )


def _decode_timeout(exc: subprocess.TimeoutExpired) -> str:
    def as_text(value: bytes | str | None) -> str:
        if value is None:
            return ""
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value

    return as_text(exc.stdout) + as_text(exc.stderr)


def _sandbox_schemas(base: str) -> list[str]:
    names = [
        base,
        criteria_module.sandbox_schema(base, Layer.STAGING),
        criteria_module.sandbox_schema(base, Layer.MARTS),
    ]
    for name in names:
        if not patch.is_sandbox_schema(name):
            raise SandboxError(f"refusing to drop a non-sandbox schema: {name!r}")
    return names


def drop_sandbox_schemas(engine: Engine, base: str, *, raise_on_error: bool = True) -> list[str]:
    """Drop the base and layer schemas; every name is validated first."""
    dropped: list[str] = []
    errors: list[str] = []
    for name in _sandbox_schemas(base):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            dropped.append(name)
        except SQLAlchemyError as exc:
            errors.append(f"{name}: {exc}")
    if errors and raise_on_error:
        raise SandboxError("could not drop sandbox schema(s): " + "; ".join(errors))
    if errors:
        logger.error("sandbox cleanup failed: %s", "; ".join(errors))
    return dropped


def cleanup_stale_sandboxes(engine: Engine) -> list[str]:
    """Drop every leftover `dqs_sandbox_*` schema (e.g. from a killed run)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'dqs_sandbox_%' ORDER BY schema_name"
                )
            ).fetchall()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not list sandbox schemas: {exc}") from exc

    dropped: list[str] = []
    for (raw_name,) in rows:
        name = str(raw_name)
        if not patch.is_sandbox_schema(name):
            logger.warning("refusing to drop unexpected schema %r", name)
            continue
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
            dropped.append(name)
        except SQLAlchemyError as exc:
            raise DatabaseError(f"could not drop sandbox schema {name!r}: {exc}") from exc
    return dropped


def _table_exists(engine: Engine, schema: str, table: str) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = :s AND table_name = :t LIMIT 1"
                ),
                {"s": schema, "t": table},
            ).fetchone()
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not check sandbox table: {exc}") from exc
    return row is not None


def persist_attempt(
    engine: Engine,
    plan: FixPlan,
    result: VerificationResult,
    *,
    attempt_no: int | None = None,
) -> int:
    """Append the plan + verdict to `dq.fix_attempts` and return the attempt number."""
    if attempt_no is None:
        if plan.incident_id is None:
            attempt_no = 1
        else:
            try:
                with engine.connect() as conn:
                    current: Any = conn.execute(
                        text(
                            "SELECT coalesce(max(attempt_no), 0) FROM dq.fix_attempts "
                            "WHERE incident_id = :i"
                        ),
                        {"i": plan.incident_id},
                    ).scalar_one()
                attempt_no = int(current) + 1
            except SQLAlchemyError as exc:
                raise DatabaseError(f"could not read previous fix attempts: {exc}") from exc
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dq.fix_attempts "
                    "(incident_id, attempt_no, plan, verification, passed) "
                    "VALUES (:i, :n, CAST(:p AS jsonb), CAST(:v AS jsonb), :ok)"
                ),
                {
                    "i": plan.incident_id,
                    "n": attempt_no,
                    "p": json.dumps(plan.model_dump(mode="json")),
                    "v": json.dumps(result.model_dump(mode="json")),
                    "ok": result.passed,
                },
            )
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not persist fix attempt: {exc}") from exc
    return attempt_no


def verify_fix(
    settings: Settings,
    plan: FixPlan,
    *,
    project_dir: Path = PROJECT_DIR,
    contracts_dir: Path = CONTRACTS_DIR,
    engine: Engine | None = None,
    timeout_s: int | None = None,
    runner: Runner = subprocess.run,
    persist: bool = True,
    sandbox_schema: str | None = None,
) -> VerificationResult:
    """Verify one deterministic fix plan in an isolated dbt sandbox."""
    started = time.monotonic()
    base_schema = sandbox_schema or patch.sandbox_schema_name(plan)
    if not patch.is_sandbox_schema(base_schema):
        raise FixPlanError(f"invalid sandbox schema name {base_schema!r}")
    timeout = timeout_s or settings.fix_verify_timeout_seconds
    owned_engine = engine is None
    resolved_engine = engine or make_rw_engine(settings)
    criteria: list[CriterionResult] = []
    outcome = BuildOutcome(exit_code=None, timed_out=False, output="", run_results_present=False)
    models: list[str] = []
    contracts: dict[str, Contract] = {}
    cleaned = False

    def finish() -> VerificationResult:
        nonlocal cleaned
        # Cleanup is part of the verdict: an undropped schema is a failed run.
        try:
            drop_sandbox_schemas(resolved_engine, base_schema)
            cleaned = True
            criteria.append(CriterionResult(name="sandbox_cleanup", passed=True))
        except SandboxError as exc:
            criteria.append(CriterionResult(name="sandbox_cleanup", passed=False, detail=str(exc)))
        row_counts: dict[str, int] = {}
        for criterion in criteria:
            for key, value in criterion.observed.items():
                if isinstance(value, int) and key.endswith(("rows", "row_count")):
                    row_counts[key] = value
        result = VerificationResult.from_criteria(
            criteria,
            dbt_log_excerpt=redacted_excerpt(outcome.output, settings),
            row_counts=row_counts,
            duration_s=time.monotonic() - started,
            sandbox_schema=base_schema,
            dbt_exit_code=outcome.exit_code,
            contract_tables=[f"staging.{model}" for model in models],
        )
        if persist:
            persist_attempt(resolved_engine, plan, result)
        return result

    try:
        # 1. allowlist
        try:
            canonical = patch.validate_edit_paths(plan.edits, project_dir)
        except FixPlanError as exc:
            criteria.append(CriterionResult(name="allowlist", passed=False, detail=str(exc)))
            return finish()

        # 2. relevance (read-only manifest; never regenerate it here)
        try:
            loaded = manifest_module.load_manifest(project_dir, regenerate_when_stale=False)
            graph = LineageGraph.from_manifest(loaded, project_dir=project_dir)
            resolved = patch.validate_relevance(canonical, graph, plan.affected_table)
        except (FixPlanError, LineageError) as exc:
            criteria.append(CriterionResult(name="relevance", passed=False, detail=str(exc)))
            return finish()
        models = patch.model_names(list(resolved.values()))
        contracts = contracts_store.load_approved(contracts_dir)

        # A stale schema from a killed run must not leak into this attempt.
        drop_sandbox_schemas(resolved_engine, base_schema)

        with tempfile.TemporaryDirectory(prefix="dqs_verify_") as temporary:
            work = Path(temporary) / "dbt_project"
            patch.copy_project(project_dir, work)
            try:
                patch.apply_edits(plan.edits, work)
            except FixPlanError as exc:
                criteria.append(CriterionResult(name="exact_match", passed=False, detail=str(exc)))
                return finish()

            outcome = run_dbt_build(
                work,
                models,
                base_schema,
                settings,
                timeout_s=timeout,
                runner=runner,
            )
            criteria.append(
                CriterionResult(name="dbt_build", passed=outcome.passed, detail=outcome.detail)
            )

            staging_schema = criteria_module.sandbox_schema(base_schema, Layer.STAGING)
            table_present = any(
                _table_exists(resolved_engine, staging_schema, model) for model in models
            )
            if table_present:
                criteria.extend(
                    criteria_module.evaluate(resolved_engine, plan, base_schema, models, contracts)
                )
            elif not outcome.passed:
                # No model output exists (e.g. a syntax error): dbt_build is the
                # honest failure, not a pile of "relation does not exist" noise.
                criteria.append(
                    CriterionResult(
                        name="targeted_check",
                        passed=False,
                        detail="no staging table was produced by the failed build",
                    )
                )
        return finish()
    finally:
        # Belt and braces for an unexpected exception: never leave a schema
        # behind, even if the normal `finish()` path was skipped.
        if not cleaned:
            drop_sandbox_schemas(resolved_engine, base_schema, raise_on_error=False)
        if owned_engine:
            resolved_engine.dispose()
