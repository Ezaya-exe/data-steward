"""CLI wiring tests for `dqs incident run|approve|reject|status` (Phase 6b).

No Postgres and no LLM: the workflow, settings and engine are replaced at the
CLI boundary. These tests prove the commands pass the right decision/resume
payload to the shared runner and render the stored outcome; the graph itself is
covered by `test_workflow.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from typer.testing import CliRunner

from dqs import cli as cli_mod
from dqs.cli import app as dqs_app
from dqs.config import Settings
from dqs.models.enums import IncidentStatus, ResolutionType
from dqs.models.state import WorkflowOutcome
from dqs.tools.base import ToolContext

INCIDENT_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeWorkflow:
    last: _FakeWorkflow | None = None

    def __init__(self, _deps: Any) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.closed = False
        type(self).last = self

    def __enter__(self) -> _FakeWorkflow:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True

    def run(self, incident_id: uuid.UUID, *, auto_approve: bool = False) -> WorkflowOutcome:
        self.calls.append(("run", auto_approve))
        return _outcome(incident_id)

    def resume(
        self, incident_id: uuid.UUID, *, decision: str, approver: str, note: str = ""
    ) -> WorkflowOutcome:
        self.calls.append(("resume", (decision, approver, note)))
        return _outcome(incident_id, decision=decision)


def _outcome(incident_id: uuid.UUID, *, decision: str = "approve") -> WorkflowOutcome:
    return WorkflowOutcome(
        incident_id=incident_id,
        status=IncidentStatus.RESOLVED if decision == "approve" else IncidentStatus.REJECTED,
        resolution_type=ResolutionType.PR,
        checkpoint_path="data/checkpoints.sqlite",
        state={"budget": {"tool_calls_used": 1, "tokens_used": 10}},
    )


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> tuple[CliRunner, _FakeEngine, _FakeContext]:
    runner = CliRunner()
    engine = _FakeEngine()
    context = _FakeContext()
    monkeypatch.setattr(cli_mod, "load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(cli_mod.dbmod, "make_rw_engine", lambda _settings: engine)
    monkeypatch.setattr(cli_mod, "make_deps", lambda *_a, **_k: object())
    monkeypatch.setattr(cli_mod, "IncidentWorkflow", _FakeWorkflow)
    monkeypatch.setattr(_FakeWorkflow, "last", None)
    monkeypatch.setattr(
        ToolContext, "from_settings", classmethod(lambda _cls, _settings, **_k: context)
    )
    return runner, engine, context


def test_run_passes_the_auto_approve_flag_and_disposes_resources(
    cli_env: tuple[CliRunner, _FakeEngine, _FakeContext],
) -> None:
    runner, engine, context = cli_env
    result = runner.invoke(dqs_app, ["incident", "run", str(INCIDENT_ID), "--auto-approve"])

    assert result.exit_code == 0, result.output
    assert _FakeWorkflow.last is not None
    assert _FakeWorkflow.last.calls == [("run", True)]
    assert _FakeWorkflow.last.closed
    assert engine.disposed and context.closed
    assert "status=resolved" in result.output


def test_approve_resumes_with_a_note_and_a_human_approver(
    cli_env: tuple[CliRunner, _FakeEngine, _FakeContext],
) -> None:
    runner, _engine, _context = cli_env
    result = runner.invoke(
        dqs_app,
        ["incident", "approve", str(INCIDENT_ID), "--approver", "shravan", "--note", "looks good"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeWorkflow.last is not None
    assert _FakeWorkflow.last.calls == [("resume", ("approve", "shravan", "looks good"))]


def test_reject_requires_a_note(
    cli_env: tuple[CliRunner, _FakeEngine, _FakeContext],
) -> None:
    runner, _engine, _context = cli_env
    result = runner.invoke(dqs_app, ["incident", "reject", str(INCIDENT_ID), "--note", "   "])

    assert result.exit_code != 0
    assert "must not be empty" in result.output
    assert _FakeWorkflow.last is None


def test_reject_resumes_with_the_note(
    cli_env: tuple[CliRunner, _FakeEngine, _FakeContext],
) -> None:
    runner, _engine, _context = cli_env
    result = runner.invoke(
        dqs_app,
        ["incident", "reject", str(INCIDENT_ID), "--approver", "shravan", "--note", "wrong batch"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeWorkflow.last is not None
    assert _FakeWorkflow.last.calls == [("resume", ("reject", "shravan", "wrong batch"))]


def test_an_invalid_id_is_rejected_before_any_work(
    cli_env: tuple[CliRunner, _FakeEngine, _FakeContext],
) -> None:
    runner, engine, _context = cli_env
    result = runner.invoke(dqs_app, ["incident", "run", "not-a-uuid"])

    assert result.exit_code != 0
    assert "uuid" in result.output
    assert not engine.disposed
    assert _FakeWorkflow.last is None
