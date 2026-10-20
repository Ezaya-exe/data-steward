"""Typer CLI for dqs.

Current surface (docs/ARCHITECTURE.md §15 defines the full target surface,
added phase by phase — commands are not invented here):

- `dqs db init | reset | ping`
- `dqs dev up | down | lint | test | test-int | check` (make-equivalent,
  owner-requested; see docs/DECISIONS.md)
- `dqs sim seed [--days N] [--seed S] | day --date YYYY-MM-DD [--no-dbt]`  (Phase 1)
- `dqs dbt build [--target dev|sandbox]`                                    (Phase 1)

Errors from the layer below are caught and printed; the process exit code is
non-zero so scripts/CI can rely on it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine

from dqs import db as dbmod
from dqs import dbt_run
from dqs import dev as devmod
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.agents.smoke import run_smoke
from dqs.checks.engine import CheckEngine
from dqs.config import load_settings
from dqs.contracts import generation as contracts_generation
from dqs.contracts import store as contracts_store
from dqs.errors import DqsError, FaultSpecError
from dqs.lineage.graph import LineageGraph
from dqs.lineage.impact import build_impact_report
from dqs.models.contract import Contract
from dqs.models.faults import FaultSpec
from dqs.models.violation import Violation
from dqs.sim import pipeline as sim_pipeline
from dqs.sim.faults import parse_fault_specs

app = typer.Typer(name="dqs", help="Agentic data-quality steward for batch pipelines.")
db_app = typer.Typer(help="Warehouse bootstrap commands (roles, schemas, dq tables).")
dev_app = typer.Typer(help="Developer wrappers around docker compose / pytest / ruff / mypy.")
sim_app = typer.Typer(help="Synthetic baseline simulator (generate + load + dbt build).")
dbt_app = typer.Typer(help="Run dbt against the warehouse warehouse/dbt_project.")
contracts_app = typer.Typer(help="Data contract generation and approval (§7).")
check_app = typer.Typer(help="Run the deterministic check engine by hand (§7.3).")
monitor_app = typer.Typer(help="The batch monitor: run checks and record incidents (§8).")
incidents_app = typer.Typer(help="Inspect recorded incidents.")
llm_app = typer.Typer(help="LLM provider checks (Phase 4).")
app.add_typer(db_app, name="db")
app.add_typer(dev_app, name="dev")
app.add_typer(sim_app, name="sim")
app.add_typer(dbt_app, name="dbt")
app.add_typer(contracts_app, name="contracts")
app.add_typer(check_app, name="check")
app.add_typer(monitor_app, name="monitor")
app.add_typer(incidents_app, name="incidents")
app.add_typer(llm_app, name="llm")

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


def _fail(exc: DqsError) -> NoReturn:
    """Print one clear error line and exit 1 (never swallow, AGENTS.md §6)."""
    console.print(f"[red]error[/red] {exc}")
    raise typer.Exit(1)


def main() -> None:
    """Console-script entry point (`dqs`)."""
    app()
