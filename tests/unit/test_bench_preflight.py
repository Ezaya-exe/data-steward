"""`dqs bench preflight` tests (Phase 8b).

Preflight must be read-only and must never leak a key. These tests inject every
external probe (Postgres ping, template cache, disk) and assert both the PASS
path and each FAIL reason, so a broken check cannot hide behind a green run.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from dqs import fast_reset
from dqs.bench import preflight
from dqs.bench.preflight import CheckStatus, run_preflight
from dqs.config import Settings
from dqs.errors import DatabaseError
from dqs.models.enums import ContractStatus


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        anthropic_api_key="sk-super-secret",
        llm_provider="anthropic",
        llm_model_bench="claude-test",
    )


@pytest.fixture(autouse=True)
def _approved_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.contracts_store,
        "load_contracts",
        lambda _directory: {"orders": SimpleNamespace(status=ContractStatus.APPROVED)},
    )


def _run(settings: Settings, **overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "model": "claude-test",
        "ping": lambda _settings: {"rw": "PostgreSQL 16.4", "ro": "PostgreSQL 16.4"},
        "template_exists": lambda _settings: True,
        "template_meta": lambda _settings: {
            "content_hash": fast_reset.content_hash(),
            "seed": fast_reset.SEED,
            "baseline_days": fast_reset.BASELINE_DAYS,
        },
        "disk_usage": lambda _path: SimpleNamespace(free=10 * 1024**3),
    }
    payload.update(overrides)
    return run_preflight(settings, **payload)


def _check(report: Any, name: str) -> Any:
    return next(check for check in report.checks if check.name == name)


def test_every_check_passes_on_a_ready_machine(settings: Settings) -> None:
    report = _run(settings)
    assert report.ok
    assert {check.status for check in report.checks} == {CheckStatus.PASS}
    assert "preflight" not in report.render()  # render is only the check lines


def test_api_key_is_never_printed(settings: Settings) -> None:
    report = _run(settings)
    rendered = report.render()
    assert "sk-super-secret" not in rendered
    assert "value not printed" in rendered
    assert _check(report, "LLM API key").status is CheckStatus.PASS


def test_missing_api_key_is_a_failure_with_the_variable_name() -> None:
    settings = Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="")
    report = _run(settings)
    check = _check(report, "LLM API key")
    assert check.status is CheckStatus.FAIL
    assert "ANTHROPIC_API_KEY" in check.detail
    assert not report.ok


def test_fake_provider_needs_no_key() -> None:
    settings = Settings(_env_file=None, llm_provider="fake", llm_model_bench="fake")
    report = _run(settings)
    assert _check(report, "LLM API key").status is CheckStatus.PASS


def test_live_github_mode_is_reported_as_forced_dry_run(settings: Settings) -> None:
    live = settings.model_copy(update={"github_mode": "live"})
    check = _check(_run(live), "GITHUB_MODE effective")
    assert check.status is CheckStatus.PASS
    assert "forced by the benchmark" in check.detail
    assert "dry_run" in check.detail


def test_a_missing_template_is_safe_to_build_on_the_first_run(settings: Settings) -> None:
    report = _run(settings, template_exists=lambda _settings: False)
    check = _check(report, "dqs_template cached/current")
    assert check.status is CheckStatus.PASS
    assert "first benchmark run will build it" in check.detail


def test_a_stale_template_is_a_failure(settings: Settings) -> None:
    stale = lambda _settings: {  # noqa: E731 - a literal injected probe
        "content_hash": "f" * 64,
        "seed": fast_reset.SEED,
        "baseline_days": fast_reset.BASELINE_DAYS,
    }
    check = _check(_run(settings, template_meta=stale), "dqs_template cached/current")
    assert check.status is CheckStatus.FAIL
    assert "stale" in check.detail


def test_no_contracts_is_a_failure(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.contracts_store, "load_contracts", lambda _directory: {})
    check = _check(_run(settings), "contracts approved")
    assert check.status is CheckStatus.FAIL
    assert "contracts approve --all" in check.detail


def test_a_draft_contract_is_a_failure(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight.contracts_store,
        "load_contracts",
        lambda _directory: {"orders": SimpleNamespace(status=ContractStatus.DRAFT)},
    )
    check = _check(_run(settings), "contracts approved")
    assert check.status is CheckStatus.FAIL
    assert "orders" in check.detail


def test_empty_bench_model_is_a_failure() -> None:
    settings = Settings(_env_file=None, anthropic_api_key="x", llm_model_bench="")
    check = _check(_run(settings, model=None), "benchmark model")
    assert check.status is CheckStatus.FAIL
    assert "LLM_MODEL_BENCH" in check.detail


def test_postgres_and_disk_failures_are_reported_not_raised(settings: Settings) -> None:
    def broken_ping(_settings: Settings) -> dict[str, str]:
        raise DatabaseError("connection refused")

    report = _run(
        settings,
        ping=broken_ping,
        disk_usage=lambda _path: SimpleNamespace(free=0),
    )
    assert _check(report, "postgres healthy").status is CheckStatus.FAIL
    assert "connection refused" in _check(report, "postgres healthy").detail
    assert _check(report, "free disk space").status is CheckStatus.FAIL
    assert "need at least 1.0 GiB" in _check(report, "free disk space").detail
    assert not report.ok


def test_harness_check_reports_a_frozen_version_and_prompt_hashes(settings: Settings) -> None:
    check = _check(_run(settings), "harness frozen")
    assert check.status is CheckStatus.PASS
    assert "investigator=" in check.detail
    assert "fixer=" in check.detail


def test_preflight_makes_no_llm_or_database_write_calls(settings: Settings) -> None:
    """The injected probes are the only calls; no real engine is constructed."""
    calls: list[str] = []

    def ping(_settings: Settings) -> dict[str, str]:
        calls.append("ping")
        return {"rw": "PostgreSQL 16.4"}

    def exists(_settings: Settings) -> bool:
        calls.append("exists")
        return True

    def meta(_settings: Settings) -> dict[str, object]:
        calls.append("meta")
        return {
            "content_hash": fast_reset.content_hash(),
            "seed": fast_reset.SEED,
            "baseline_days": fast_reset.BASELINE_DAYS,
        }

    _run(settings, ping=ping, template_exists=exists, template_meta=meta)
    assert calls == ["ping", "exists", "meta"]


def test_report_is_ascii_safe() -> None:
    settings = Settings(_env_file=None, llm_provider="fake", llm_model_bench="fake")
    _run(settings).render().encode("ascii")
