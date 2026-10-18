"""Typer CLI for dqs.

Phase 0 surface (`docs/ARCHITECTURE.md` §15 defines the full target surface,
added phase by phase — commands are not invented here):

- `dqs db init | reset | ping`
- `dqs dev up | down | lint | test | test-int | check` (make-equivalent,
  owner-requested; see docs/DECISIONS.md)

Errors from the layer below are caught and printed; the process exit code is
non-zero so scripts/CI can rely on it.
"""

from __future__ import annotations

from typing import NoReturn

import typer
from rich.console import Console

from dqs import db as dbmod
from dqs import dev as devmod
from dqs.config import load_settings
from dqs.errors import DqsError

app = typer.Typer(name="dqs", help="Agentic data-quality steward for batch pipelines.")
db_app = typer.Typer(help="Warehouse bootstrap commands (roles, schemas, dq tables).")
dev_app = typer.Typer(help="Developer wrappers around docker compose / pytest / ruff / mypy.")
app.add_typer(db_app, name="db")
app.add_typer(dev_app, name="dev")

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
