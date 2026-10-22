"""Run and resume the incident workflow over a durable SQLite checkpoint.

Why a runner separate from the graph: the graph is pure wiring, while this
module owns process boundaries — opening the checkpoint database, constructing
the real LLM/tool/publisher services, and turning any node exception into a
`failed` incident without deleting the checkpoint that proves what happened.

`thread_id` is the incident id (§10.1), so the same incident resumed in a new
process picks up exactly where it stopped.
"""

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from sqlalchemy import Engine

from dqs import dbt_run
from dqs.agents.fixer import CONTRACTS_DIR
from dqs.agents.llm import (
    BudgetTracker,
    LLMClient,
    redact_secrets,
    settings_secrets,
)
from dqs.agents.steps import DbStepSink
from dqs.config import Settings
from dqs.errors import DqsError, WorkflowError
from dqs.lineage.graph import LineageGraph
from dqs.lineage.impact import build_impact_report
from dqs.models.enums import IncidentStatus
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.state import WorkflowBudget, WorkflowOutcome, WorkflowState
from dqs.publish.base import Publisher
from dqs.publish.github import build_publisher
from dqs.publish.store import DbPublicationStore
from dqs.tools.base import ToolContext
from dqs.verify.patch import PROJECT_DIR
from dqs.workflow.graph import build_graph
from dqs.workflow.journal import DbJournal
from dqs.workflow.nodes import WorkflowDeps

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHECKPOINT_PATH = REPO_ROOT / "data" / "checkpoints.sqlite"


def _initial_state(incident: Incident, budget: WorkflowBudget, auto_approve: bool) -> WorkflowState:
    return WorkflowState(
        incident_id=incident.incident_id,
        incident=incident,
        root_cause=None,
        impact=None,
        resolution_type=None,
        fix_plan=None,
        attempts=[],
        fix_attempts=[],
        pending_plan=None,
        attempt_no=0,
        approval=None,
        publication=None,
        issue=None,
        candidate_models=[],
        budget=budget,
        errors=[],
        auto_approve=auto_approve,
        verification_passed=False,
        budget_exceeded=False,
        completed=False,
    )


class IncidentWorkflow:
    """One compiled graph + one SQLite saver for a single CLI invocation."""

    def __init__(
        self,
        deps: WorkflowDeps,
        *,
        checkpoint_path: Path | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.deps = deps
        self.checkpoint_path = Path(checkpoint_path or DEFAULT_CHECKPOINT_PATH)
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._owns_connection = connection is None
        self._connection = connection or sqlite3.connect(
            str(self.checkpoint_path), check_same_thread=False
        )
        self._saver = SqliteSaver(self._connection)
        self.graph = build_graph(deps, checkpointer=self._saver)

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> IncidentWorkflow:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- lifecycle -----------------------------------------------------------------

    def _config(self, incident_id: uuid.UUID) -> RunnableConfig:
        return {"configurable": {"thread_id": str(incident_id)}}

    def run(self, incident_id: uuid.UUID, *, auto_approve: bool = False) -> WorkflowOutcome:
        """Start a new run, or refuse when the checkpoint is already in use."""
        snapshot = self.graph.get_state(self._config(incident_id))
        if snapshot.values:
            if snapshot.next:
                next_nodes = ", ".join(str(node) for node in snapshot.next)
                raise WorkflowError(
                    f"incident {incident_id} already has a checkpoint waiting at {next_nodes}; "
                    "use `dqs incident approve/reject` instead of starting a new run"
                )
            raise WorkflowError(
                f"incident {incident_id} already has a completed checkpoint; "
                "start a fresh incident to run again"
            )

        incident = self.deps.journal.get_incident(incident_id)
        if incident is None:
            raise WorkflowError(f"no incident with id {incident_id}")
        self.deps.journal.set_status(
            incident_id, IncidentStatus.INVESTIGATING, reason="run started"
        )
        started_at = dt.datetime.now(dt.UTC)
        state = _initial_state(incident, self._budget_state(started_at), auto_approve)
        return self._invoke(state, incident_id)

    def resume(
        self,
        incident_id: uuid.UUID,
        *,
        decision: str,
        approver: str,
        note: str = "",
    ) -> WorkflowOutcome:
        """Resume the approval interrupt with a typed decision."""
        if decision not in {"approve", "reject"}:
            raise WorkflowError("decision must be 'approve' or 'reject'")
        if decision == "reject" and not note.strip():
            raise WorkflowError("a rejection must include a note explaining why")
        snapshot = self.graph.get_state(self._config(incident_id))
        if not snapshot.next:
            if not snapshot.values:
                raise WorkflowError(f"incident {incident_id} has no checkpoint to resume")
            raise WorkflowError(f"incident {incident_id} is not waiting for a decision")
        if "approval_gate" not in {str(node) for node in snapshot.next}:
            raise WorkflowError(
                f"incident {incident_id} is waiting at {snapshot.next}, not at the approval gate"
            )
        resume_value = {"decision": decision, "approver": approver, "note": note}
        return self._invoke(Command(resume=resume_value), incident_id)

    def _invoke(
        self, payload: WorkflowState | Command[Any], incident_id: uuid.UUID
    ) -> WorkflowOutcome:
        try:
            result = self.graph.invoke(payload, self._config(incident_id))
        except Exception as exc:
            return self._failed(incident_id, exc)
        return self._outcome(incident_id, result)

    # --- outcomes ------------------------------------------------------------------

    def _budget_state(self, started_at: dt.datetime) -> WorkflowBudget:
        snapshot = self.deps.budget.state()
        return WorkflowBudget(
            tool_calls_used=snapshot.tool_calls,
            tokens_used=snapshot.tokens,
            started_at=started_at,
            elapsed_seconds=snapshot.elapsed_seconds,
            max_tool_calls=snapshot.max_tool_calls,
            max_tokens=snapshot.max_tokens,
            timeout_seconds=snapshot.timeout_seconds,
        )

    def _outcome(self, incident_id: uuid.UUID, result: dict[str, Any]) -> WorkflowOutcome:
        interrupts = result.get("__interrupt__") or ()
        approval_request = None
        if interrupts:
            approval_request = getattr(interrupts[0], "value", None)
        incident = self.deps.journal.get_incident(incident_id)
        status = incident.status if incident is not None else IncidentStatus.FAILED
        budget = result.get("budget")
        return WorkflowOutcome(
            incident_id=incident_id,
            status=status,
            interrupted=bool(interrupts),
            completed=bool(result.get("completed")),
            resolution_type=result.get("resolution_type"),
            publication=result.get("publication"),
            approval_request=approval_request,
            attempts=len(result.get("attempts", [])),
            checkpoint_path=str(self.checkpoint_path),
            state={
                "errors": list(result.get("errors", [])),
                "resolution_type": getattr(result.get("resolution_type"), "value", None),
                "budget": (
                    budget.model_dump(mode="json") if isinstance(budget, WorkflowBudget) else None
                ),
            },
        )

    def _failed(self, incident_id: uuid.UUID, exc: BaseException) -> WorkflowOutcome:
        secrets = settings_secrets(self.deps.settings)
        reason = redact_secrets(f"{type(exc).__name__}: {exc}", secrets=secrets)[:2000]
        logger.warning("incident %s workflow failed: %s", incident_id, reason)
        try:
            self.deps.journal.set_status(
                incident_id, IncidentStatus.FAILED, reason=f"workflow failed: {reason}"
            )
        except DqsError as status_exc:
            # Never hide the original failure, but record that the status write
            # also failed so the operator sees both facts.
            reason = f"{reason}; additionally could not record status: {status_exc}"
        incident = self.deps.journal.get_incident(incident_id)
        return WorkflowOutcome(
            incident_id=incident_id,
            status=incident.status if incident is not None else IncidentStatus.FAILED,
            error=reason,
            checkpoint_path=str(self.checkpoint_path),
        )


def make_deps(
    settings: Settings,
    engine: Engine,
    *,
    provider: str | None = None,
    model: str | None = None,
    journal: DbJournal | None = None,
    client: LLMClient | None = None,
    budget: BudgetTracker | None = None,
    publisher: Publisher | None = None,
    tool_context: ToolContext | None = None,
    tools: list[Any] | None = None,
    project_dir: Path = PROJECT_DIR,
    contracts_dir: Path = CONTRACTS_DIR,
) -> WorkflowDeps:
    """Construct the production services for one workflow invocation.

    The lineage graph is built lazily and reused for every incident handled by
    this process; `latest_run_for_batch` still runs per incident because the
    stored run record is incident-specific.
    """
    tracker = budget or BudgetTracker.from_settings(settings)
    llm = client or LLMClient.from_provider(
        provider or settings.llm_provider,
        model or settings.llm_model,
        settings=settings,
        budget=tracker,
    )
    store = DbPublicationStore(engine)
    graph_holder: list[LineageGraph] = []

    def impact_report(incident: Incident) -> ImpactReport:
        if not graph_holder:
            graph_holder.append(LineageGraph.from_manifest(project_dir=project_dir))
        record = dbt_run.latest_run_for_batch(engine, incident.batch_id)
        return build_impact_report(graph_holder[0], incident, record)

    def sink(node: str, incident_id: uuid.UUID) -> Any:
        return DbStepSink(engine, node=node, incident_id=incident_id, settings=settings)

    return WorkflowDeps(
        settings=settings,
        journal=journal or DbJournal(engine),
        client=llm,
        budget=tracker,
        publisher=publisher or build_publisher(settings, store=store),
        sink=sink,
        impact_report=impact_report,
        engine=engine,
        tool_context=tool_context
        or ToolContext.from_settings(
            settings, contracts_dir=contracts_dir, project_dir=project_dir
        ),
        tools=tools,
        project_dir=project_dir,
        contracts_dir=contracts_dir,
    )


__all__ = ["DEFAULT_CHECKPOINT_PATH", "IncidentWorkflow", "make_deps"]
