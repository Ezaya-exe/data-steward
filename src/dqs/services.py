"""Read-mostly service layer shared by the CLI-adjacent UI (docs/ARCHITECTURE.md §13).

Why this module exists: the Streamlit pages must not contain SQL, engine setup or
workflow wiring. They need one typed surface that (a) reuses the production
functions (`list_incidents`, `get_incident`, `load_contracts`, the benchmark
report), (b) fills the few read gaps that have no public function yet
(`dq.agent_steps`, `dq.fix_attempts`, `dq.publications`), and (c) exposes the one
write the UI is allowed to perform — resuming a paused incident with a human
decision.

Determinism boundary: everything that can be computed without a database is a
pure function (`filter_incident_rows`, `database_names`, `benchmark_notes`,
`build_incident_detail`). The DB functions only load rows and hand them to those
builders, which is what makes the view models unit-testable with stub data.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from dqs import fast_reset
from dqs import incidents as incidents_mod
from dqs.bench.report import BenchMetadata, BenchRunFile, load_run_file, render_markdown
from dqs.config import Settings
from dqs.contracts.store import load_contracts, to_yaml
from dqs.db import make_ro_engine, make_rw_engine
from dqs.errors import ConfigError, DatabaseError, DqsError, WorkflowError
from dqs.models.contract import Contract
from dqs.models.enums import ContractStatus, IncidentStatus
from dqs.models.fix import FixPlan, VerificationResult
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause
from dqs.models.state import WorkflowOutcome
from dqs.publish.diff import build_file_patches, full_patch
from dqs.tools.base import ToolContext
from dqs.verify.patch import PROJECT_DIR
from dqs.workflow.runner import IncidentWorkflow, make_deps

# A full benchmark catalogue is at least 40 fault + 6 clean scenarios (§14).
# Anything below this ceiling is a wiring pilot, not a measurement.
PILOT_CATALOGUE_MAX = 10

DATABASE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
DEMO_DATABASE = "dqs_demo"
WAREHOUSE_DATABASE = "warehouse"
BENCHMARK_DB_PREFIX = "dqs_bench_"


# --- typed view models -----------------------------------------------------------------


class DatabaseChoice(BaseModel):
    """One selectable database in the sidebar (never a benchmark scenario DB)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    is_default: bool = False


class IncidentRow(BaseModel):
    """One row of the incidents table, already flattened for display."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    status: str
    severity: str
    table: str | None = None
    fingerprint: str = ""
    summary: str = ""
    created_at: dt.datetime
    updated_at: dt.datetime


class StepView(BaseModel):
    """One `dq.agent_steps` row, with bounded previews instead of raw payloads."""

    model_config = ConfigDict(extra="forbid")

    seq: int
    node: str
    kind: str
    name: str
    args_preview: str = ""
    result_preview: str = ""
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: dt.datetime | None = None


class ChecklistItem(BaseModel):
    """One verification criterion: pass/fail plus the observed evidence."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str = ""
    observed: str = ""


class FixAttemptView(BaseModel):
    """One stored fix attempt: plan metadata, diff and verification checklist."""

    model_config = ConfigDict(extra="forbid")

    attempt_no: int
    passed: bool | None = None
    verification_passed: bool | None = None
    failed_criteria: list[str] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    summary: str = ""
    category: str = ""
    target_files: list[str] = Field(default_factory=list)
    risk: str = ""
    expected_effect: str = ""
    diff: str = ""
    diff_error: str = ""
    created_at: dt.datetime | None = None


class PublicationView(BaseModel):
    """One `dq.publications` row (URL may be a local dry-run preview path)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    mode: str
    url: str
    status: str = "unknown"
    created_at: dt.datetime | None = None


class IncidentDetail(BaseModel):
    """The complete approval payload shown to a human (§13)."""

    model_config = ConfigDict(extra="forbid")

    incident: Incident
    root_cause: RootCause | None = None
    impact: ImpactReport | None = None
    steps: list[StepView] = Field(default_factory=list)
    attempts: list[FixAttemptView] = Field(default_factory=list)
    publications: list[PublicationView] = Field(default_factory=list)
    latest_diff: str = ""
    latest_diff_error: str = ""
    warnings: list[str] = Field(default_factory=list)


class ContractView(BaseModel):
    """One contract file, rendered as YAML for the human reviewer."""

    model_config = ConfigDict(extra="forbid")

    table: str
    layer: str
    status: str
    generated_at: dt.datetime
    yaml: str


class BenchmarkFile(BaseModel):
    """A results JSON on disk (metadata only; parsing happens on selection)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    size_bytes: int
    modified_at: dt.datetime


class BenchmarkMetadataRow(BaseModel):
    """One label/value pair in the benchmark provenance table."""

    model_config = ConfigDict(extra="forbid")

    label: str
    value: str


class BenchmarkView(BaseModel):
    """Everything the Benchmark page renders for one results file."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str
    split: str
    notes: list[str] = Field(default_factory=list)
    metadata: list[BenchmarkMetadataRow] = Field(default_factory=list)
    report_markdown: str = ""
    infra_errors: int = 0
    scenario_count: int = 0
    fault_kinds: list[str] = Field(default_factory=list)


class DecisionResult(BaseModel):
    """The outcome of an approve/reject call, as the UI needs it."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    status: str
    completed: bool = False
    interrupted: bool = False
    publication_url: str | None = None
    message: str = ""


# --- pure helpers ----------------------------------------------------------------------


def _preview(value: Any, limit: int = 160) -> str:
    """A single-line, bounded rendering of a JSON-ish payload (never raises)."""
    try:
        rendered = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = repr(value)
    rendered = " ".join(rendered.split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(limit - 3, 0)] + "..."


def _summary(incident: Incident) -> str:
    if not incident.violations:
        return ""
    violation = incident.violations[0]
    target = violation.column or violation.table or incident.table or ""
    label = f"{violation.check} ({target})" if target else violation.check
    return _preview(f"{label}: {violation.message}", limit=120)


def incident_row(incident: Incident) -> IncidentRow:
    """Flatten one incident for the table (pure)."""
    return IncidentRow(
        incident_id=incident.incident_id,
        status=incident.status.value,
        severity=incident.severity.value,
        table=incident.table,
        fingerprint=incident.fingerprint,
        summary=_summary(incident),
        created_at=incident.created_at,
        updated_at=incident.updated_at,
    )


def filter_incident_rows(
    rows: Sequence[IncidentRow],
    *,
    statuses: Iterable[str] | None = None,
    severities: Iterable[str] | None = None,
    table: str | None = None,
    search: str | None = None,
) -> list[IncidentRow]:
    """Apply the Incidents-page filters (pure; empty/None means "no filter")."""
    wanted_status = {value for value in statuses or () if value}
    wanted_severity = {value for value in severities or () if value}
    table_term = (table or "").strip().lower()
    search_term = (search or "").strip().lower()
    selected: list[IncidentRow] = []
    for row in rows:
        if wanted_status and row.status not in wanted_status:
            continue
        if wanted_severity and row.severity not in wanted_severity:
            continue
        if table_term and table_term not in (row.table or "").lower():
            continue
        if search_term:
            haystack = " ".join(
                (str(row.incident_id), row.fingerprint, row.table or "", row.summary)
            ).lower()
            if search_term not in haystack:
                continue
        selected.append(row)
    return selected


def database_names(
    steward_databases: Iterable[str],
    *,
    configured: str = WAREHOUSE_DATABASE,
) -> list[str]:
    """The databases the UI may expose: warehouse + dqs_demo, never bench DBs.

    Benchmark scenario databases (`dqs_bench_*`) and the cached template are
    deliberately excluded: switching the UI onto a scenario mid-run would make a
    benchmark observation depend on a human click.
    """
    present = set(steward_databases)
    names: list[str] = []
    for candidate in (configured, WAREHOUSE_DATABASE, DEMO_DATABASE):
        if candidate in names:
            continue
        if candidate == DEMO_DATABASE and candidate not in present:
            continue
        names.append(candidate)
    return names


def default_database(names: Sequence[str]) -> str:
    """`dqs_demo` when present, else the warehouse, else the first choice."""
    if DEMO_DATABASE in names:
        return DEMO_DATABASE
    if WAREHOUSE_DATABASE in names:
        return WAREHOUSE_DATABASE
    if names:
        return names[0]
    return WAREHOUSE_DATABASE


def effective_publish_mode(settings: Settings) -> str:
    """The mode a UI approval would actually use (`GITHUB_MODE`)."""
    return "live" if settings.github_mode == "live" else "dry_run"


def require_awaiting_approval(incident: Incident | None, incident_id: uuid.UUID) -> Incident:
    """Gate the only UI write: an incident must be paused at the approval node."""
    if incident is None:
        raise WorkflowError(f"no incident with id {incident_id}")
    if incident.status is not IncidentStatus.AWAITING_APPROVAL:
        raise WorkflowError(
            f"incident {incident_id} is {incident.status.value}, not awaiting approval"
        )
    return incident


def _checklist(verification: VerificationResult) -> list[ChecklistItem]:
    return [
        ChecklistItem(
            name=criterion.name,
            passed=criterion.passed,
            detail=criterion.detail,
            observed=_preview(criterion.observed, limit=400),
        )
        for criterion in verification.criteria
    ]


def build_attempt_view(
    attempt_no: int,
    plan_payload: Any,
    verification_payload: Any,
    *,
    passed: bool | None = None,
    created_at: dt.datetime | None = None,
    project_dir: Path = PROJECT_DIR,
) -> FixAttemptView:
    """Turn one stored plan+verification pair into a reviewable attempt (pure).

    A NULL `plan` is a normal record: the model produced no usable plan on that
    attempt. The view then carries the verifier's verdict and explains the
    missing diff instead of raising.
    """
    plan: FixPlan | None = None
    verification: VerificationResult | None = None
    diff = ""
    diff_error = ""
    if plan_payload is None:
        diff_error = "no fix plan was stored for this attempt (the model reply was invalid)"
    else:
        try:
            plan = FixPlan.model_validate(plan_payload)
        except ValidationError as exc:
            diff_error = f"stored plan does not validate: {exc.error_count()} error(s)"
    try:
        verification = VerificationResult.model_validate(verification_payload)
    except ValidationError as exc:
        diff_error = (diff_error + "; " if diff_error else "") + (
            f"stored verification does not validate: {exc.error_count()} error(s)"
        )

    if plan is not None:
        try:
            diff = full_patch(build_file_patches(plan.edits, project_dir))
        except (DqsError, OSError) as exc:
            diff_error = str(exc)

    return FixAttemptView(
        attempt_no=attempt_no,
        passed=passed,
        verification_passed=None if verification is None else verification.passed,
        failed_criteria=[] if verification is None else list(verification.failed_criteria),
        checklist=[] if verification is None else _checklist(verification),
        summary="" if plan is None else plan.summary,
        category="" if plan is None else plan.category.value,
        target_files=[] if plan is None else [edit.path for edit in plan.edits],
        risk="" if plan is None else plan.risk,
        expected_effect="" if plan is None else plan.expected_effect,
        diff=diff,
        diff_error=diff_error,
        created_at=created_at,
    )


def build_incident_detail(
    incident: Incident,
    *,
    steps: Sequence[StepView] = (),
    attempts: Sequence[FixAttemptView] = (),
    publications: Sequence[PublicationView] = (),
) -> IncidentDetail:
    """Assemble the detail view from already-loaded pieces (pure)."""
    warnings: list[str] = []
    root_cause: RootCause | None = None
    impact: ImpactReport | None = None
    if incident.root_cause is not None:
        try:
            root_cause = RootCause.model_validate(incident.root_cause)
        except ValidationError:
            warnings.append("Stored root cause does not match the current schema.")
    if incident.impact is not None:
        try:
            impact = ImpactReport.model_validate(incident.impact)
        except ValidationError:
            warnings.append("Stored impact report does not match the current schema.")
    latest = attempts[-1] if attempts else None
    return IncidentDetail(
        incident=incident,
        root_cause=root_cause,
        impact=impact,
        steps=list(steps),
        attempts=list(attempts),
        publications=list(publications),
        latest_diff="" if latest is None else latest.diff,
        latest_diff_error="" if latest is None else latest.diff_error,
        warnings=warnings,
    )


def benchmark_notes(metadata: BenchMetadata, scenario_count: int) -> list[str]:
    """Visible caveats for a results file (pilot, partial, dirty tree)."""
    notes: list[str] = []
    if metadata.catalogue_size <= PILOT_CATALOGUE_MAX:
        notes.append(
            f"Pilot run: only {metadata.catalogue_size} catalogue scenarios. "
            "A full benchmark has at least 40 fault + 6 clean scenarios."
        )
    expected = metadata.catalogue_size * max(metadata.repeats, 1)
    if scenario_count < expected:
        notes.append(
            f"Incomplete: {scenario_count} result records for "
            f"{metadata.catalogue_size} scenarios x {metadata.repeats} repeat(s)."
        )
    if metadata.stop_reason:
        notes.append(f"Stopped early: {metadata.stop_reason}")
    if metadata.git_dirty:
        notes.append("The working tree was dirty when this run started.")
    if metadata.split == "holdout":
        notes.append("Holdout split: never tune prompts or code on this result.")
    return notes


def build_benchmark_view(name: str, run: BenchRunFile) -> BenchmarkView:
    """Assemble the Benchmark page from a validated results file (pure)."""
    metadata = run.metadata
    aggregate = run.aggregates()
    notes = benchmark_notes(metadata, len(run.scenarios))
    if aggregate.overall.infra_errors:
        notes.append(
            f"{aggregate.overall.infra_errors} scenario result(s) were infrastructure "
            "errors and are excluded from the denominators."
        )
    prompt_hashes = ", ".join(
        f"{key}={value[:8]}" for key, value in sorted(metadata.prompt_hashes.items())
    )
    rows = [
        BenchmarkMetadataRow(label="Model", value=metadata.model),
        BenchmarkMetadataRow(label="Provider", value=metadata.provider),
        BenchmarkMetadataRow(label="Split", value=metadata.split),
        BenchmarkMetadataRow(label="Run id", value=metadata.run_id),
        BenchmarkMetadataRow(label="Started", value=metadata.started_at),
        BenchmarkMetadataRow(label="Finished", value=metadata.finished_at),
        BenchmarkMetadataRow(
            label="Git SHA",
            value=f"{metadata.git_sha} (dirty={metadata.git_dirty})",
        ),
        BenchmarkMetadataRow(label="Code fingerprint", value=metadata.code_fingerprint),
        BenchmarkMetadataRow(label="Harness version", value=metadata.harness_version),
        BenchmarkMetadataRow(label="Prompt hashes", value=prompt_hashes or "not recorded"),
        BenchmarkMetadataRow(
            label="Catalogue",
            value=(
                f"{metadata.catalogue_size} scenarios "
                f"({metadata.fault_count} fault, {metadata.control_count} clean)"
            ),
        ),
        BenchmarkMetadataRow(label="Repeats", value=str(metadata.repeats)),
        BenchmarkMetadataRow(label="Tokens", value=str(metadata.tokens_total)),
        BenchmarkMetadataRow(
            label="Estimated cost",
            value=(
                f"${metadata.cost_usd_total:.4f} "
                "(estimated at list price; actual billing depends on the provider)"
            ),
        ),
    ]
    return BenchmarkView(
        name=name,
        model=metadata.model,
        split=metadata.split,
        notes=notes,
        metadata=rows,
        report_markdown=render_markdown(run),
        infra_errors=aggregate.overall.infra_errors,
        scenario_count=len(run.scenarios),
        fault_kinds=sorted(aggregate.by_kind),
    )


def describe_outcome(outcome: WorkflowOutcome) -> str:
    """One plain sentence summarising a resumed workflow (pure)."""
    if outcome.interrupted:
        return f"Incident {outcome.incident_id} is paused again at the approval gate."
    if outcome.error:
        return f"Incident {outcome.incident_id} ended as {outcome.status.value}: {outcome.error}"
    return f"Incident {outcome.incident_id} is now {outcome.status.value}."


# --- database access -------------------------------------------------------------------


def validate_database_name(name: str) -> str:
    if not DATABASE_NAME_RE.fullmatch(name):
        raise ConfigError(f"invalid database name {name!r}")
    if name.startswith(BENCHMARK_DB_PREFIX) or name == fast_reset.TEMPLATE_DB:
        raise ConfigError(f"refusing to open benchmark/template database {name!r}")
    return name


def engine_for(settings: Settings, database: str, *, read_only: bool = True) -> Engine:
    """An engine scoped to one database; read-only unless explicitly requested."""
    name = validate_database_name(database)
    scoped = settings.model_copy(update={"pg_database": name})
    return make_ro_engine(scoped) if read_only else make_rw_engine(scoped)


def database_choices(settings: Settings) -> list[DatabaseChoice]:
    """The sidebar choices, defaulting to `dqs_demo` when it exists."""
    try:
        present = fast_reset.list_steward_databases(settings)
    except DatabaseError:
        present = []
    names = database_names(present, configured=settings.pg_database)
    default = default_database(names)
    return [DatabaseChoice(name=name, is_default=name == default) for name in names]


def list_incident_rows(engine: Engine, *, limit: int = 500) -> list[IncidentRow]:
    """All incidents, newest first, flattened for the table."""
    incidents = incidents_mod.list_incidents(engine, limit=limit)
    return [incident_row(incident) for incident in incidents]


def list_steps(engine: Engine, incident_id: uuid.UUID) -> list[StepView]:
    """The agent timeline for one incident, in durable sequence order."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT seq, node, kind, name, input, output, tokens_in, tokens_out, "
                    "latency_ms, created_at FROM dq.agent_steps "
                    "WHERE incident_id = :i ORDER BY created_at, seq"
                ),
                {"i": incident_id},
            ).mappings()
            records = list(rows)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not read dq.agent_steps: {exc}") from exc
    return [
        StepView(
            seq=int(record["seq"]),
            node=str(record["node"]),
            kind=str(record["kind"]),
            name=str(record["name"]),
            args_preview=_preview(record["input"], limit=160),
            result_preview=_preview(record["output"], limit=160),
            latency_ms=int(record["latency_ms"] or 0),
            tokens_in=int(record["tokens_in"] or 0),
            tokens_out=int(record["tokens_out"] or 0),
            created_at=record["created_at"],
        )
        for record in records
    ]


def list_fix_attempts(
    engine: Engine, incident_id: uuid.UUID, *, project_dir: Path = PROJECT_DIR
) -> list[FixAttemptView]:
    """Every stored fix attempt, oldest first (the UI shows all of them)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT attempt_no, plan, verification, passed, created_at "
                    "FROM dq.fix_attempts WHERE incident_id = :i "
                    "ORDER BY attempt_no, attempt_id"
                ),
                {"i": incident_id},
            ).mappings()
            records = list(rows)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not read dq.fix_attempts: {exc}") from exc
    return [
        build_attempt_view(
            int(record["attempt_no"]),
            record["plan"],
            record["verification"],
            passed=record["passed"],
            created_at=record["created_at"],
            project_dir=project_dir,
        )
        for record in records
    ]


def list_publications(engine: Engine, incident_id: uuid.UUID) -> list[PublicationView]:
    """Every publication row, including pending ones (the UI must show them)."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT kind, mode, url, status, created_at FROM dq.publications "
                    "WHERE incident_id = :i ORDER BY created_at, kind"
                ),
                {"i": incident_id},
            ).mappings()
            records = list(rows)
    except SQLAlchemyError as exc:
        raise DatabaseError(f"could not read dq.publications: {exc}") from exc
    return [
        PublicationView(
            kind=str(record["kind"]),
            mode=str(record["mode"]),
            url=str(record["url"]),
            status=str(record["status"] or "unknown"),
            created_at=record["created_at"],
        )
        for record in records
    ]


def incident_detail(
    engine: Engine, incident_id: uuid.UUID, *, project_dir: Path = PROJECT_DIR
) -> IncidentDetail | None:
    """Load and assemble one incident's approval payload (None when absent)."""
    incident = incidents_mod.get_incident(engine, incident_id)
    if incident is None:
        return None
    return build_incident_detail(
        incident,
        steps=list_steps(engine, incident_id),
        attempts=list_fix_attempts(engine, incident_id, project_dir=project_dir),
        publications=list_publications(engine, incident_id),
    )


def list_contracts(directory: Path) -> list[ContractView]:
    """Every contract file, sorted by qualified table name."""
    contracts: dict[str, Contract] = load_contracts(directory)
    return [
        ContractView(
            table=contract.table,
            layer=contract.layer.value,
            status=contract.status.value,
            generated_at=contract.generated_at,
            yaml=to_yaml(contract),
        )
        for _, contract in sorted(contracts.items())
    ]


def contract_counts(contracts: Sequence[ContractView]) -> tuple[int, int]:
    """(approved, draft) counts, without assuming only two statuses exist."""
    approved = sum(1 for contract in contracts if contract.status == ContractStatus.APPROVED)
    draft = sum(1 for contract in contracts if contract.status == ContractStatus.DRAFT)
    return approved, draft


def list_benchmark_files(directory: Path) -> list[BenchmarkFile]:
    """Results JSON files, newest first; `latest.md` is not a results file."""
    if not directory.exists():
        return []
    files = sorted(
        directory.glob("*.json"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    return [
        BenchmarkFile(
            name=path.name,
            size_bytes=path.stat().st_size,
            modified_at=dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC),
        )
        for path in files
    ]


def load_benchmark_file(name: str, directory: Path) -> BenchRunFile:
    """Load one results file by bare filename (rejects traversal and subdirs)."""
    if name != Path(name).name or not name.endswith(".json"):
        raise DqsError(f"invalid benchmark file name {name!r}")
    path = (directory / name).resolve()
    if path.parent != directory.resolve():
        raise DqsError(f"refusing to read outside the results directory: {name!r}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DqsError(f"could not read {name!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DqsError(f"{name!r} is not valid JSON: {exc}") from exc
    try:
        return load_run_file(payload)
    except ValidationError as exc:
        raise DqsError(f"{name!r} is not a valid benchmark result file: {exc}") from exc


def benchmark_view(name: str, directory: Path) -> BenchmarkView:
    """Load a results file and build the page view in one call."""
    return build_benchmark_view(name, load_benchmark_file(name, directory))


# --- the one write: human approval -----------------------------------------------------


class ResumeWorkflow(Protocol):
    """Injectable seam so the approval gate can be tested without a checkpoint."""

    def __call__(
        self,
        settings: Settings,
        engine: Engine,
        incident_id: uuid.UUID,
        *,
        decision: str,
        approver: str,
        note: str,
    ) -> WorkflowOutcome: ...


GetIncident = Callable[[Engine, uuid.UUID], Incident | None]


def _resume_via_workflow(
    settings: Settings,
    engine: Engine,
    incident_id: uuid.UUID,
    *,
    decision: str,
    approver: str,
    note: str,
) -> WorkflowOutcome:
    context = ToolContext.from_settings(settings)
    try:
        deps = make_deps(settings, engine, tool_context=context)
        with IncidentWorkflow(deps) as workflow:
            return workflow.resume(incident_id, decision=decision, approver=approver, note=note)
    finally:
        context.close()


def decide_incident(
    settings: Settings,
    incident_id: uuid.UUID,
    *,
    decision: str,
    approver: str,
    note: str = "",
    database: str | None = None,
    engine: Engine | None = None,
    resume: ResumeWorkflow | None = None,
    get_incident: GetIncident | None = None,
) -> DecisionResult:
    """Approve or reject a paused incident by resuming its checkpoint.

    The status gate is checked here, not in the UI, so a stale page cannot
    resume an incident that already moved on. This is the only function in the
    service layer that writes (through the workflow, not through raw SQL).
    """
    if decision not in {"approve", "reject"}:
        raise WorkflowError("decision must be 'approve' or 'reject'")
    if not approver.strip():
        raise WorkflowError("an approver is required")
    if decision == "reject" and not note.strip():
        raise WorkflowError("a rejection must include a note explaining why")

    loader = get_incident or incidents_mod.get_incident
    runner = resume or _resume_via_workflow
    owns_engine = engine is None
    active = engine or engine_for(settings, database or settings.pg_database, read_only=False)
    try:
        require_awaiting_approval(loader(active, incident_id), incident_id)
        outcome = runner(
            settings,
            active,
            incident_id,
            decision=decision,
            approver=approver.strip(),
            note=note.strip(),
        )
    finally:
        if owns_engine:
            active.dispose()

    return DecisionResult(
        incident_id=outcome.incident_id,
        status=outcome.status.value,
        completed=outcome.completed,
        interrupted=outcome.interrupted,
        publication_url=None if outcome.publication is None else outcome.publication.url,
        message=describe_outcome(outcome),
    )


__all__ = [
    "BenchmarkFile",
    "BenchmarkMetadataRow",
    "BenchmarkView",
    "ChecklistItem",
    "ContractView",
    "DatabaseChoice",
    "DecisionResult",
    "FixAttemptView",
    "IncidentDetail",
    "IncidentRow",
    "PublicationView",
    "StepView",
    "benchmark_notes",
    "benchmark_view",
    "build_attempt_view",
    "build_benchmark_view",
    "build_incident_detail",
    "contract_counts",
    "database_choices",
    "database_names",
    "decide_incident",
    "default_database",
    "describe_outcome",
    "effective_publish_mode",
    "engine_for",
    "filter_incident_rows",
    "incident_detail",
    "incident_row",
    "list_benchmark_files",
    "list_contracts",
    "list_fix_attempts",
    "list_incident_rows",
    "list_publications",
    "list_steps",
    "load_benchmark_file",
    "require_awaiting_approval",
    "validate_database_name",
]
