"""Benchmark runner: catalogue → fault → monitor → workflow → score (Phase 8a).

Design choices that protect integrity:

* every scenario starts from an **exact clone** of the cached clean baseline, so
  one scenario can never leak data into the next;
* the workflow always runs with `auto_approve=True` and `GITHUB_MODE=dry_run`
  (a live mode is refused), so a benchmark can never write to GitHub;
* a daily provider quota (or the local daily token cap) stops the run cleanly
  and leaves a resume hint; a per-minute rate limit is retried by `LLMClient`;
* results are rewritten after every scenario, so an interrupted run keeps the
  work it already paid for.

The runner is sequential on purpose: parallel scenarios would share the LLM
rate limit and make wall-time/cost numbers meaningless.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import subprocess
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine

from dqs import db as dbmod
from dqs import fast_reset
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.agents.llm import (
    BudgetTracker,
    LLMClient,
    get_llm,
    is_daily_quota_message,
    is_infra_error,
    settings_secrets,
)
from dqs.bench import oracle
from dqs.bench.harness import code_fingerprint, git_dirty, harness_metadata, prompt_hashes
from dqs.bench.ledger import DEFAULT_LEDGER_PATH, TokenLedger
from dqs.bench.report import BenchMetadata, BenchRunFile, load_run_file, render_markdown
from dqs.bench.scenarios import (
    FAULT_DATE,
    SCENARIOS,
    SEED,
    Scenario,
    scenarios_for,
)
from dqs.bench.scoring import Observation, ScenarioRun, score_scenario
from dqs.bench.summary import StepSummary, load_fix_history, load_step_records, summarize_steps
from dqs.config import Settings, estimate_cost_usd, price_for_model
from dqs.contracts import store as contracts_store
from dqs.errors import DailyQuotaExceeded, DqsError
from dqs.models.enums import ResolutionType
from dqs.models.root_cause import RootCause
from dqs.publish.base import Publisher
from dqs.publish.store import load_latest_fix_attempt
from dqs.sim import pipeline as sim_pipeline
from dqs.sim.faults import parse_fault_specs
from dqs.tools.base import ToolContext
from dqs.workflow.runner import IncidentWorkflow, make_deps

logger = logging.getLogger(__name__)

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
RESULTS_DIR: Final = REPO_ROOT / "benchmarks" / "results"
CHECKPOINT_PATH: Final = REPO_ROOT / "data" / "bench_checkpoints.sqlite"
CONTRACTS_DIR: Final = REPO_ROOT / "contracts"
# A prompt above this many input tokens usually means a tool result was not
# compacted; the benchmark warns instead of silently paying for it.
PROMPT_TOKEN_WARNING: Final = 6000
# Two consecutive provider-side failures are enough evidence that continuing
# would just burn the day's quota. A daily-quota error stops immediately.
INFRA_CIRCUIT_BREAKER: Final = 2

PRIMARY_KEYS: Final = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
}


class BenchError(DqsError):
    """The benchmark cannot start or continue with the requested options."""


class BenchOptions(BaseModel):
    """CLI/runner options with documented defaults (`ARCHITECTURE.md` §14)."""

    model_config = ConfigDict(extra="forbid")

    model: str
    split: str = "dev"
    limit: int | None = None
    ids: list[str] | None = None
    resume: Path | None = None
    no_dbt: bool = False
    daily_token_cap: int = 180_000
    tpm_limit: int = 8_000
    pause_seconds: float = 1.0
    confirm_holdout: bool = False
    llm_retry_max_wait_seconds: float = 90.0
    repeats: int = 1
    acknowledge_changes: str | None = None
    # A diagnostic run is deliberately kept separate from benchmark evidence.
    diagnostic: bool = False
    ledger_path: Path = DEFAULT_LEDGER_PATH
    results_dir: Path = RESULTS_DIR


Progress = Callable[[str], None]
ClientFactory = Callable[[Settings, str, str, float], LLMClient]


# --- small deterministic helpers ------------------------------------------------------


def git_sha(cwd: Path = REPO_ROOT) -> str:
    """Current commit, or `unknown` outside a git checkout (never raises)."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and sha else "unknown"


def _safe_model_name(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "model"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def force_bench_dry_run(settings: Settings) -> Settings:
    """The benchmark *ignores* `GITHUB_MODE` and always publishes to `out/`.

    A benchmark may run hundreds of real model decisions; one misconfigured
    `.env` must not turn that into live issues/PRs. Forcing the effective mode is
    safer than refusing to start, because it lets the measurement run while the
    publisher construction still goes through the dry-run path.
    """
    return settings.model_copy(update={"github_mode": "dry_run"})


def _run_id() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _non_secret_settings(settings: Settings) -> dict[str, Any]:
    """A whitelist snapshot: never a `model_dump` (that would leak keys)."""
    return {
        "pg_host": settings.pg_host,
        "pg_port": settings.pg_port,
        "dqs_seed": settings.dqs_seed,
        "max_tool_calls": settings.max_tool_calls,
        "max_fix_attempts": settings.max_fix_attempts,
        "max_tokens_per_incident": settings.max_tokens_per_incident,
        "incident_timeout_seconds": settings.incident_timeout_seconds,
        "fix_verify_timeout_seconds": settings.fix_verify_timeout_seconds,
        "llm_provider": settings.llm_provider,
        "llm_max_retries": settings.llm_max_retries,
        "llm_retry_base_seconds": settings.llm_retry_base_seconds,
        "github_mode": settings.github_mode,
        "pricing": "override" if settings.llm_input_price_per_mtok is not None else "table",
    }


def _scenario_database(scenario: Scenario) -> str:
    suffix = re.sub(r"[^a-z0-9]+", "_", scenario.id.lower()).strip("_")
    return f"{fast_reset.BENCH_DB_PREFIX}{suffix}"


def _load_previous_dev(results_dir: Path, current_run_id: str) -> dict[str, Any] | None:
    """Newest earlier `dev` results file (provenance for the holdout warning)."""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            metadata = payload.get("metadata") or {}
        except (OSError, json.JSONDecodeError):
            continue
        if metadata.get("split") != "dev" or metadata.get("run_id") == current_run_id:
            continue
        candidates.append((str(metadata.get("finished_at") or ""), metadata))
    newest = max(candidates, key=lambda item: item[0], default=None)
    return None if newest is None else newest[1]


class TokenThrottle:
    """Sliding one-minute token window; sleeps only when the limit is reached."""

    def __init__(
        self,
        tpm_limit: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tpm_limit = max(1, int(tpm_limit))
        self._clock = clock
        self._sleep = sleep
        self._events: deque[tuple[float, int]] = deque()

    def record(self, tokens: int) -> None:
        self._events.append((self._clock(), max(0, int(tokens))))
        self._prune()

    def wait(self) -> float:
        """Wait until the last minute's total is below the limit; return waited."""
        waited = 0.0
        while True:
            self._prune()
            total = sum(tokens for _, tokens in self._events)
            if total < self.tpm_limit or not self._events:
                return waited
            oldest_time = self._events[0][0]
            delay = max(0.05, 60.0 - (self._clock() - oldest_time))
            self._sleep(delay)
            waited += delay

    def _prune(self) -> None:
        cutoff = self._clock() - 60.0
        while self._events and self._events[0][0] <= cutoff:
            self._events.popleft()


def _default_client_factory(
    settings: Settings, model: str, provider: str, max_retry_wait: float
) -> LLMClient:
    return LLMClient(
        get_llm(provider, model, settings=settings),
        provider=provider,
        model=model,
        budget=BudgetTracker.from_settings(settings),
        max_retries=settings.llm_max_retries,
        max_retry_wait_seconds=max_retry_wait,
        retry_base_seconds=settings.llm_retry_base_seconds,
        secret_values=settings_secrets(settings),
    )


# --- one scenario ---------------------------------------------------------------------


def _root_cause_from_incident(engine: Engine, incident_id: uuid.UUID) -> RootCause | None:
    incident = incidents_mod.get_incident(engine, incident_id)
    if incident is None or incident.root_cause is None:
        return None
    try:
        return RootCause.model_validate(incident.root_cause)
    except Exception as exc:  # a corrupt row is a failed diagnosis, not a crash
        logger.warning("incident %s root_cause does not validate: %s", incident_id, exc)
        return None


def _probe_incident_id(engine: Engine) -> uuid.UUID:
    """The newest incident in this scenario database (the workflow's subject)."""
    found = incidents_mod.list_incidents(engine, limit=1)
    if not found:
        raise BenchError("no incident exists to verify in the scenario database")
    return found[0].incident_id


def _safe_step_summary(
    engine: Engine,
    incident_id: uuid.UUID,
    fix_attempts: int,
    failed_criteria: list[str],
) -> StepSummary | None:
    """Read the step log for the result record; reporting must never fail a run."""
    try:
        return summarize_steps(
            load_step_records(engine, incident_id),
            fix_attempts=fix_attempts,
            failed_criteria=failed_criteria,
        )
    except Exception as exc:  # a diagnostic read is not worth losing the score
        logger.warning("could not summarise agent steps for %s: %s", incident_id, exc)
        return None


def execute_scenario(
    settings: Settings,
    options: BenchOptions,
    scenario: Scenario,
    *,
    contracts: dict[str, Any],
    repeat: int = 0,
    publisher: Publisher | None = None,
    client_factory: ClientFactory = _default_client_factory,
    progress: Progress = lambda _message: None,
) -> ScenarioRun:
    """Run and score one scenario; the caller owns the database lifecycle."""
    database = _scenario_database(scenario)
    # Safety: the benchmark owns the effective mode, not `.env`.
    bench_settings = force_bench_dry_run(settings).model_copy(update={"pg_database": database})
    started = time.perf_counter()
    client: LLMClient | None = None
    engine: Engine | None = None
    context: ToolContext | None = None
    observation = Observation(repeat=repeat)
    oracle_error: str | None = None

    try:
        fast_reset.clone_template(settings, database)
        specs = [scenario.fault_spec] if scenario.fault_spec else []
        sim_pipeline.load_day(
            bench_settings,
            FAULT_DATE,
            SEED,
            run_dbt=not options.no_dbt,
            specs=parse_fault_specs(specs),
        )
        engine = dbmod.make_rw_engine(bench_settings)
        as_of = dt.datetime.combine(FAULT_DATE + dt.timedelta(days=1), dt.time(0, 0), tzinfo=dt.UTC)
        monitor = monitor_mod.run_monitor(engine, contracts, as_of)
        found = [incidents_mod.get_incident(engine, incident_id) for incident_id in monitor.created]
        incidents = [incident for incident in found if incident is not None]
        observation.incidents = incidents

        if incidents:
            incident = incidents[0]
            client = client_factory(
                bench_settings,
                options.model,
                settings.llm_provider,
                options.llm_retry_max_wait_seconds,
            )
            tracker = client.budget or BudgetTracker.from_settings(bench_settings)
            client.budget = tracker
            context = ToolContext.from_settings(
                bench_settings,
                contracts_dir=CONTRACTS_DIR,
                project_dir=REPO_ROOT / "warehouse" / "dbt_project",
            )
            deps = make_deps(
                bench_settings,
                engine,
                client=client,
                budget=tracker,
                tool_context=context,
                contracts_dir=CONTRACTS_DIR,
                publisher=publisher,
            )
            with IncidentWorkflow(deps, checkpoint_path=CHECKPOINT_PATH) as workflow:
                outcome = workflow.run(incident.incident_id, auto_approve=True)
                if outcome.interrupted:
                    outcome = workflow.resume(
                        incident.incident_id, decision="approve", approver="benchmark"
                    )
            observation.resolution = outcome.resolution_type
            if outcome.error:
                if is_infra_error(outcome.error):
                    observation.infra_error = outcome.error
                else:
                    observation.error = outcome.error
            observation.root_cause = _root_cause_from_incident(engine, incident.incident_id)
            try:
                fix_attempts, failed_criteria = load_fix_history(engine, incident.incident_id)
            except Exception as exc:
                logger.warning("could not read fix history for %s: %s", incident.incident_id, exc)
                fix_attempts, failed_criteria = 0, []
            observation.fix_attempts = fix_attempts
            observation.fix_attempted = fix_attempts > 0
            observation.failed_criteria = failed_criteria
            observation.step_summary = _safe_step_summary(
                engine, incident.incident_id, fix_attempts, failed_criteria
            )
            attempt = load_latest_fix_attempt(engine, incident.incident_id)
            observation.verification_passed = (
                None if attempt is None else attempt.verification.passed
            )
            if (
                scenario.expected is not None
                and scenario.expected.expected_resolution is ResolutionType.PR
                and observation.verification_passed
            ):
                semantic, oracle_error = _semantic_oracle(
                    bench_settings, scenario, engine, incident.incident_id
                )
                observation.semantic_passed = semantic
            max_prompt = max((call.input_tokens for call in client.calls), default=0)
            if max_prompt > PROMPT_TOKEN_WARNING:
                progress(
                    f"warning: {scenario.id} sent {max_prompt} input tokens in one call "
                    f"(> {PROMPT_TOKEN_WARNING}); check tool-result compaction"
                )
            observation.tool_calls = tracker.tool_calls
            observation.tokens_in = sum(call.input_tokens for call in client.calls)
            observation.tokens_out = sum(call.output_tokens for call in client.calls)

        if (
            oracle_error is not None
            and observation.error is None
            and observation.infra_error is None
        ):
            observation.error = oracle_error
    except DailyQuotaExceeded:
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if is_infra_error(exc):
            observation.infra_error = message
        else:
            observation.error = message
    finally:
        observation.wall_s = time.perf_counter() - started
        if context is not None:
            context.close()
        if engine is not None:
            engine.dispose()
        try:
            fast_reset.drop_database(settings, database)
        except DqsError as cleanup_exc:
            # Never lose the scored result because cleanup failed; the DB is
            # named deterministically and the next run drops it first.
            logger.warning("could not drop scenario database %s: %s", database, cleanup_exc)

    observation.cost_usd = estimate_cost_usd(
        options.model, observation.tokens_in, observation.tokens_out, settings
    )
    return score_scenario(scenario, observation)


def _semantic_oracle(
    settings: Settings, scenario: Scenario, engine: Engine, incident_id: uuid.UUID
) -> tuple[bool | None, str | None]:
    """Run the answer-key oracle for the incident a PR fix was verified against."""
    attempt = load_latest_fix_attempt(engine, incident_id)
    if attempt is None:
        return False, "verification passed but no fix attempt was stored"
    plan = attempt.plan
    if plan is None:
        return False, "verification passed but the stored attempt carries no plan"
    assert scenario.table is not None
    key = PRIMARY_KEYS.get(scenario.table)
    if key is None:
        return None, f"no oracle primary key for table {scenario.table!r}"
    try:
        result = oracle.compare_patched_staging(
            settings,
            plan,
            engine=engine,
            fault_date=FAULT_DATE,
            seed=SEED,
            raw_table=scenario.table,
            staging_table=f"stg_{scenario.table}",
            key=key,
        )
    except Exception as exc:
        return False, f"oracle failed: {type(exc).__name__}: {exc}"
    return result.passed, None if result.passed else result.detail


# --- the whole run --------------------------------------------------------------------


def _select_scenarios(options: BenchOptions) -> list[Scenario]:
    selected = scenarios_for(options.split)
    if options.ids:
        wanted = [item.strip() for item in options.ids if item.strip()]
        by_id = {scenario.id: scenario for scenario in SCENARIOS}
        missing = [scenario_id for scenario_id in wanted if scenario_id not in by_id]
        if missing:
            raise BenchError(f"unknown scenario id(s): {', '.join(missing)}")
        selected = [by_id[scenario_id] for scenario_id in wanted]
        for scenario in selected:
            if scenario.split.value != options.split and options.split != "all":
                raise BenchError(
                    f"scenario {scenario.id} is {scenario.split.value}, not {options.split}"
                )
    if options.limit is not None:
        selected = selected[: max(0, options.limit)]
    return selected


def _require_fingerprint_ack(
    previous_fingerprint: object,
    current_fingerprint: str,
    acknowledge_changes: str | None,
    *,
    source: str,
) -> None:
    """Refuse a holdout/all run when the code that produced the dev file changed."""
    previous = str(previous_fingerprint or "")
    if previous == current_fingerprint:
        return
    if acknowledge_changes and acknowledge_changes.strip():
        return
    raise BenchError(
        "the benchmark code/prompt fingerprint changed since "
        f"{source} ({previous[:12] or 'unknown'} -> {current_fingerprint[:12]}); "
        'pass --acknowledge-changes "reason" to record and continue'
    )


def _new_run_file(
    settings: Settings,
    options: BenchOptions,
    selected: Sequence[Scenario],
    run_id: str,
    *,
    fingerprint: str,
    dirty: bool,
    github_mode_env: str,
) -> BenchRunFile:
    holdout_count = sum(1 for scenario in selected if scenario.split.value == "holdout")
    previous = _load_previous_dev(options.results_dir, run_id)
    prompt_changed: bool | None = None
    previous_run: str | None = None
    if previous is not None:
        previous_run = str(previous.get("run_id") or "")
        prompt_changed = previous.get("prompt_hashes") != prompt_hashes()
        if options.split in {"holdout", "all"}:
            _require_fingerprint_ack(
                previous.get("code_fingerprint"),
                fingerprint,
                options.acknowledge_changes,
                source=f"the newest dev run `{previous_run or 'unknown'}`",
            )
    if holdout_count and not options.confirm_holdout:
        raise BenchError(
            "the holdout split is for reporting only: pass --confirm-holdout to run it"
        )
    pricing = price_for_model(options.model)
    hashes = prompt_hashes()
    metadata = BenchMetadata(
        run_id=run_id,
        model=options.model,
        provider=settings.llm_provider,
        split=options.split,
        started_at=_now_iso(),
        finished_at=_now_iso(),
        git_sha=git_sha(),
        git_dirty=dirty,
        code_fingerprint=fingerprint,
        acknowledged_changes=options.acknowledge_changes,
        harness_version=str(harness_metadata()["harness_version"]),
        prompt_hashes=hashes,
        settings={
            **_non_secret_settings(settings),
            "priced_model": pricing is not None,
            "daily_token_cap": options.daily_token_cap,
            "tpm_limit": options.tpm_limit,
            "llm_retry_max_wait_seconds": options.llm_retry_max_wait_seconds,
            "dbt_after_load": not options.no_dbt,
            "github_mode_env": github_mode_env,
            "github_mode_forced": github_mode_env != "dry_run",
            "repeats": options.repeats,
        },
        dbt_after_load=not options.no_dbt,
        daily_token_cap=options.daily_token_cap,
        tpm_limit=options.tpm_limit,
        pause_seconds=options.pause_seconds,
        llm_retry_max_wait_seconds=options.llm_retry_max_wait_seconds,
        repeats=options.repeats,
        holdout_confirmed=options.confirm_holdout,
        prompt_changed_since_last_dev=prompt_changed,
        previous_dev_run=previous_run,
        diagnostic=options.diagnostic,
        diagnostic_note=(
            "diagnostic selection; not a representative benchmark run"
            if options.diagnostic
            else None
        ),
        catalogue_size=len(selected),
        fault_count=sum(1 for scenario in selected if not scenario.is_control),
        control_count=sum(1 for scenario in selected if scenario.is_control),
        holdout_count=holdout_count,
    )
    return BenchRunFile(metadata=metadata, scenarios=[])


def _write_run(run: BenchRunFile, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(run.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _results_path(options: BenchOptions, run_id: str) -> Path:
    return options.results_dir / f"{run_id}_{_safe_model_name(options.model)}.json"


def _refresh_totals(run: BenchRunFile) -> None:
    run.metadata.tokens_total = sum(
        result.tokens_in + result.tokens_out for result in run.scenarios
    )
    run.metadata.cost_usd_total = round(sum(result.cost_usd for result in run.scenarios), 6)
    run.metadata.wall_s_total = round(sum(result.wall_s for result in run.scenarios), 3)


def _scenario_key(result: ScenarioRun) -> tuple[str, int]:
    return result.scenario_id, result.repeat


def _ordered_results(
    existing: dict[tuple[str, int], ScenarioRun], selected: Sequence[Scenario]
) -> list[ScenarioRun]:
    order = {scenario.id: index for index, scenario in enumerate(selected)}
    return sorted(
        existing.values(),
        key=lambda result: (order.get(result.scenario_id, len(order)), result.repeat),
    )


def _infra_result(scenario: Scenario, repeat: int, message: str) -> ScenarioRun:
    return score_scenario(scenario, Observation(repeat=repeat, infra_error=message))


def _finish(run: BenchRunFile, results_path: Path) -> None:
    _refresh_totals(run)
    run.metadata.finished_at = _now_iso()
    _write_run(run, results_path)
    write_latest_md(run, results_path.parent)


def run_benchmark(
    settings: Settings,
    options: BenchOptions,
    *,
    progress: Progress = lambda _message: None,
    client_factory: ClientFactory = _default_client_factory,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    today: Callable[[], dt.date] = _utc_today,
) -> tuple[BenchRunFile, Path]:
    """Run the selected scenarios sequentially; returns the file and its path."""
    if options.repeats < 1:
        raise BenchError("--repeats must be at least 1")
    github_mode_env = settings.github_mode
    effective = force_bench_dry_run(settings)
    contracts = contracts_store.load_approved(CONTRACTS_DIR)
    if not contracts:
        raise BenchError(
            "no approved contracts in contracts/; run `dqs contracts approve --all` first"
        )
    selected = _select_scenarios(options)
    if not selected:
        raise BenchError("no scenarios selected")

    fingerprint = code_fingerprint()
    dirty = git_dirty()

    resume_from: BenchRunFile | None = None
    if options.resume is not None:
        path = Path(options.resume)
        if not path.exists():
            raise BenchError(f"--resume file does not exist: {path}")
        resume_from = load_run_file(json.loads(path.read_text(encoding="utf-8")))
        if resume_from.metadata.model != options.model:
            raise BenchError(
                f"refusing to mix models in one file: {path} is {resume_from.metadata.model!r}, "
                f"--model is {options.model!r}"
            )
        if resume_from.metadata.split != options.split:
            raise BenchError(
                f"resume file split is {resume_from.metadata.split!r}, --split is {options.split!r}"
            )
        if resume_from.metadata.provider != effective.llm_provider:
            raise BenchError(
                f"resume file provider is {resume_from.metadata.provider!r}, "
                f"settings say {effective.llm_provider!r}"
            )
        if resume_from.metadata.repeats != options.repeats:
            raise BenchError(
                f"resume file used --repeats {resume_from.metadata.repeats}, "
                f"but --repeats is {options.repeats}"
            )
        if options.split in {"holdout", "all"}:
            _require_fingerprint_ack(
                resume_from.metadata.code_fingerprint,
                fingerprint,
                options.acknowledge_changes,
                source=f"the resume file {path.name}",
            )
        run = resume_from
        results_path = path
        completed = {
            _scenario_key(result)
            for result in run.scenarios
            if result.error is None and result.infra_error is None
        }
        tasks = [(scenario, repeat) for scenario in selected for repeat in range(options.repeats)]
        remaining = sum(1 for key in tasks if (key[0].id, key[1]) not in completed)
        progress(f"resuming {path.name}: {len(completed)} completed, {remaining} to run")
    else:
        run_id = _run_id()
        run = _new_run_file(
            effective,
            options,
            selected,
            run_id,
            fingerprint=fingerprint,
            dirty=dirty,
            github_mode_env=github_mode_env,
        )
        results_path = _results_path(options, run_id)
        completed = set()

    # Holdout protection also applies when resuming (metadata may be older).
    if any(scenario.split.value == "holdout" for scenario in selected) and not (
        options.confirm_holdout or run.metadata.holdout_confirmed
    ):
        raise BenchError("the holdout split requires --confirm-holdout")

    existing = {_scenario_key(result): result for result in run.scenarios}
    throttle = TokenThrottle(options.tpm_limit, clock=clock, sleep=sleep)
    run.metadata.resume_hint = None
    run.metadata.stop_reason = None
    tasks = [(scenario, repeat) for scenario in selected for repeat in range(options.repeats)]
    total_runs = len(tasks)
    progress(
        f"benchmark model={options.model} split={options.split} "
        f"scenarios={len(selected)} repeats={options.repeats} "
        f"dbt_after_load={not options.no_dbt}"
    )

    ledger = TokenLedger(options.ledger_path, today=today)
    provider = effective.llm_provider
    already_used = ledger.total(provider, options.model)
    progress(
        f"token ledger: {already_used} tokens already recorded today for "
        f"{provider}/{options.model} (daily cap {options.daily_token_cap})"
    )
    progress(
        "note: other LLM use (dqs demo, tests, another terminal) shares the same "
        "provider quota; this ledger only sees benchmark runs"
    )

    consecutive_infra = 0
    for index, (scenario, repeat) in enumerate(tasks):
        key = (scenario.id, repeat)
        if key in completed:
            continue
        used = ledger.total(provider, options.model)
        if used >= options.daily_token_cap:
            run.metadata.stop_reason = (
                f"daily token cap reached before {scenario.id} repeat {repeat + 1} "
                f"({used} >= {options.daily_token_cap} tokens today)"
            )
            run.metadata.resume_hint = _resume_command(options, results_path)
            progress(f"stopping: {run.metadata.stop_reason}")
            break

        throttle.wait()
        label = f"{scenario.id} ({scenario.kind})"
        if options.repeats > 1:
            label += f" repeat {repeat + 1}/{options.repeats}"
        progress(f"[{index + 1}/{total_runs}] {label}")
        try:
            result = execute_scenario(
                effective,
                options,
                scenario,
                contracts=contracts,
                repeat=repeat,
                client_factory=client_factory,
                progress=progress,
            )
        except DailyQuotaExceeded as exc:
            # Daily quota is the strongest infra signal: record the attempt (so
            # --resume knows to re-run it), then stop immediately.
            result = _infra_result(scenario, repeat, f"daily provider quota exhausted: {exc}")
            existing[key] = result
            run.scenarios = _ordered_results(existing, selected)
            ledger.add(provider, options.model, result.tokens_in + result.tokens_out)
            run.metadata.stop_reason = f"daily provider quota exhausted: {exc}"
            run.metadata.resume_hint = _resume_command(options, results_path)
            progress(f"quota exhausted; recorded {scenario.id} as infra_error; stopping")
            _finish(run, results_path)
            return run, results_path

        tokens = result.tokens_in + result.tokens_out
        ledger.add(provider, options.model, tokens)
        throttle.record(tokens)
        existing[key] = result
        run.scenarios = _ordered_results(existing, selected)
        _refresh_totals(run)
        run.metadata.finished_at = _now_iso()
        _write_run(run, results_path)

        if result.infra_error is not None:
            consecutive_infra += 1
        else:
            consecutive_infra = 0
        flag = ""
        if result.infra_error is not None:
            flag = f" INFRA {result.infra_error}"
        elif result.error is not None:
            flag = f" ERROR {result.error}"
        progress(
            f"    detected={result.detected} category={result.actual_category} "
            f"resolution={result.actual_resolution} tokens={tokens}{flag}"
        )

        if result.infra_error is not None and is_daily_quota_message(result.infra_error):
            run.metadata.stop_reason = f"daily provider quota exhausted: {result.infra_error}"
            run.metadata.resume_hint = _resume_command(options, results_path)
            progress(f"stopping: {run.metadata.stop_reason}")
            break
        if consecutive_infra >= INFRA_CIRCUIT_BREAKER:
            run.metadata.stop_reason = (
                f"circuit breaker: {consecutive_infra} consecutive infrastructure errors "
                f"(last: {result.infra_error})"
            )
            run.metadata.resume_hint = _resume_command(options, results_path)
            progress(f"stopping: {run.metadata.stop_reason}")
            break
        if options.pause_seconds > 0 and index < total_runs - 1:
            sleep(options.pause_seconds)

    _finish(run, results_path)
    return run, results_path


def _resume_command(options: BenchOptions, results_path: Path) -> str:
    repeats = f" --repeats {options.repeats}" if options.repeats > 1 else ""
    return (
        f"dqs bench run --split {options.split} --model {options.model}{repeats} "
        f"--resume {results_path.as_posix()}"
    )


def write_latest_md(run: BenchRunFile, directory: Path = RESULTS_DIR) -> Path:
    """Write the human-readable report next to the JSON results."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "latest.md"
    path.write_text(render_markdown(run), encoding="utf-8")
    return path


__all__ = [
    "BenchError",
    "BenchOptions",
    "CHECKPOINT_PATH",
    "CONTRACTS_DIR",
    "PROMPT_TOKEN_WARNING",
    "RESULTS_DIR",
    "TokenThrottle",
    "execute_scenario",
    "git_sha",
    "run_benchmark",
    "write_latest_md",
]
