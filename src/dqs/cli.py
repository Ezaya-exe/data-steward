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
from typing import NoReturn

import typer
from rich.console import Console

from dqs import db as dbmod
from dqs import dbt_run
from dqs import dev as devmod
from dqs.config import load_settings
from dqs.errors import DqsError
from dqs.sim import pipeline as sim_pipeline

app = typer.Typer(name="dqs", help="Agentic data-quality steward for batch pipelines.")
db_app = typer.Typer(help="Warehouse bootstrap commands (roles, schemas, dq tables).")
dev_app = typer.Typer(help="Developer wrappers around docker compose / pytest / ruff / mypy.")
sim_app = typer.Typer(help="Synthetic baseline simulator (generate + load + dbt build).")
dbt_app = typer.Typer(help="Run dbt against the warehouse warehouse/dbt_project.")
app.add_typer(db_app, name="db")
app.add_typer(dev_app, name="dev")
app.add_typer(sim_app, name="sim")
app.add_typer(dbt_app, name="dbt")

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
    dbt_build: bool = typer.Option(True, "--dbt/--no-dbt", help="Run dbt build after loading."),
    seed_value: int | None = typer.Option(
        None, "--seed", help="Generator seed; defaults to DQS_SEED from settings."
    ),
) -> None:
    """Generate + load one day's batch, then run dbt build and record the run."""
    settings = load_settings()
    seed = seed_value if seed_value is not None else settings.dqs_seed
    try:
        business_date = dt.date.fromisoformat(date)
    except ValueError as exc:
        raise typer.BadParameter(f"--date must be YYYY-MM-DD ({exc})") from exc
    console.print(f"loading clean day {business_date} (seed={seed}) …")
    try:
        result = sim_pipeline.load_day(settings, business_date, seed, run_dbt=dbt_build)
    except DqsError as exc:
        _fail(exc)
    dbt_mark = "" if result["dbt_exit"] is None else f" dbt_exit={result['dbt_exit']}"
    console.print(f"  rows={result['rows']}{dbt_mark}")
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
