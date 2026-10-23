"""`dqs demo`: the two-minute break → detect → diagnose → verify → PR story.

Why a separate module: the demo is orchestration, not presentation. It resets a
**separate** `dqs_demo` database (the warehouse is never touched), seeds a clean
baseline, injects one fault, runs the real monitor and the real workflow, and
returns a structured result the CLI prints. The approval decision is injected
as a callback so the CLI owns the prompt and `--yes` is explicit.

The template clone keeps the demo reproducible: roles/schemas/table DDL come
from the same idempotent SQL files as production, not from a hand-built fixture.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, text

from dqs import db as dbmod
from dqs import fast_reset
from dqs import incidents as incidents_mod
from dqs import monitor as monitor_mod
from dqs.config import Settings
from dqs.contracts import generation as contracts_generation
from dqs.contracts import store as contracts_store
from dqs.errors import DqsError
from dqs.models.faults import FaultSpec
from dqs.models.state import WorkflowOutcome
from dqs.publish.base import PublicationKind
from dqs.publish.dry_run import ISSUE_OUTPUT_DIR, preview_paths
from dqs.sim import pipeline as sim_pipeline
from dqs.sim.faults import parse_fault_specs
from dqs.tools.base import ToolContext
from dqs.workflow.runner import IncidentWorkflow, make_deps

DEMO_DB: Final = "dqs_demo"
BASELINE_DAYS: Final = 30
DEFAULT_FAULT: Final = "column_rename:orders.customer_id:new_name=cust_id"

# Returns (approve?, note). The CLI supplies a prompt; --yes supplies None and
# lets the workflow's own explicit auto-approval path run.
ApprovalCallback = Callable[[dict[str, Any]], tuple[bool, str]]
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True)
class StepRow:
    """One `dq.agent_steps` row, for the printed transcript."""

    node: str
    seq: int
    kind: str
    name: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


@dataclass(frozen=True)
class DemoResult:
    """Everything the CLI needs to tell the story honestly."""

    database: str
    baseline_days: int
    fault_specs: list[str]
    fault_date: dt.date
    incident_id: uuid.UUID
    outcome: WorkflowOutcome
    preview_md: Path | None
    preview_patch: Path | None
    steps: list[StepRow]
    created_incidents: list[uuid.UUID]


def ensure_demo_database(settings: Settings, *, reset: bool = False) -> bool:
    """Create `dqs_demo` by cloning the cached clean baseline.

    Phase 8a: the 30-day seed is no longer rebuilt for a demo. `dqs_template`
    is built once (and rebuilt when generator/contracts/dbt models change), then
    `dqs_demo` is always a fresh `CREATE DATABASE ... TEMPLATE` clone. `reset`
    is kept for CLI compatibility; a demo run never reuses a dirty database.
    """
    _ = reset
    fast_reset.clone_template(settings, DEMO_DB)
    return True


def _demo_settings(settings: Settings) -> Settings:
    """The demo owns a throwaway DB and never writes to GitHub.

    Like the benchmark, it forces `dry_run` rather than trusting `.env`: a demo
    auto-approves, so a live misconfiguration would publish without a review.
    """
    return settings.model_copy(update={"pg_database": DEMO_DB, "github_mode": "dry_run"})


def _contracts_dir() -> Path:
    return Path.cwd() / contracts_store.CONTRACTS_DIRNAME


def _approve_contracts(settings: Settings) -> dict[str, Any]:
    directory = _contracts_dir()
    files = sorted(directory.glob("*.yml"))
    if not files:
        engine = dbmod.make_rw_engine(settings)
        try:
            generated = contracts_generation.generate_all(engine)
        finally:
            engine.dispose()
        contracts_store.save_generated_many(generated, directory)
    contracts_store.approve(directory, None)
    return contracts_store.load_approved(directory)


def _pick_incident(engine: Engine, created: list[uuid.UUID], fault_table: str) -> uuid.UUID:
    """The incident for the faulted raw table, or the first created one."""
    table = fault_table.split(".")[-1]
    fallback: uuid.UUID | None = None
    for incident_id in created:
        incident = incidents_mod.get_incident(engine, incident_id)
        if incident is None:
            continue
        if fallback is None:
            fallback = incident_id
        if incident.table and incident.table.endswith(table):
            return incident_id
    if fallback is None:
        raise DqsError(
            "monitoring created no incident for the injected fault; "
            "run `dqs check batch` on the fault day to inspect the checks"
        )
    return fallback


def _as_int(value: object) -> int:
    """SQLAlchemy rows are untyped; keep the dataclass strictly numeric."""
    return 0 if value is None else int(str(value))


def _load_steps(engine: Engine, incident_id: uuid.UUID) -> list[StepRow]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT node, seq, kind, name, tokens_in, tokens_out, latency_ms "
                "FROM dq.agent_steps WHERE incident_id = :id "
                "ORDER BY created_at, node, seq"
            ),
            {"id": incident_id},
        ).fetchall()
    return [
        StepRow(
            node=str(node),
            seq=_as_int(seq),
            kind=str(kind),
            name=str(name),
            tokens_in=_as_int(tokens_in),
            tokens_out=_as_int(tokens_out),
            latency_ms=_as_int(latency_ms),
        )
        for node, seq, kind, name, tokens_in, tokens_out, latency_ms in rows
    ]


def _preview_paths(outcome: WorkflowOutcome) -> tuple[Path | None, Path | None]:
    if outcome.publication is None:
        return None, None
    if outcome.publication.kind is PublicationKind.PR:
        return preview_paths(outcome.incident_id)
    return ISSUE_OUTPUT_DIR / f"{outcome.incident_id.hex[:8]}.md", None


def run_demo(
    settings: Settings,
    *,
    fault_specs: list[str] | None = None,
    auto_approve: bool = False,
    reset: bool = False,
    approver: str = "auto",
    approval_callback: ApprovalCallback | None = None,
    progress: ProgressCallback | None = None,
) -> DemoResult:
    """Reset the demo DB, seed 30 days, inject one fault, monitor and run."""
    specs: list[FaultSpec] = parse_fault_specs(fault_specs or [DEFAULT_FAULT])
    if not specs:
        raise DqsError("the demo needs at least one fault spec")
    notify = progress if progress is not None else (lambda _message: None)

    demo_settings = _demo_settings(settings)
    if settings.github_mode != "dry_run":
        notify("GITHUB_MODE forced to dry_run for the demo (no GitHub writes)")
    notify(f"preparing {DEMO_DB} from the cached baseline template")
    ensure_demo_database(settings, reset=reset)
    notify("approving generated contracts")
    contracts = _approve_contracts(demo_settings)

    fault_date = sim_pipeline.BASELINE_START + dt.timedelta(days=BASELINE_DAYS)
    notify(f"injecting {len(specs)} fault(s) on {fault_date} and rebuilding")
    loaded = sim_pipeline.load_day(
        demo_settings, fault_date, demo_settings.dqs_seed, run_dbt=True, specs=specs
    )
    ground_truth = loaded["ground_truth"]
    fault_table = ground_truth[0].table if ground_truth else specs[0].table

    engine = dbmod.make_rw_engine(demo_settings)
    context: ToolContext | None = None
    try:
        as_of = dt.datetime.combine(fault_date + dt.timedelta(days=1), dt.time(0, 0), tzinfo=dt.UTC)
        monitor = monitor_mod.run_monitor(engine, contracts, as_of)
        incident_id = _pick_incident(engine, monitor.created, fault_table)
        notify(f"monitor created {len(monitor.created)} incident(s); running the workflow")
        context = ToolContext.from_settings(demo_settings)
        deps = make_deps(demo_settings, engine, tool_context=context)
        with IncidentWorkflow(deps) as workflow:
            outcome = workflow.run(incident_id, auto_approve=auto_approve)
            if outcome.interrupted:
                if approval_callback is not None:
                    approve, note = approval_callback(outcome.approval_request or {})
                    outcome = workflow.resume(
                        incident_id,
                        decision="approve" if approve else "reject",
                        approver=approver,
                        note=note,
                    )
        notify(f"workflow finished: status={outcome.status.value} attempts={outcome.attempts}")
        steps = _load_steps(engine, incident_id)
        preview_md, preview_patch = _preview_paths(outcome)
        return DemoResult(
            database=DEMO_DB,
            baseline_days=BASELINE_DAYS,
            fault_specs=[spec.format() for spec in specs],
            fault_date=fault_date,
            incident_id=incident_id,
            outcome=outcome,
            preview_md=preview_md,
            preview_patch=preview_patch,
            steps=steps,
            created_incidents=list(monitor.created),
        )
    finally:
        if context is not None:
            context.close()
        engine.dispose()


__all__ = [
    "BASELINE_DAYS",
    "DEFAULT_FAULT",
    "DEMO_DB",
    "DemoResult",
    "StepRow",
    "ensure_demo_database",
    "run_demo",
]
