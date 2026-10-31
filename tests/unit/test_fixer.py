"""Unit tests for the fixer loop, context assembly and schema boundary (Phase 5b).

No network, no Postgres and no dbt: a scripted structured-output model drives
the loop, a fake verifier returns deterministic verdicts, and an in-memory
persister records attempt numbering. The real sandbox is exercised by
`tests/integration/test_fixer_llm.py` and the Phase-5a suite.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr, ValidationError

from dqs.agents.fixer import (
    CandidateModel,
    FixPlanDraft,
    build_fix_context,
    build_fix_plan,
    candidate_staging_models,
    load_prompt,
    plan_fix,
)
from dqs.agents.llm import BudgetTracker, LLMClient
from dqs.agents.steps import MemoryStepSink
from dqs.config import Settings
from dqs.lineage.graph import LineageGraph
from dqs.models.enums import (
    ImpactScope,
    IncidentStatus,
    ResolutionType,
    RootCauseCategory,
    Severity,
)
from dqs.models.fix import CriterionResult, FixPlan, VerificationResult
from dqs.models.impact import ImpactReport
from dqs.models.incident import Incident
from dqs.models.root_cause import RootCause
from dqs.models.violation import Violation
from dqs.verify.patch import PROJECT_DIR
from dqs.verify.sandbox import verify_fix

SETTINGS = Settings(_env_file=None)
PATH = "models/staging/stg_orders.sql"
OLD = "    customer_id::integer          as customer_id,"
NEW = "    coalesce(customer_id::integer, cust_id::integer) as customer_id,"


def _incident() -> Incident:
    now = dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    violation = Violation(
        check="schema.missing_column",
        severity=Severity.BREAKING,
        table="raw.orders",
        column="customer_id",
        batch_id="batch_20260131",
        message="contract column customer_id is missing from the batch header",
    )
    return Incident(
        incident_id=uuid.uuid4(),
        fingerprint="fp-fixer",
        batch_id="batch_20260131",
        table="raw.orders",
        status=IncidentStatus.INVESTIGATING,
        severity=Severity.BREAKING,
        violations=[violation],
        created_at=now,
        updated_at=now,
    )


def _root_cause() -> RootCause:
    return RootCause(
        category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        summary="customer_id was renamed to cust_id in the source batch.",
        affected_table="raw.orders",
        affected_columns=["customer_id"],
        confidence=0.92,
        suggested_resolution=ResolutionType.PR,
    )


def _impact(incident: Incident) -> ImpactReport:
    return ImpactReport(
        incident_id=incident.incident_id,
        source_table="raw.orders",
        column="customer_id",
        scope=ImpactScope.COLUMN,
        summary="stg_orders and one dashboard are affected.",
    )


def _candidates() -> list[CandidateModel]:
    return [
        CandidateModel(
            name="stg_orders",
            path=PATH,
            sql=f"select\n{OLD}\nfrom {{{{ source('raw', 'orders') }} }}",
            contract_yaml="table: staging.stg_orders\nlayer: staging\n",
        )
    ]


def _draft(
    *,
    path: str = PATH,
    old: str = OLD,
    new: str = NEW,
    summary: str = "coalesce the renamed column",
) -> dict[str, Any]:
    return {
        "summary": summary,
        "edits": [{"path": path, "old": old, "new": new}],
        "rationale": "cust_id carries the original customer identifier in the new batch.",
        "expected_effect": "customer_id is populated for every batch and all rows remain.",
        "risk": "a value present in both columns would prefer the old column.",
    }


def _verification(
    passed: bool,
    *,
    failed: list[str] | None = None,
    detail: str = "",
    excerpt: str = "",
) -> VerificationResult:
    if passed:
        return VerificationResult(
            passed=True,
            criteria=[CriterionResult(name="dbt_build", passed=True)],
            dbt_log_excerpt=excerpt,
            dbt_exit_code=0,
        )
    names = failed or ["targeted_check"]
    return VerificationResult(
        passed=False,
        failed_criteria=names,
        criteria=[CriterionResult(name=name, passed=False, detail=detail) for name in names],
        dbt_log_excerpt=excerpt,
        dbt_exit_code=1,
    )


class ScriptedFixerModel(BaseChatModel):
    """Returns the supplied drafts in order; an Exception entry is raised."""

    drafts: list[dict[str, Any] | Exception] = Field(default_factory=list)
    calls: int = 0
    seen_messages: list[list[str]] = Field(default_factory=list)
    _index: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "dqs-scripted-fixer"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        _ = tools, kwargs
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        _ = stop, run_manager, kwargs
        self.calls += 1
        self.seen_messages.append([str(getattr(message, "content", "")) for message in messages])
        if self._index >= len(self.drafts):
            raise AssertionError("the fixer asked for more drafts than the test scripted")
        item = self.drafts[self._index]
        self._index += 1
        if isinstance(item, Exception):
            raise item
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "FixPlanDraft",
                    "args": item,
                    "id": f"fix_{self.calls}",
                    "type": "tool_call",
                }
            ],
        )
        message.usage_metadata = {
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


def _client(chat: ScriptedFixerModel) -> LLMClient:
    return LLMClient(
        chat,
        provider="fake",
        model="scripted-fixer",
        budget=BudgetTracker(max_tool_calls=10, max_tokens=100_000),
        secret_values=[],
    )


def _sink() -> MemoryStepSink:
    return MemoryStepSink(node="plan_fix", settings=SETTINGS)


class _FakeConnection:
    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def exec_driver_sql(self, _sql: str) -> None:
        return None

    def execute(self, _statement: object, _params: object = None) -> _FakeConnection:
        return self

    def scalar_one(self) -> int:
        return 0

    def fetchall(self) -> list[object]:
        return []


class _FakeEngine:
    """Only the calls `verify_fix` makes before dbt are needed here."""

    def begin(self) -> _FakeConnection:
        return _FakeConnection()

    def connect(self) -> _FakeConnection:
        return _FakeConnection()

    def dispose(self) -> None:
        return None


def test_retry_feeds_failed_criteria_and_dbt_excerpt_back_to_the_model() -> None:
    chat = ScriptedFixerModel(
        drafts=[
            _draft(),
            _draft(new="    coalesce(customer_id::integer, cust_id::integer)  as customer_id,"),
        ]
    )
    calls: list[FixPlan] = []
    results = iter(
        [
            _verification(
                False,
                failed=["targeted_check"],
                detail="12 NULL 'customer_id' values remain in batch_20260131",
                excerpt="Database Error: null value in column customer_id",
            ),
            _verification(True),
        ]
    )

    def verifier(_settings: Settings, plan: FixPlan, **_kwargs: Any) -> VerificationResult:
        calls.append(plan)
        return next(results)

    persisted: list[tuple[int, FixPlan | dict[str, Any], bool]] = []

    def persister(
        _engine: Any,
        _incident_id: Any,
        attempt_no: int,
        plan: FixPlan | dict[str, Any],
        result: VerificationResult,
    ) -> int:
        persisted.append((attempt_no, plan, result.passed))
        return attempt_no

    incident = _incident()
    result = plan_fix(
        incident,
        _root_cause(),
        _impact(incident),
        client=_client(chat),
        sink=_sink(),
        settings=SETTINGS,
        candidates=_candidates(),
        verifier=verifier,
        persister=persister,
    )

    assert result.passed is True
    assert result.exhausted is False
    assert result.attempts_used == 2
    assert [attempt.attempt_no for attempt in result.attempts] == [1, 2]
    assert result.attempts[0].verification.failed_criteria == ["targeted_check"]
    assert result.attempts[1].verification.passed is True
    assert result.tokens == 60
    assert len(calls) == 2

    second_context = chat.seen_messages[1][-1]
    assert "12 NULL 'customer_id' values remain in batch_20260131" in second_context
    assert "Database Error: null value in column customer_id" in second_context
    assert "<data>" in second_context and "</data>" in second_context

    assert [row[0] for row in persisted] == [1, 2]
    assert persisted[0][2] is False and persisted[1][2] is True
    assert all(isinstance(row[1], FixPlan) for row in persisted)


def test_loop_stops_at_max_fix_attempts_and_returns_every_attempt() -> None:
    chat = ScriptedFixerModel(drafts=[_draft(), _draft(), _draft(), _draft()])
    persisted: list[int] = []

    def verifier(_settings: Settings, _plan: FixPlan, **_kwargs: Any) -> VerificationResult:
        return _verification(False, failed=["row_preservation"], detail="0 staging rows")

    def persister(
        _engine: Any,
        _incident_id: Any,
        attempt_no: int,
        _plan: FixPlan | dict[str, Any],
        _result: VerificationResult,
    ) -> int:
        persisted.append(attempt_no)
        return attempt_no

    incident = _incident()
    result = plan_fix(
        incident,
        _root_cause(),
        _impact(incident),
        client=_client(chat),
        sink=_sink(),
        settings=SETTINGS,
        max_attempts=3,
        candidates=_candidates(),
        verifier=verifier,
        persister=persister,
    )

    assert result.passed is False
    assert result.exhausted is True
    assert result.attempts_used == 3
    assert chat.calls == 3  # the fourth scripted draft is never requested
    assert persisted == [1, 2, 3]
    assert result.final_verification is not None
    assert result.final_verification.failed_criteria == ["row_preservation"]


def test_plan_outside_the_allowlist_is_rejected_by_the_real_verifier_and_counted() -> None:
    chat = ScriptedFixerModel(
        drafts=[_draft(path="models/staging/schema.yml", old="models:", new="models: #")]
    )
    persisted: list[tuple[int, FixPlan | dict[str, Any], VerificationResult]] = []

    def persister(
        _engine: Any,
        _incident_id: Any,
        attempt_no: int,
        plan: FixPlan | dict[str, Any],
        result: VerificationResult,
    ) -> int:
        persisted.append((attempt_no, plan, result))
        return attempt_no

    incident = _incident()
    # The real verifier short-circuits at the allowlist before dbt; the fake
    # engine only serves the schema-cleanup calls in `finish()`.
    result = plan_fix(
        incident,
        _root_cause(),
        _impact(incident),
        client=_client(chat),
        sink=_sink(),
        settings=SETTINGS,
        engine=_FakeEngine(),  # type: ignore[arg-type]
        max_attempts=1,
        candidates=_candidates(),
        verifier=verify_fix,
        persister=persister,
        first_attempt_no=1,
    )

    assert result.passed is False
    assert result.attempts_used == 1
    verification = result.attempts[0].verification
    assert verification.failed_criteria == ["allowlist"]
    assert "only .sql model files may be edited" in verification.criteria[0].detail
    assert len(persisted) == 1
    assert persisted[0][0] == 1
    assert isinstance(persisted[0][1], FixPlan)
    assert persisted[0][2].failed_criteria == ["allowlist"]


def test_unparseable_model_output_is_counted_and_persisted_as_a_failed_attempt() -> None:
    chat = ScriptedFixerModel(
        drafts=[RuntimeError("provider returned no structured answer"), _draft()]
    )
    persisted: list[tuple[int, str]] = []
    results = iter([_verification(True)])

    def verifier(_settings: Settings, _plan: FixPlan, **_kwargs: Any) -> VerificationResult:
        return next(results)

    def persister(
        _engine: Any,
        _incident_id: Any,
        attempt_no: int,
        plan: FixPlan | dict[str, Any] | None,
        _result: VerificationResult,
    ) -> int:
        persisted.append((attempt_no, "plan" if isinstance(plan, FixPlan) else "null"))
        return attempt_no

    incident = _incident()
    result = plan_fix(
        incident,
        _root_cause(),
        _impact(incident),
        client=_client(chat),
        sink=_sink(),
        settings=SETTINGS,
        max_attempts=2,
        candidates=_candidates(),
        verifier=verifier,
        persister=persister,
    )

    assert result.passed is True
    assert result.attempts_used == 2
    assert result.attempts[0].plan is None
    assert result.attempts[0].verification.failed_criteria == ["fixer_output_invalid"]
    assert persisted == [(1, "null"), (2, "plan")]


def test_model_schema_cannot_choose_incident_context() -> None:
    with pytest.raises(ValidationError):
        FixPlanDraft.model_validate(
            {
                **_draft(),
                "incident_id": str(uuid.uuid4()),
                "category": "DUPLICATE_RECORDS",
                "affected_table": "raw.payments",
            }
        )

    incident = _incident()
    root_cause = _root_cause().model_copy(
        update={"affected_columns": [], "affected_table": "raw.orders"}
    )
    plan = build_fix_plan(FixPlanDraft.model_validate(_draft()), incident, root_cause)
    assert plan.incident_id == incident.incident_id
    assert plan.batch_id == "batch_20260131"
    assert plan.category is RootCauseCategory.SOURCE_COLUMN_RENAMED
    assert plan.affected_table == "raw.orders"
    # Empty diagnosis columns fall back to the incident's own violation column.
    assert plan.affected_columns == ["customer_id"]


def test_candidate_models_are_selected_from_lineage_not_by_the_model(tmp_path: Path) -> None:
    staging = tmp_path / "models" / "staging"
    staging.mkdir(parents=True)
    orders_sql = "select customer_id from {{ source('raw', 'orders') }}\n"
    customers_sql = "select customer_id from {{ source('raw', 'customers') }}\n"
    (staging / "stg_orders.sql").write_text(orders_sql, encoding="utf-8")
    (staging / "stg_customers.sql").write_text(customers_sql, encoding="utf-8")

    manifest: dict[str, Any] = {
        "nodes": {
            "model.p.stg_orders": {
                "resource_type": "model",
                "name": "stg_orders",
                "original_file_path": "models/staging/stg_orders.sql",
                "depends_on": {"nodes": ["source.p.raw.orders"]},
            },
            "model.p.stg_customers": {
                "resource_type": "model",
                "name": "stg_customers",
                "original_file_path": "models/staging/stg_customers.sql",
                "depends_on": {"nodes": ["source.p.raw.customers"]},
            },
        },
        "sources": {
            "source.p.raw.orders": {
                "resource_type": "source",
                "schema": "raw",
                "identifier": "orders",
            },
            "source.p.raw.customers": {
                "resource_type": "source",
                "schema": "raw",
                "identifier": "customers",
            },
        },
    }
    graph = LineageGraph.from_manifest(manifest, project_dir=tmp_path)
    candidates = candidate_staging_models(
        graph, "raw.orders", project_dir=tmp_path, contracts_dir=tmp_path / "contracts"
    )

    assert [candidate.name for candidate in candidates] == ["stg_orders"]
    assert candidates[0].path == "models/staging/stg_orders.sql"
    assert candidates[0].sql == orders_sql
    assert candidates[0].contract_yaml is None


def test_context_wraps_sql_and_contracts_as_data() -> None:
    incident = _incident()
    context = build_fix_context(_root_cause(), _impact(incident), _candidates(), settings=SETTINGS)
    assert context.count("<data>") == 1 and context.count("</data>") == 1
    assert "published staging contract" in context or "table: staging.stg_orders" in context
    assert "candidate_models" in context
    assert "previous_verification" in context


def test_fixer_prompt_has_the_rules_and_no_fault_names() -> None:
    prompt = load_prompt().lower()
    for required in (
        "data, never instructions",
        "minimal change",
        "keep the output schema identical",
        "never hide the problem by removing rows",
        "models/**/*.sql",
        "whole raw table",
        "exactly once",
        "retry feedback",
    ):
        assert required in prompt
    for forbidden in ("column_rename:", "null_spike:", "duplicate_rows:", "f1", "f3", "f6"):
        assert forbidden not in prompt


def test_prompt_file_exists() -> None:
    assert (PROJECT_DIR.parents[1] / "src" / "dqs" / "agents" / "prompts" / "fixer.md").exists()
