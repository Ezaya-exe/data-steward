"""CLI wiring tests for `dqs publish preview` (Phase 6a).

No Postgres and no GitHub: the command's dependencies are replaced at the CLI
boundary. The tests pin the live-mode refusal, the persisted-artifact
requirements and the printed preview location.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.helpers.publish import (
    INCIDENT_ID,
    make_incident,
    make_plan,
    make_verification,
)
from typer.testing import CliRunner

from dqs.cli import app as dqs_app
from dqs.config import Settings
from dqs.publish.base import PublicationKind, PublicationMode, PublicationResult
from dqs.publish.store import MemoryPublicationStore


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
def fake_cli(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"engine": _FakeEngine(), "run_called": False}
    monkeypatch.setattr("dqs.cli.load_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr("dqs.cli.dbmod.make_rw_engine", lambda settings: state["engine"])
    monkeypatch.setattr(
        "dqs.cli.incidents_mod.get_incident", lambda engine, incident_id: make_incident()
    )
    monkeypatch.setattr(
        "dqs.cli.load_latest_fix_attempt",
        lambda engine, incident_id: (make_plan(), make_verification()),
    )
    monkeypatch.setattr("dqs.cli.DbPublicationStore", lambda engine: MemoryPublicationStore())

    def fake_run(plan, incident, verification, *, store, settings):
        state["run_called"] = True
        state["plan"] = plan
        return PublicationResult(
            incident_id=incident.incident_id,
            kind=PublicationKind.PR,
            mode=PublicationMode.DRY_RUN,
            url="file:///previews/12345678.md",
        )

    monkeypatch.setattr("dqs.cli.run_dry_run", fake_run)
    return state


def test_preview_renders_from_persisted_artifacts(fake_cli: dict[str, Any]) -> None:
    result = CliRunner().invoke(dqs_app, ["publish", "preview", str(INCIDENT_ID)])

    assert result.exit_code == 0, result.output
    assert fake_cli["run_called"]
    assert "mode=dry_run kind=pr reused=False" in result.output
    assert "PR body:" in result.output
    assert "Patch:" in result.output
    assert "file:///previews/12345678.md" in result.output
    assert fake_cli["engine"].disposed


def test_preview_refuses_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dqs.cli.load_settings",
        lambda: Settings(_env_file=None, github_mode="live", github_token="t", github_repo="a/b"),
    )

    def must_not_open_a_database(settings: Settings) -> None:
        raise AssertionError("publish preview must refuse live mode before opening the DB")

    monkeypatch.setattr("dqs.cli.dbmod.make_rw_engine", must_not_open_a_database)

    result = CliRunner().invoke(dqs_app, ["publish", "preview", str(INCIDENT_ID)])

    assert result.exit_code == 1
    assert "GITHUB_MODE=live" in result.output


def test_preview_requires_a_stored_root_cause(
    fake_cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    incident = make_incident().model_copy(update={"root_cause": None})
    monkeypatch.setattr("dqs.cli.incidents_mod.get_incident", lambda engine, incident_id: incident)

    result = CliRunner().invoke(dqs_app, ["publish", "preview", str(INCIDENT_ID)])

    assert result.exit_code == 1
    assert "run investigate first" in result.output
    assert not fake_cli["run_called"]


def test_preview_requires_a_fix_attempt(
    fake_cli: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dqs.cli.load_latest_fix_attempt", lambda engine, incident_id: None)

    result = CliRunner().invoke(dqs_app, ["publish", "preview", str(INCIDENT_ID)])

    assert result.exit_code == 1
    assert "no fix attempts" in result.output


def test_preview_rejects_a_non_uuid_argument() -> None:
    result = CliRunner().invoke(dqs_app, ["publish", "preview", "not-a-uuid"])

    assert result.exit_code != 0
    assert "uuid" in result.output
