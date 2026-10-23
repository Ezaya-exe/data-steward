"""Typer CLI for dqs.

Current surface (docs/ARCHITECTURE.md §15 defines the full target surface,
added phase by phase — commands are not invented here):

- `dqs db init | reset | ping`
- `dqs dev up | down | lint | test | test-int | check` (make-equivalent,
  owner-requested; see docs/DECISIONS.md)
- `dqs sim seed [--days N] [--seed S] | day --date YYYY-MM-DD [--no-dbt]`  (Phase 1)
- `dqs dbt build [--target dev|sandbox]`                                    (Phase 1)
- `dqs verify <plan.json> | cleanup`                                        (Phase 5a)
- `dqs incident plan-fix <id>`                                              (Phase 5b)
- `dqs incident run|approve|reject|status <id>`                              (Phase 6b)
- `dqs demo [--fault SPEC] [--yes] [--reset]`                                (Phase 6b)
- `dqs publish preview <id>`                                                (Phase 6a, dry-run only)

Errors from the layer below are caught and printed; the process exit code is
non-zero so scripts/CI can rely on it.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine

from dqs import db as dbmod
from dqs import dbt_run
from dqs import demo as demo_mod
from dqs import dev as devmod
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.agents import fixer as fixer_mod
from dqs.agents.investigator import NODE_NAME, investigate
from dqs.agents.llm import BudgetTracker, LLMClient
from dqs.agents.smoke import run_smoke
from dqs.agents.steps import DbStepSink
from dqs.bench.ledger import DEFAULT_LEDGER_PATH
from dqs.bench.preflight import run_preflight
from dqs.bench.report import load_run_file, render_markdown
from dqs.bench.runner import RESULTS_DIR, BenchOptions, run_benchmark
from dqs.checks.engine import CheckEngine
from dqs.config import Settings, load_settings
from dqs.contracts import generation as contracts_generation
from dqs.contracts import store as contracts_store
from dqs.errors import DqsError, FaultSpecError
from dqs.lineage.graph import LineageGraph
from dqs.lineage.impact import build_impact_report
from dqs.models.contract import Contract
from dqs.models.faults import FaultSpec
from dqs.models.fix import FixPlan
from dqs.models.impact import ImpactReport
from dqs.models.root_cause import RootCause
from dqs.models.state import WorkflowOutcome
from dqs.models.violation import Violation
from dqs.publish.dry_run import preview_paths, run_dry_run
from dqs.publish.store import DbPublicationStore, load_latest_fix_attempt
from dqs.resolve import plan_resolution
from dqs.sim import pipeline as sim_pipeline
from dqs.sim.faults import parse_fault_specs
from dqs.tools.base import ToolContext
from dqs.verify.sandbox import cleanup_stale_sandboxes, verify_fix
from dqs.workflow.runner import DEFAULT_CHECKPOINT_PATH, IncidentWorkflow, make_deps

app = typer.Typer(name="dqs", help="Agentic data-quality steward for batch pipelines.")
db_app = typer.Typer(help="Warehouse bootstrap commands (roles, schemas, dq tables).")
dev_app = typer.Typer(help="Developer wrappers around docker compose / pytest / ruff / mypy.")
sim_app = typer.Typer(help="Synthetic baseline simulator (generate + load + dbt build).")
dbt_app = typer.Typer(help="Run dbt against the warehouse warehouse/dbt_project.")
contracts_app = typer.Typer(help="Data contract generation and approval (§7).")
check_app = typer.Typer(help="Run the deterministic check engine by hand (§7.3).")
monitor_app = typer.Typer(help="The batch monitor: run checks and record incidents (§8).")
incidents_app = typer.Typer(help="Inspect recorded incidents.")
incident_app = typer.Typer(help="Run one incident workflow node by hand (Phase 4+).")
llm_app = typer.Typer(help="LLM provider checks (Phase 4).")
publish_app = typer.Typer(help="Preview verified fixes locally (dry-run only, no GitHub call).")
bench_app = typer.Typer(help="Fault-injection benchmark: scenarios, runs and reports (Phase 8).")
app.add_typer(db_app, name="db")
app.add_typer(dev_app, name="dev")
app.add_typer(sim_app, name="sim")
app.add_typer(dbt_app, name="dbt")
app.add_typer(contracts_app, name="contracts")
app.add_typer(check_app, name="check")
app.add_typer(monitor_app, name="monitor")
app.add_typer(incidents_app, name="incidents")
app.add_typer(incident_app, name="incident")
app.add_typer(llm_app, name="llm")
app.add_typer(publish_app, name="publish")
app.add_typer(bench_app, name="bench")

console = Console()


@db_app.command()
def init() -> None:
    """Create roles, schemas and dq tables (idempotent — safe to re-run)."""
    try:
        dbmod.init_database(load_settings())
    except DqsError as exc:
        _fail(exc)
    console.print("[green]ok[/green] roles, schemas and dq tables are in place")


@db_app.command()
def reset() -> None:
    """Drop all steward schemas (raw/staging/marts/dq) and re-init."""
    try:
        dbmod.reset_database(load_settings())
    except DqsError as exc:
        _fail(exc)
    console.print("[green]ok[/green] schemas dropped and re-initialized")


@db_app.command()
def ping() -> None:
    """Print the server version answered by the rw and ro roles."""
    try:
        versions = dbmod.ping(load_settings())
    except DqsError as exc:
        _fail(exc)
    for role, version in versions.items():
        console.print(f"{role}: {version}")


@sim_app.command()
def seed(
    days: int = typer.Option(30, help="Clean baseline batches starting 2026-01-01."),
    seed_value: int | None = typer.Option(
        None, "--seed", help="Generator seed; defaults to DQS_SEED from settings."
    ),
) -> None:
    """Generate + load + dbt-build one clean batch per day (day 1 = initial dims)."""
    settings = load_settings()
    seed = seed_value if seed_value is not None else settings.dqs_seed
    console.print(f"seeding {days} baseline days (seed={seed}) …")
    try:
        results = sim_pipeline.seed_baseline(settings, days, seed)
    except DqsError as exc:
        _fail(exc)
    for entry in results:
        dbt_mark = "" if entry["dbt_exit"] is None else f" dbt_exit={entry['dbt_exit']}"
        console.print(f"  {entry['date']} rows={entry['rows']}{dbt_mark}")
    failed = [e for e in results if e["dbt_exit"] not in (None, 0)]
    if failed:
        console.print(f"[red]{len(failed)} dbt builds failed[/red]")
        raise typer.Exit(1)
    console.print("[green]ok[/green] baseline seeded")


@sim_app.command()
def day(
    date: str = typer.Option(..., help="Business date, YYYY-MM-DD."),
    fault_specs: Annotated[
        list[str] | None,
        typer.Option("--fault", help="Fault spec (repeatable): kind:table[.column][:k=v,...]."),
    ] = None,  # typer: the repeatable Option lives in the annotation
    dbt_build: bool = typer.Option(True, "--dbt/--no-dbt", help="Run dbt build after loading."),
    seed_value: int | None = typer.Option(
        None, "--seed", help="Generator seed; defaults to DQS_SEED from settings."
    ),
) -> None:
    """Generate + load one day's batch (with --fault specs), then run dbt build."""
    settings = load_settings()
    seed = seed_value if seed_value is not None else settings.dqs_seed
    try:
        business_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise typer.BadParameter(f"--date must be YYYY-MM-DD ({exc})") from exc
    try:
        specs: list[FaultSpec] = parse_fault_specs(fault_specs)
    except FaultSpecError as exc:
        _fail(exc)
    console.print(f"loading day {business_date} (seed={seed}, {len(specs)} fault specs) …")
    try:
        result = sim_pipeline.load_day(
            settings, business_date, seed, run_dbt=dbt_build, specs=specs
        )
    except DqsError as exc:
        _fail(exc)
    dbt_mark = "" if result["dbt_exit"] is None else f" dbt_exit={result['dbt_exit']}"
    console.print(f"  rows={result['rows']}{dbt_mark}")
    for truth in result["ground_truth"]:
        target = truth.table if truth.column is None else f"{truth.table}.{truth.column}"
        console.print(
            f"  fault {truth.kind.value} on {target} -> "
            f"expects {', '.join(truth.expected_checks)} "
            f"({truth.expected_category.value}/{truth.expected_resolution.value})"
        )
    if result["dbt_exit"] not in (None, 0):
        raise typer.Exit(result["dbt_exit"] or 1)
    console.print(f"[green]ok[/green] day {business_date} loaded")


@dbt_app.command()
def build(
    target: str = typer.Option("dev", help="dev | sandbox"),
) -> None:
    """Run dbt build against the warehouse and record the run in dq.pipeline_runs."""
    if target not in ("dev", "sandbox"):
        raise typer.BadParameter("--target must be 'dev' or 'sandbox'")
    try:
        code, output = dbt_run.run_dbt_build(load_settings(), target=target)
    except DqsError as exc:
        _fail(exc)
    console.print(_tail(output, lines=20))
    raise typer.Exit(code)


@contracts_app.command("generate")
def contracts_generate(
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace approved contracts whose regenerated content differs (writes drafts).",
    ),
) -> None:
    """Profile the current baseline and (re)write contract YAMLs (§7.2).

    Deterministic: two runs over the same baseline produce byte-identical
    files (Phase 2 acceptance), so this is safe to re-run at any time. An
    identical regeneration preserves `status: approved`; changed content over
    an approved file is refused unless `--force` is given.
    """
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        contracts = contracts_generation.generate_all(engine)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    directory = Path.cwd() / contracts_store.CONTRACTS_DIRNAME
    try:
        written = contracts_store.save_generated_many(contracts, directory, force=force)
    except DqsError as exc:
        _fail(exc)
    except OSError as exc:
        _fail(DqsError(f"couldn't write contract file: {exc}"))
    for path in written:
        contract = contracts_store.load_contract(path)
        pk = ",".join(contract.primary_key) or "-"
        console.print(
            f"  {contract.table} cols={len(contract.columns)} pk=[{pk}] "
            f"volume={contract.volume.min_rows}..{contract.volume.max_rows} "
            f"status={contract.status.value}",
            markup=False,
        )
    console.print(f"[green]ok[/green] {len(written)} contracts in {directory}")


@contracts_app.command()
def show(
    table: str = typer.Argument(..., help="Qualified table, e.g. raw.orders."),
) -> None:
    """Print one contract from contracts/ (draft files are the visible artifact)."""
    directory = Path.cwd() / contracts_store.CONTRACTS_DIRNAME
    path = contracts_store.contract_path(directory, table)
    try:
        contract = contracts_store.load_contract(path)
    except DqsError as exc:
        _fail(exc)
    console.print(path)
    for name, spec in contract.columns.items():
        flags = []
        if spec.unique:
            flags.append("unique")
        if spec.references is not None:
            flags.append(f"fk->{spec.references.table}.{spec.references.column}")
        if spec.allowed_values is not None:
            flags.append(f"values={spec.allowed_values}")
        if spec.range is not None:
            flags.append(f"range={spec.range.min}..{spec.range.max}")
        if spec.mean_bounds is not None:
            flags.append(f"mean={spec.mean_bounds.min}..{spec.mean_bounds.max}")
        null = "nullable" if spec.nullable else "not-null"
        console.print(f"  {name}: {spec.logical_type.value} {null} {' '.join(flags)}", markup=False)
    pk = ",".join(contract.primary_key) or "-"
    console.print(
        f"  volume {contract.volume.min_rows}..{contract.volume.max_rows} "
        f"(required_in_batch={contract.volume.required_in_batch}), "
        f"freshness {contract.freshness.max_delay_minutes}min, "
        f"pk=[{pk}], status={contract.status.value}",
        markup=False,
    )


@contracts_app.command()
def approve(
    table: str | None = typer.Argument(None, help="Qualified table; omit with --all."),
    every: bool = typer.Option(False, "--all", help="Approve all contracts."),
) -> None:
    """Approve contracts (the human gate before checks run — §7.2)."""
    if table is None and not every:
        raise typer.BadParameter("pass a table name or --all")
    directory = Path.cwd() / contracts_store.CONTRACTS_DIRNAME
    tables = None if table is None else [table]
    try:
        rewritten = contracts_store.approve(directory, tables)
    except DqsError as exc:
        _fail(exc)
    for path in rewritten:
        console.print(f"  approved {contracts_store.load_contract(path).table}")
    if not rewritten:
        console.print("[yellow]no matching contracts found[/yellow]")
    else:
        console.print(f"[green]ok[/green] {len(rewritten)} approved")


@check_app.command("batch")
def check_batch(
    batch_id: str = typer.Argument(..., help="Batch id, e.g. batch_20260131."),
    as_of: str | None = typer.Option(None, "--as-of", help="ISO-8601 instant (informational)."),
) -> None:
    """Run the check engine for one batch and print the violations (§7.3)."""
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        check_engine = _check_engine(engine)
        moment = monitor_mod.parse_as_of(as_of) if as_of is not None else None
        violations = check_engine.run(batch_id, moment)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    _print_violations(violations)


@monitor_app.command("run")
def monitor_run(
    as_of: str | None = typer.Option(
        None, "--as-of", help="ISO-8601 instant (default: watermark)."
    ),
) -> None:
    """Detect on unchecked batches + missing batches, record incidents (§8)."""
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        contracts = _approved_contracts()
        moment = monitor_mod.parse_as_of(as_of) if as_of is not None else None
        result = monitor_mod.run_monitor(engine, contracts, moment)
        incidents = {
            incident_id: incidents_mod.get_incident(engine, incident_id)
            for incident_id in (*result.created, *result.updated)
        }
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    console.print(f"as-of {result.as_of.isoformat()}")
    console.print(f"batches checked: {len(result.batches_checked)}")
    for label, ids in (("created", result.created), ("updated", result.updated)):
        for incident_id in ids:
            incident = incidents[incident_id]
            table = incident.table if incident is not None else "-"
            severity = incident.severity.value if incident is not None else "-"
            console.print(
                f"  {label} {str(incident_id)[:8]} {severity} {table}",
                markup=False,
            )
    if result.missing_batches:
        days = ", ".join(day.isoformat() for day in result.missing_batches)
        console.print(f"[red]missing batches[/red] {days}")
    if not result.created and not result.updated and not result.missing_batches:
        console.print("[green]ok[/green] no incidents")


@incidents_app.command("list")
def incidents_list(
    status: str | None = typer.Option(None, "--status", help="Filter by incident status."),
) -> None:
    """List recorded incidents (newest first)."""
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        items = incidents_mod.list_incidents(engine, status)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    table = Table(show_header=True, header_style="bold")
    for column in ("id", "status", "severity", "fingerprint", "table", "violations", "updated_at"):
        table.add_column(column)
    for incident in items:
        table.add_row(
            str(incident.incident_id)[:8],
            incident.status.value,
            incident.severity.value,
            incident.fingerprint[:8],
            incident.table or "-",
            str(len(incident.violations)),
            incident.updated_at.isoformat(),
        )
    console.print(table)
    console.print(f"{len(items)} incident(s)")


@incidents_app.command("show")
def incidents_show(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
) -> None:
    """Print one incident with its violations and de-duplicated batch list."""
    try:
        identifier = uuid.UUID(incident_id)
    except ValueError as exc:
        raise typer.BadParameter(f"incident id must be a uuid ({exc})") from exc
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        incident = incidents_mod.get_incident(engine, identifier)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    if incident is None:
        _fail(DqsError(f"no incident with id {incident_id}"))
    console.print(
        f"incident {incident.incident_id} [{incident.severity.value}] {incident.status.value}"
    )
    console.print(f"  fingerprint {incident.fingerprint}")
    console.print(f"  table {incident.table or '-'}  batches {', '.join(incident.batches) or '-'}")
    console.print(
        f"  created {incident.created_at.isoformat()}  updated {incident.updated_at.isoformat()}"
    )
    _print_violations(incident.violations)


@llm_app.command("smoke")
def llm_smoke(
    provider: str | None = typer.Option(None, help="Provider override (default: LLM_PROVIDER)."),
    model: str | None = typer.Option(None, help="Model override (default: LLM_MODEL)."),
) -> None:
    """Run four provider probes: completion, tool call, structured output, 3-step loop."""
    settings = load_settings()
    chosen_provider = provider or settings.llm_provider
    chosen_model = model or settings.llm_model
    try:
        report = run_smoke(chosen_provider, chosen_model, settings=settings)
    except DqsError as exc:
        _fail(exc)
    console.print(f"llm smoke provider={report.provider} model={report.model}")
    table = Table(show_header=True, header_style="bold")
    for column in ("probe", "result", "latency_ms", "tokens_in", "tokens_out", "detail"):
        table.add_column(column)
    for result in report.results:
        table.add_row(
            result.name,
            "PASS" if result.passed else "FAIL",
            str(result.latency_ms),
            str(result.input_tokens),
            str(result.output_tokens),
            result.detail,
        )
    console.print(table)
    if not report.passed:
        console.print("[red]one or more probes failed[/red]")
        raise typer.Exit(1)
    console.print("[green]ok[/green] all four probes passed")


@incident_app.command("investigate")
def incident_investigate(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
    provider: str | None = typer.Option(None, help="Provider override (default: LLM_PROVIDER)."),
    model: str | None = typer.Option(None, help="Model override (default: LLM_MODEL)."),
) -> None:
    """Run only the investigate node, persist its RootCause and print the timeline (§10.3)."""
    try:
        identifier = uuid.UUID(incident_id)
    except ValueError as exc:
        raise typer.BadParameter(f"incident id must be a uuid ({exc})") from exc
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    context: ToolContext | None = None
    try:
        incident = incidents_mod.get_incident(engine, identifier)
        if incident is None:
            _fail(DqsError(f"no incident with id {incident_id}"))
        chosen_provider = provider or settings.llm_provider
        chosen_model = model or settings.llm_model
        budget = BudgetTracker.from_settings(settings)
        client = LLMClient.from_provider(
            chosen_provider, chosen_model, settings=settings, budget=budget
        )
        context = ToolContext.from_settings(settings)
        sink = DbStepSink(engine, node=NODE_NAME, incident_id=identifier, settings=settings)
        result = investigate(incident, client=client, context=context, sink=sink, budget=budget)
        incidents_mod.save_root_cause(engine, identifier, result.root_cause.model_dump(mode="json"))
    except DqsError as exc:
        _fail(exc)
    finally:
        if context is not None:
            context.close()
        engine.dispose()

    console.print_json(data=result.root_cause.model_dump(mode="json"))
    console.print(
        f"provider={chosen_provider} model={chosen_model} channel={result.final_channel} "
        f"tool_calls={result.tool_calls} tokens={result.tokens} retries={result.retries} "
        f"budget_exceeded={result.budget_exceeded}"
    )
    console.print(f"steps={len(result.steps)} (timeline below; persisted in dq.agent_steps)")
    table = Table(show_header=True, header_style="bold")
    for column in ("seq", "kind", "name", "tokens_in", "tokens_out", "latency_ms", "output"):
        table.add_column(column)
    for step in result.steps:
        preview = json.dumps(step.output, ensure_ascii=False, sort_keys=True)[:120]
        table.add_row(
            str(step.seq),
            step.kind,
            step.name,
            str(step.tokens_in),
            str(step.tokens_out),
            str(step.latency_ms),
            preview,
        )
    console.print(table)


@incident_app.command("plan-fix")
def incident_plan_fix(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
    provider: str | None = typer.Option(None, help="Provider override (default: LLM_PROVIDER)."),
    model: str | None = typer.Option(None, help="Model override (default: LLM_MODEL)."),
) -> None:
    """Plan and verify a fix for one investigated incident (§10.5, §11).

    The command refuses to plan when deterministic routing says the incident is
    not PR-eligible. Every attempt is persisted to `dq.fix_attempts` and every
    model call to `dq.agent_steps`, so a failed run leaves a complete timeline.
    """
    try:
        identifier = uuid.UUID(incident_id)
    except ValueError as exc:
        raise typer.BadParameter(f"incident id must be a uuid ({exc})") from exc
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        incident = incidents_mod.get_incident(engine, identifier)
        if incident is None:
            _fail(DqsError(f"no incident with id {incident_id}"))
        if incident.root_cause is None:
            _fail(
                DqsError(f"incident {incident_id} has no stored root cause; run investigate first")
            )
        if incident.impact is None:
            _fail(DqsError(f"incident {incident_id} has no stored impact report; run impact first"))
        try:
            root_cause = RootCause.model_validate(incident.root_cause)
            impact = ImpactReport.model_validate(incident.impact)
        except ValidationError as exc:
            _fail(DqsError(f"stored incident artifacts do not validate:\n{exc}"))

        candidates = fixer_mod.select_candidates(incident, root_cause)
        resolution = plan_resolution(root_cause.category, root_cause.confidence, bool(candidates))
        console.print(
            f"incident {str(identifier)[:8]} category={root_cause.category.value} "
            f"confidence={root_cause.confidence:.2f} "
            f"candidates={', '.join(candidate.name for candidate in candidates) or '-'} "
            f"resolution={resolution.value}",
            markup=False,
        )
        if resolution.value != "PR":
            console.print(
                f"[yellow]routing says {resolution.value}[/yellow] — plan-fix only runs for "
                "PR-eligible incidents; file an issue instead"
            )
            raise typer.Exit(1)

        chosen_provider = provider or settings.llm_provider
        chosen_model = model or settings.llm_model
        budget = BudgetTracker.from_settings(settings)
        client = LLMClient.from_provider(
            chosen_provider, chosen_model, settings=settings, budget=budget
        )
        sink = DbStepSink(
            engine, node=fixer_mod.NODE_NAME, incident_id=identifier, settings=settings
        )
        result = fixer_mod.plan_fix(
            incident,
            root_cause,
            impact,
            client=client,
            sink=sink,
            settings=settings,
            engine=engine,
            candidates=candidates,
        )
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()

    console.print(
        f"provider={chosen_provider} model={chosen_model} attempts={result.attempts_used} "
        f"tokens={result.tokens} retries={result.retries} "
        f"budget_exceeded={result.budget_exceeded}"
    )
    attempts = Table(show_header=True, header_style="bold")
    for column in ("attempt", "verdict", "failed criteria", "plan", "duration_s"):
        attempts.add_column(column)
    for attempt in result.attempts:
        verification = attempt.verification
        summary = attempt.plan.summary if attempt.plan is not None else "-"
        attempts.add_row(
            str(attempt.attempt_no),
            "PASS" if verification.passed else "FAIL",
            ", ".join(verification.failed_criteria) or "-",
            summary[:100],
            f"{verification.duration_s:.1f}",
        )
    console.print(attempts)

    final = result.final_verification
    if final is not None and final.criteria:
        criteria = Table(show_header=True, header_style="bold")
        for column in ("criterion", "verdict", "detail"):
            criteria.add_column(column)
        for criterion in final.criteria:
            criteria.add_row(
                criterion.name,
                "PASS" if criterion.passed else "FAIL",
                criterion.detail[:140],
            )
        console.print(criteria)

    if not result.passed:
        if result.budget_exceeded:
            console.print("[red]FAIL[/red] the model budget was exhausted before a fix passed")
        else:
            console.print(
                f"[red]FAIL[/red] {result.attempts_used} attempt(s) exhausted without a "
                "passing fix; all attempts are stored in dq.fix_attempts"
            )
        if final is not None and final.dbt_log_excerpt:
            console.print("[bold]dbt excerpt[/bold]")
            console.print(final.dbt_log_excerpt)
        raise typer.Exit(1)

    passed_plan = result.attempts[-1].plan
    if passed_plan is not None:
        console.print_json(data=passed_plan.model_dump(mode="json"))
    console.print(
        f"[green]PASS[/green] fix verified in attempt {result.attempts[-1].attempt_no}; "
        "plan and attempt are persisted"
    )


@incident_app.command("run")
def incident_run(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
    auto_approve: bool = typer.Option(
        False,
        "--auto-approve",
        help="Approve without a human (benchmark/demo only; labeled in the PR body).",
    ),
    provider: str | None = typer.Option(None, help="Provider override (default: LLM_PROVIDER)."),
    model: str | None = typer.Option(None, help="Model override (default: LLM_MODEL)."),
) -> None:
    """Run the whole §10.2 workflow until it publishes or waits for approval."""
    identifier = _parse_incident_id(incident_id)
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    context: ToolContext | None = None
    try:
        context = ToolContext.from_settings(settings)
        deps = make_deps(settings, engine, provider=provider, model=model, tool_context=context)
        with IncidentWorkflow(deps) as workflow:
            outcome = workflow.run(identifier, auto_approve=auto_approve)
    except DqsError as exc:
        _fail(exc)
    finally:
        if context is not None:
            context.close()
        engine.dispose()
    _print_workflow_outcome(outcome)
    if outcome.status.value == "failed":
        raise typer.Exit(1)


def _workflow_for(settings: Settings, engine: Engine) -> tuple[IncidentWorkflow, ToolContext]:
    """Build the production deps and a resumable workflow for one CLI call."""
    context = ToolContext.from_settings(settings)
    deps = make_deps(settings, engine, tool_context=context)
    return IncidentWorkflow(deps), context


@incident_app.command("approve")
def incident_approve(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
    note: str = typer.Option("", "--note", help="Optional note recorded with the approval."),
    approver: str | None = typer.Option(
        None, "--approver", help="Who approved (default: OS user name or 'operator')."
    ),
) -> None:
    """Approve the artifact built by the paused workflow; only then is it published."""
    identifier = _parse_incident_id(incident_id)
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    context: ToolContext | None = None
    try:
        workflow, context = _workflow_for(settings, engine)
        with workflow:
            outcome = workflow.resume(
                identifier,
                decision="approve",
                approver=_approver_name(approver),
                note=note,
            )
    except DqsError as exc:
        _fail(exc)
    finally:
        if context is not None:
            context.close()
        engine.dispose()
    _print_workflow_outcome(outcome)
    if outcome.status.value == "failed":
        raise typer.Exit(1)


@incident_app.command("reject")
def incident_reject(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
    note: str = typer.Option(
        ..., "--note", help="Why the artifact was rejected (required, kept in the audit trail)."
    ),
    approver: str | None = typer.Option(
        None, "--approver", help="Who rejected (default: OS user name or 'operator')."
    ),
) -> None:
    """Reject the pending artifact; nothing is published and the reason is stored."""
    identifier = _parse_incident_id(incident_id)
    if not note.strip():
        raise typer.BadParameter("--note must not be empty")
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    context: ToolContext | None = None
    try:
        workflow, context = _workflow_for(settings, engine)
        with workflow:
            outcome = workflow.resume(
                identifier,
                decision="reject",
                approver=_approver_name(approver),
                note=note,
            )
    except DqsError as exc:
        _fail(exc)
    finally:
        if context is not None:
            context.close()
        engine.dispose()
    _print_workflow_outcome(outcome)
    if outcome.status.value == "failed":
        raise typer.Exit(1)


@incident_app.command("status")
def incident_status(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
) -> None:
    """Print the stored lifecycle state, resolution and checkpoint location."""
    identifier = _parse_incident_id(incident_id)
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        incident = incidents_mod.get_incident(engine, identifier)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    if incident is None:
        _fail(DqsError(f"no incident with id {incident_id}"))
    console.print(
        f"incident {str(incident.incident_id)[:8]} status={incident.status.value} "
        f"severity={incident.severity.value} table={incident.table or '-'}"
    )
    console.print(f"  updated {incident.updated_at.isoformat()}")
    resolution = incident.resolution or {}
    if resolution:
        console.print_json(data=resolution)
    else:
        console.print("  resolution: (none recorded yet)")
    checkpoint = Path(DEFAULT_CHECKPOINT_PATH)
    console.print(f"  checkpoint {checkpoint} ({'present' if checkpoint.exists() else 'missing'})")
    if incident.status.value == "awaiting_approval":
        console.print(
            f"  next: dqs incident approve {incident.incident_id} "
            f"or dqs incident reject {incident.incident_id} --note TEXT"
        )


def _parse_incident_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"incident id must be a uuid ({exc})") from exc


def _approver_name(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    import getpass

    try:
        return getpass.getuser() or "operator"
    except Exception:  # getuser can fail in minimal containers
        return "operator"


def _print_workflow_outcome(outcome: WorkflowOutcome) -> None:
    """One compact, honest summary for run/approve/reject."""
    console.print(
        f"incident {str(outcome.incident_id)[:8]} status={outcome.status.value} "
        f"resolution={outcome.resolution_type.value if outcome.resolution_type else '-'} "
        f"attempts={outcome.attempts} checkpoint={outcome.checkpoint_path}",
        markup=False,
    )
    if outcome.error:
        console.print(f"[red]failed[/red] {outcome.error}")
        return
    budget = outcome.state.get("budget")
    if isinstance(budget, dict):
        console.print(
            f"  budget tool_calls={budget.get('tool_calls_used')} "
            f"tokens={budget.get('tokens_used')} elapsed_s={budget.get('elapsed_seconds')} "
            f"limits calls={budget.get('max_tool_calls')} tokens={budget.get('max_tokens')} "
            f"timeout_s={budget.get('timeout_seconds')}"
        )
    if outcome.interrupted:
        request = outcome.approval_request or {}
        kind = request.get("kind", "artifact")
        console.print(
            f"[yellow]waiting for approval[/yellow] {kind} "
            f"(resolution={request.get('resolution', '-')}, "
            f"verification_attempts={request.get('attempts', 0)})"
        )
        root_cause = request.get("root_cause") or {}
        impact = request.get("impact") or {}
        if root_cause:
            console.print(f"  root cause: {root_cause.get('summary', '-')}")
        if impact:
            console.print(f"  impact: {impact.get('summary', '-')}")
        verification = request.get("verification") or {}
        if verification:
            console.print(
                f"  verification: passed={verification.get('passed')} "
                f"failed={', '.join(verification.get('failed_criteria') or []) or '-'}"
            )
        diff = request.get("diff")
        if diff:
            console.print("[bold]diff (truncated)[/bold]")
            console.print("\n".join(str(diff).splitlines()[:60]), markup=False)
        console.print(
            f"  next: dqs incident approve {outcome.incident_id} "
            f"or dqs incident reject {outcome.incident_id} --note TEXT"
        )
    if outcome.publication is not None:
        console.print(
            f"[green]published[/green] {outcome.publication.kind.value} "
            f"({outcome.publication.mode.value}) {outcome.publication.url}"
        )


@app.command("demo")
def demo(
    fault: Annotated[
        list[str] | None,
        typer.Option(
            "--fault",
            help=(
                "Fault spec (repeatable), e.g. "
                "column_rename:orders.customer_id:new_name=cust_id. "
                f"Default: {demo_mod.DEFAULT_FAULT}"
            ),
        ),
    ] = None,
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Approve without a prompt (benchmark/demo only; visibly labeled in the artifact).",
    ),
    reset: bool = typer.Option(
        False, "--reset", help=f"Drop and recreate {demo_mod.DEMO_DB} before seeding."
    ),
) -> None:
    """Run the whole break → detect → diagnose → verify → preview story.

    The demo owns a **separate** database (`dqs_demo`); the warehouse configured
    in `.env` is never read or written. Publication follows `GITHUB_MODE`, so
    the default is a local PR preview under `out/pr_previews/`.
    """
    settings = load_settings()
    chosen = list(fault) if fault else [demo_mod.DEFAULT_FAULT]
    console.print(
        f"demo database={demo_mod.DEMO_DB} warehouse={settings.pg_database} "
        f"fault={' + '.join(chosen)} mode={settings.github_mode}",
        markup=False,
    )
    if yes:
        console.print(
            "[yellow]auto-approve[/yellow] enabled (--yes): the published artifact "
            "will be labeled as auto-approved"
        )

    def progress(message: str) -> None:
        console.print(f"[dim]{message}[/dim]")

    try:
        result = demo_mod.run_demo(
            settings,
            fault_specs=chosen,
            auto_approve=yes,
            reset=reset,
            approver="auto",
            approval_callback=None if yes else _demo_approval_callback,
            progress=progress,
        )
    except DqsError as exc:
        _fail(exc)

    _print_demo_result(result)
    if result.outcome.status.value == "failed":
        raise typer.Exit(1)


def _demo_approval_callback(request: dict[str, Any]) -> tuple[bool, str]:
    """Interactive approval prompt for the demo (never used with `--yes`)."""
    kind = request.get("kind", "artifact")
    resolution = request.get("resolution", "-")
    attempts = request.get("attempts", 0)
    console.print(
        f"[yellow]approval required[/yellow] {kind} "
        f"(resolution={resolution}, verification_attempts={attempts})"
    )
    root_cause = request.get("root_cause") or {}
    if root_cause:
        console.print(f"  root cause: {root_cause.get('summary', '-')}")
    impact = request.get("impact") or {}
    if impact:
        console.print(f"  impact: {impact.get('summary', '-')}")
    verification = request.get("verification") or {}
    if verification:
        console.print(
            f"  verification: passed={verification.get('passed')} "
            f"failed={', '.join(verification.get('failed_criteria') or []) or '-'}"
        )
    diff = request.get("diff")
    if diff:
        console.print("[bold]diff (truncated)[/bold]")
        console.print("\n".join(str(diff).splitlines()[:60]), markup=False)
    if not typer.confirm("Approve and publish this artifact?", default=False):
        note = typer.prompt(
            "Reason for rejection", default="rejected by the demo operator", show_default=True
        )
        return False, note
    note = typer.prompt("Approval note (optional)", default="", show_default=False)
    return True, note


def _print_demo_result(result: demo_mod.DemoResult) -> None:
    """Transcript: incident, timeline, and the exact preview path(s)."""
    outcome = result.outcome
    console.print(
        f"incident {str(outcome.incident_id)[:8]} status={outcome.status.value} "
        f"resolution={outcome.resolution_type.value if outcome.resolution_type else '-'} "
        f"attempts={outcome.attempts}",
        markup=False,
    )
    console.print(
        f"  database={result.database} baseline_days={result.baseline_days} "
        f"fault_date={result.fault_date.isoformat()} faults={', '.join(result.fault_specs)}",
        markup=False,
    )
    console.print(f"  monitor created {len(result.created_incidents)} incident(s)")
    if result.steps:
        table = Table(show_header=True, header_style="bold")
        for column in ("node", "seq", "kind", "name", "tokens", "latency_ms"):
            table.add_column(column)
        for step in result.steps:
            table.add_row(
                step.node,
                str(step.seq),
                step.kind,
                step.name,
                f"{step.tokens_in}+{step.tokens_out}",
                str(step.latency_ms),
            )
        console.print(table)
    else:
        console.print("  timeline: no agent steps recorded")
    if outcome.publication is not None:
        publication = outcome.publication
        console.print(
            f"[green]published[/green] {publication.kind.value} "
            f"({publication.mode.value}) {publication.url}"
        )
        if result.preview_md is not None:
            console.print(f"  body:  {result.preview_md}", markup=False)
        if result.preview_patch is not None:
            console.print(f"  patch: {result.preview_patch}", markup=False)
    else:
        console.print("[yellow]nothing published[/yellow] (rejected or no artifact)")
    if outcome.error:
        console.print(f"[red]failed[/red] {outcome.error}", markup=False)


@publish_app.command("preview")
def publish_preview(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
) -> None:
    """Write a dry-run PR preview from persisted root cause, fix attempt and impact.

    This command is deliberately dry-run only: it refuses `GITHUB_MODE=live`
    before opening a database connection, and it never constructs a GitHub
    client. The preview is recorded in `dq.publications` with `mode=dry_run`.
    """
    try:
        identifier = uuid.UUID(incident_id)
    except ValueError as exc:
        raise typer.BadParameter(f"incident id must be a uuid ({exc})") from exc
    settings = load_settings()
    if settings.github_mode != "dry_run":
        _fail(
            DqsError(
                "dqs publish preview refuses GITHUB_MODE=live: this command only writes "
                "a local preview and never calls GitHub"
            )
        )
    engine = dbmod.make_rw_engine(settings)
    try:
        incident = incidents_mod.get_incident(engine, identifier)
        if incident is None:
            _fail(DqsError(f"no incident with id {incident_id}"))
        if incident.root_cause is None:
            _fail(
                DqsError(f"incident {incident_id} has no stored root cause; run investigate first")
            )
        if incident.impact is None:
            _fail(DqsError(f"incident {incident_id} has no stored impact report; run impact first"))
        try:
            # Validate the stored artifacts now; the body renders honest
            # "not recorded" text for absent data, but a preview built on
            # corrupt JSON would be misleading.
            RootCause.model_validate(incident.root_cause)
            ImpactReport.model_validate(incident.impact)
        except ValidationError as exc:
            _fail(DqsError(f"stored incident artifacts do not validate:\n{exc}"))

        loaded = load_latest_fix_attempt(engine, identifier)
        if loaded is None:
            _fail(
                DqsError(f"incident {incident_id} has no fix attempts; run incident plan-fix first")
            )
        plan, verification = loaded
        result = run_dry_run(
            plan,
            incident,
            verification,
            store=DbPublicationStore(engine),
            settings=settings,
        )
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()

    md_path, patch_path = preview_paths(identifier)
    console.print(
        f"mode={result.mode.value} kind={result.kind.value} reused={result.reused}",
        markup=False,
    )
    console.print(f"PR body: {md_path}", markup=False)
    console.print(f"Patch:   {patch_path}", markup=False)
    console.print(f"URL:     {result.url}", markup=False)


@app.command("impact")
def impact(
    incident_id: str = typer.Argument(..., help="Incident id (uuid)."),
) -> None:
    """Print the deterministic blast radius of one incident (ARCH §9).

    Reads the checked-out dbt manifest (regenerated via `dbt compile` when
    missing/stale), the stored failing-node record from `dq.pipeline_runs`
    and the incident's violations; persists the report into `dq.incidents.impact`.
    """
    try:
        identifier = uuid.UUID(incident_id)
    except ValueError as exc:
        raise typer.BadParameter(f"incident id must be a uuid ({exc})") from exc
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        incident = incidents_mod.get_incident(engine, identifier)
        if incident is None:
            _fail(DqsError(f"no incident with id {incident_id}"))
        graph = LineageGraph.from_manifest()
        record = dbt_run.latest_run_for_batch(engine, incident.batch_id)
        report = build_impact_report(graph, incident, record)
        incidents_mod.save_impact(engine, identifier, report.model_dump(mode="json"))
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()

    console.print(
        f"impact for {str(report.incident_id)[:8]} [{report.scope.value}] "
        f"source {report.source_table or '-'}"
        f"{' (column ' + report.column + ')' if report.column else ''}"
    )
    console.print(f"  summary: {report.summary}")
    table = Table(show_header=True, header_style="bold")
    for column in ("model", "layer", "distance", "column-lineage"):
        table.add_column(column)
    for model in report.affected_models:
        table.add_row(
            model.name,
            model.layer,
            str(model.distance),
            "yes" if model.referencing_column else "-",
        )
    console.print(table)
    if report.failing_tests:
        tests = Table(show_header=True, header_style="bold")
        for column in ("failing dbt node (stored record)", "status", "batch"):
            tests.add_column(column)
        for node in report.failing_tests:
            tests.add_row(node.name, node.status, node.batch_id or "-")
        console.print(tests)
    exposures = Table(show_header=True, header_style="bold")
    for column in ("exposure", "owner", "maturity"):
        exposures.add_column(column)
    for exposure in report.affected_exposures:
        exposures.add_row(exposure.name, exposure.owner or "-", exposure.maturity)
    console.print(exposures)
    console.print(f"[bold]blast radius score: {report.blast_radius_score}[/bold]")


@app.command("verify")
def verify(
    target: Annotated[str, typer.Argument(help="A fix-plan JSON path, or the word 'cleanup'.")],
) -> None:
    """Verify a fix plan in a sandbox, or drop stale sandbox schemas.

    `dqs verify <plan.json>` runs the §11 checks; `dqs verify cleanup` drops
    leftover `dqs_sandbox_*` schemas from an interrupted run.
    """
    if target.lower() == "cleanup":
        _verify_cleanup()
        return
    plan = Path(target)
    try:
        raw = plan.read_text(encoding="utf-8")
        loaded = FixPlan.model_validate_json(raw)
    except OSError as exc:
        raise typer.BadParameter(f"cannot read plan {plan}: {exc}") from exc
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid fix plan {plan}:\n{exc}") from exc

    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        result = verify_fix(settings, loaded, engine=engine)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()

    table = Table(show_header=True, header_style="bold")
    for column in ("criterion", "verdict", "detail"):
        table.add_column(column)
    for criterion in result.criteria:
        table.add_row(
            criterion.name,
            "PASS" if criterion.passed else "FAIL",
            criterion.detail[:140],
        )
    console.print(table)
    if result.row_counts:
        counts = " ".join(f"{key}={value}" for key, value in sorted(result.row_counts.items()))
        console.print(f"rows: {counts}")
    console.print(
        f"sandbox={result.sandbox_schema} dbt_exit={result.dbt_exit_code} "
        f"duration={result.duration_s:.1f}s tables={result.contract_tables}"
    )
    if result.dbt_log_excerpt and not result.passed:
        console.print("[bold]dbt excerpt[/bold]")
        console.print(result.dbt_log_excerpt)
    if result.passed:
        console.print("[green]PASS[/green] fix verified; attempt persisted in dq.fix_attempts")
    else:
        console.print(f"[red]FAIL[/red] failed criteria: {', '.join(result.failed_criteria)}")
        raise typer.Exit(1)


def _verify_cleanup() -> None:
    settings = load_settings()
    engine = dbmod.make_rw_engine(settings)
    try:
        dropped = cleanup_stale_sandboxes(engine)
    except DqsError as exc:
        _fail(exc)
    finally:
        engine.dispose()
    if dropped:
        console.print(f"[green]ok[/green] dropped {len(dropped)} schema(s): {', '.join(dropped)}")
    else:
        console.print("[green]ok[/green] no sandbox schemas remained")


def _check_engine(engine: Engine) -> CheckEngine:
    contracts = _approved_contracts()
    return CheckEngine(engine, contracts)


def _approved_contracts() -> dict[str, Contract]:
    directory = Path.cwd() / contracts_store.CONTRACTS_DIRNAME
    contracts = contracts_store.load_approved(directory)
    if not contracts:
        console.print(
            "[yellow]warning[/yellow] no approved contracts — "
            "run 'dqs contracts approve --all' (nothing will be enforced)"
        )
    return contracts


def _print_violations(violations: list[Violation]) -> None:
    if not violations:
        console.print("[green]ok[/green] no violations")
        return
    table = Table(show_header=True, header_style="bold")
    for column in ("check", "severity", "table", "column", "batch", "message"):
        table.add_column(column)
    for violation in violations:
        table.add_row(
            violation.check,
            violation.severity.value,
            violation.table or "-",
            violation.column or "-",
            violation.batch_id or "-",
            violation.message,
        )
    console.print(table)
    console.print(f"{len(violations)} violation(s)")


def _tail(output: str, lines: int) -> str:
    return "\n".join(output.splitlines()[-lines:])


@dev_app.command()
def up() -> None:
    """Start Postgres via docker compose and wait until healthy."""
    raise typer.Exit(devmod.up())


@dev_app.command()
def down() -> None:
    """Stop Postgres."""
    raise typer.Exit(devmod.down())


@dev_app.command()
def lint() -> None:
    """ruff check, ruff format --check and mypy on src/."""
    raise typer.Exit(devmod.lint())


@dev_app.command()
def test() -> None:
    """Run fast unit tests (`pytest -m "not integration"`)."""
    raise typer.Exit(devmod.test())


@dev_app.command()
def test_int() -> None:
    """Run integration tests (require `dqs dev up`)."""
    raise typer.Exit(devmod.test_int())


@dev_app.command()
def check() -> None:
    """Full quality gate: lint + test + test-int."""
    raise typer.Exit(devmod.check())


@bench_app.command("run")
def bench_run(
    split: str = typer.Option("dev", "--split", help="dev | holdout | all."),
    model: str | None = typer.Option(
        None, "--model", help="Model name; defaults to LLM_MODEL_BENCH."
    ),
    limit: int | None = typer.Option(None, "--limit", help="Run at most N scenarios."),
    ids: str | None = typer.Option(None, "--ids", help="Comma-separated scenario ids."),
    resume: Annotated[
        Path | None, typer.Option("--resume", help="Continue a results JSON file.")
    ] = None,
    no_dbt: bool = typer.Option(False, "--no-dbt", help="Skip the dbt build after loading."),
    daily_token_cap: int = typer.Option(180_000, "--daily-token-cap"),
    tpm_limit: int = typer.Option(8_000, "--tpm-limit"),
    pause: float = typer.Option(1.0, "--pause", help="Seconds between scenarios."),
    confirm_holdout: bool = typer.Option(
        False, "--confirm-holdout", help="Required to run the reporting-only holdout split."
    ),
    llm_retry_max_wait: float = typer.Option(
        90.0, "--llm-retry-max-wait", help="Total seconds a per-minute 429 may spend retrying."
    ),
    repeats: int = typer.Option(
        1, "--repeats", help="Run every selected scenario N times (flakiness check)."
    ),
    acknowledge_changes: str | None = typer.Option(
        None,
        "--acknowledge-changes",
        help="Reason for running holdout/all after the code fingerprint changed.",
    ),
    token_ledger: Annotated[
        Path | None, typer.Option("--token-ledger", help="Daily token ledger JSON path.")
    ] = None,
    results_dir: Annotated[Path | None, typer.Option("--results-dir")] = None,
) -> None:
    """Run fault-injection scenarios against one model (dry-run publishing)."""
    settings = load_settings()
    options = BenchOptions(
        model=model or settings.llm_model_bench,
        split=split,
        limit=limit,
        ids=[item for item in (ids or "").split(",") if item.strip()] or None,
        resume=resume,
        no_dbt=no_dbt,
        daily_token_cap=daily_token_cap,
        tpm_limit=tpm_limit,
        pause_seconds=pause,
        confirm_holdout=confirm_holdout,
        llm_retry_max_wait_seconds=llm_retry_max_wait,
        repeats=repeats,
        acknowledge_changes=acknowledge_changes,
        ledger_path=(token_ledger or DEFAULT_LEDGER_PATH),
        results_dir=(results_dir or RESULTS_DIR),
    )
    try:
        run, path = run_benchmark(
            settings, options, progress=lambda message: console.print(message)
        )
    except DqsError as exc:
        _fail(exc)
    aggregates = run.aggregates()
    console.print(
        f"[green]ok[/green] {len(run.scenarios)} scenario(s) written to {path}\n"
        f"  detection={aggregates.overall.detection_recall} "
        f"rc_category={aggregates.overall.rc_category_accuracy} "
        f"resolution={aggregates.overall.resolution_accuracy} "
        f"fp_rate={aggregates.overall.false_positive_rate}"
    )
    if run.metadata.stop_reason:
        console.print(f"[yellow]stopped:[/yellow] {run.metadata.stop_reason}")
        if run.metadata.resume_hint:
            console.print(f"resume: {run.metadata.resume_hint}")


@bench_app.command("preflight")
def bench_preflight(
    model: str | None = typer.Option(
        None, "--model", help="Model to check; defaults to LLM_MODEL_BENCH."
    ),
) -> None:
    """Read-only readiness checks before spending benchmark quota (no LLM calls)."""
    settings = load_settings()
    report = run_preflight(settings, model=model)
    typer.echo(report.render())
    if not report.ok:
        _fail(DqsError("preflight failed; fix the FAIL lines before `dqs bench run`"))
    typer.echo("preflight OK - safe to start a benchmark run")


@bench_app.command("report")
def bench_report(
    file: Annotated[
        Path | None, typer.Argument(help="Results JSON; default is the newest one.")
    ] = None,
) -> None:
    """Print the per-kind/overall markdown report for a results file."""
    target = file or _latest_results_file()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        run = load_run_file(payload)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _fail(DqsError(f"could not read results file {target}: {exc}"))
    typer.echo(render_markdown(run))


def _latest_results_file() -> Path:
    candidates = sorted(
        RESULTS_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    if not candidates:
        _fail(DqsError(f"no benchmark results in {RESULTS_DIR}; run `dqs bench run` first"))
    return candidates[0]


def _fail(exc: DqsError) -> NoReturn:
    """Print one clear error line and exit 1 (never swallow, AGENTS.md §6)."""
    console.print(f"[red]error[/red] {exc}")
    raise typer.Exit(1)


def main() -> None:
    """Console-script entry point (`dqs`)."""
    app()
