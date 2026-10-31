"""Runner orchestration tests (Phase 8a) with a scripted scenario executor.

The real per-scenario path needs Docker, dbt and optionally an LLM; these tests
replace only `execute_scenario` so the orchestration rules under test — resume,
daily caps, quota discards, holdout protection, incremental writes, cleanup —
are exercised for real. The end-to-end fake-LLM run lives in
`tests/integration/test_bench_e2e.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dqs.bench import runner
from dqs.bench.harness import prompt_hashes
from dqs.bench.report import BenchMetadata, BenchRunFile
from dqs.bench.scenarios import get_scenario
from dqs.bench.scoring import ScenarioRun
from dqs.config import Settings
from dqs.errors import DailyQuotaExceeded, DatabaseError

Execute = Callable[..., ScenarioRun]


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


@pytest.fixture(autouse=True)
def _offline_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """No git subprocess and no contract filesystem dependency."""
    monkeypatch.setattr(runner, "git_sha", lambda *_args, **_kwargs: "deadbeef")
    monkeypatch.setattr(
        runner.contracts_store, "load_approved", lambda _directory: {"orders": object()}
    )


def _options(tmp_path: Path, **overrides: Any) -> runner.BenchOptions:
    payload: dict[str, Any] = {
        "model": "test-model",
        "split": "dev",
        "results_dir": tmp_path,
        "ledger_path": tmp_path / "token_ledger.json",
        "pause_seconds": 0.0,
    }
    payload.update(overrides)
    return runner.BenchOptions(**payload)


def _result(
    scenario_id: str,
    *,
    tokens_in: int = 10,
    tokens_out: int = 5,
    error: str | None = None,
    infra_error: str | None = None,
    repeat: int = 0,
    cost: float = 0.001,
) -> ScenarioRun:
    scenario = get_scenario(scenario_id)
    return ScenarioRun(
        scenario_id=scenario.id,
        split=scenario.split.value,
        kind=scenario.kind,
        clean=scenario.is_control,
        repeat=repeat,
        error=error,
        infra_error=infra_error,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost,
        wall_s=1.5,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    fake: Execute,
) -> None:
    monkeypatch.setattr(runner, "execute_scenario", fake)


def _plain(calls: list[str], *, tokens: int = 10) -> Execute:
    def fake(
        _settings: Settings,
        _options: runner.BenchOptions,
        scenario: Any,
        **_kwargs: Any,
    ) -> ScenarioRun:
        calls.append(scenario.id)
        return _result(scenario.id, tokens_in=tokens, repeat=int(_kwargs.get("repeat", 0)))

    return fake


def test_run_writes_results_after_every_scenario_and_a_latest_report(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = ["f1-orders-status", "f2-customers-email"]
    calls: list[str] = []
    counts_seen: list[int] = []

    def fake(
        _settings: Settings,
        options: runner.BenchOptions,
        scenario: Any,
        **_kwargs: Any,
    ) -> ScenarioRun:
        calls.append(scenario.id)
        files = list(options.results_dir.glob("*.json"))
        if not files:
            counts_seen.append(0)
        else:
            payload = json.loads(files[0].read_text(encoding="utf-8"))
            counts_seen.append(len(payload["scenarios"]))
        return _result(scenario.id, tokens_in=100, tokens_out=20, cost=0.25)

    _install(monkeypatch, fake)
    run, path = runner.run_benchmark(settings, _options(tmp_path, ids=ids), sleep=lambda _s: None)

    assert calls == ids
    assert counts_seen == [0, 1], "the first scenario must be on disk before the second runs"
    assert [result.scenario_id for result in run.scenarios] == ids
    assert path.exists() and path.name.endswith("_test-model.json")
    assert (tmp_path / "latest.md").exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = payload["metadata"]
    assert metadata["model"] == "test-model"
    assert metadata["provider"] == settings.llm_provider
    assert metadata["git_sha"] == "deadbeef"
    assert metadata["prompt_hashes"] == prompt_hashes()
    assert metadata["split"] == "dev"
    assert metadata["catalogue_size"] == 2
    assert metadata["fault_count"] == 2
    assert metadata["tokens_total"] == 240
    assert metadata["cost_usd_total"] == 0.5
    assert metadata["settings"]["dbt_after_load"] is True
    assert metadata["settings"]["priced_model"] is False


def test_metadata_never_contains_secrets(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_settings = settings.model_copy(
        update={"anthropic_api_key": "sk-secret", "llm_api_key": "sk-secret"}
    )
    _install(monkeypatch, _plain([], tokens=1))
    _run, path = runner.run_benchmark(
        secret_settings,
        _options(tmp_path, ids=["f1-orders-status"]),
        sleep=lambda _s: None,
    )
    text = path.read_text(encoding="utf-8")
    assert "sk-secret" not in text
    assert "api_key" not in text


def test_resume_skips_completed_scenarios_without_rerunning_or_duplicating(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = ["f1-orders-status", "f2-customers-email"]
    first_calls: list[str] = []
    _install(monkeypatch, _plain(first_calls))
    first, path = runner.run_benchmark(
        settings, _options(tmp_path, ids=[ids[0]]), sleep=lambda _s: None
    )
    assert first_calls == [ids[0]]

    second_calls: list[str] = []
    _install(monkeypatch, _plain(second_calls))
    resumed, same_path = runner.run_benchmark(
        settings, _options(tmp_path, ids=ids, resume=path), sleep=lambda _s: None
    )

    assert second_calls == [ids[1]]
    assert same_path == path
    assert resumed.metadata.run_id == first.metadata.run_id
    assert [result.scenario_id for result in resumed.scenarios] == ids
    assert len(resumed.scenarios) == 2


def test_resume_refuses_mixing_models(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _plain([]))
    _run, path = runner.run_benchmark(
        settings, _options(tmp_path, ids=["f1-orders-status"]), sleep=lambda _s: None
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["metadata"]["model"] = "a-different-model"
    path.write_text(json.dumps(payload), encoding="utf-8")

    calls: list[str] = []
    _install(monkeypatch, _plain(calls))
    with pytest.raises(runner.BenchError, match="refusing to mix models"):
        runner.run_benchmark(
            settings,
            _options(tmp_path, ids=["f1-orders-status"], resume=path),
            sleep=lambda _s: None,
        )
    assert calls == []


def test_resume_refuses_a_different_split(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _plain([]))
    _run, path = runner.run_benchmark(
        settings, _options(tmp_path, ids=["f1-orders-status"]), sleep=lambda _s: None
    )
    with pytest.raises(runner.BenchError, match="split"):
        runner.run_benchmark(
            settings,
            _options(tmp_path, split="all", ids=["f1-orders-status"], resume=path),
            sleep=lambda _s: None,
        )


def test_holdout_requires_explicit_confirmation(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _install(monkeypatch, _plain(calls))
    with pytest.raises(runner.BenchError, match="confirm-holdout"):
        runner.run_benchmark(
            settings,
            _options(tmp_path, split="holdout", ids=["f7-orders-total-100"]),
            sleep=lambda _s: None,
        )
    assert calls == []
    assert not list(tmp_path.glob("*.json"))


def _previous_dev_file(
    tmp_path: Path,
    prompt_hashes_value: dict[str, str],
    *,
    fingerprint: str | None = None,
) -> Path:
    metadata = BenchMetadata(
        run_id="20251231T000000Z",
        model="test-model",
        provider="anthropic",
        split="dev",
        started_at="2025-12-31T00:00:00+00:00",
        finished_at="2025-12-31T01:00:00+00:00",
        git_sha="older",
        code_fingerprint=fingerprint if fingerprint is not None else runner.code_fingerprint(),
        harness_version="8a.1",
        prompt_hashes=prompt_hashes_value,
        daily_token_cap=180_000,
        tpm_limit=8_000,
        pause_seconds=0.0,
        llm_retry_max_wait_seconds=90.0,
        catalogue_size=1,
        fault_count=1,
        control_count=0,
        holdout_count=0,
    )
    path = tmp_path / "20251231T000000Z_test-model.json"
    path.write_text(
        json.dumps(BenchRunFile(metadata=metadata, scenarios=[]).model_dump(mode="json")),
        encoding="utf-8",
    )
    return path


def test_holdout_records_whether_prompts_changed_since_the_last_dev_run(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _previous_dev_file(tmp_path, {"investigator": "stale", "fixer": "stale"})
    calls: list[str] = []
    _install(monkeypatch, _plain(calls))
    run, _path = runner.run_benchmark(
        settings,
        _options(
            tmp_path,
            split="holdout",
            ids=["f7-orders-total-100"],
            confirm_holdout=True,
        ),
        sleep=lambda _s: None,
    )
    assert calls == ["f7-orders-total-100"]
    assert run.metadata.prompt_changed_since_last_dev is True
    assert run.metadata.previous_dev_run == "20251231T000000Z"
    assert run.metadata.holdout_confirmed is True
    assert run.aggregates().overall.scenarios == 1


def test_daily_token_cap_stops_before_starting_another_scenario(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    _install(monkeypatch, _plain(calls, tokens=100))
    run, path = runner.run_benchmark(
        settings,
        _options(
            tmp_path,
            ids=["f1-orders-status", "f2-customers-email"],
            daily_token_cap=50,
        ),
        sleep=lambda _s: None,
    )
    assert calls == ["f1-orders-status"]
    assert len(run.scenarios) == 1
    assert run.metadata.stop_reason is not None
    assert "daily token cap" in run.metadata.stop_reason
    assert run.metadata.resume_hint is not None
    assert "--resume" in run.metadata.resume_hint
    assert json.loads(path.read_text(encoding="utf-8"))["metadata"]["stop_reason"]


def test_daily_quota_is_recorded_as_infra_and_stops_immediately(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = ["f1-orders-status", "f2-customers-email"]
    calls: list[str] = []

    def fake(
        _settings: Settings,
        _options: runner.BenchOptions,
        scenario: Any,
        **_kwargs: Any,
    ) -> ScenarioRun:
        calls.append(scenario.id)
        if scenario.id == ids[1]:
            raise DailyQuotaExceeded("daily request quota exhausted")
        return _result(scenario.id)

    _install(monkeypatch, fake)
    run, path = runner.run_benchmark(settings, _options(tmp_path, ids=ids), sleep=lambda _s: None)

    assert calls == ids
    assert [result.scenario_id for result in run.scenarios] == ids
    assert run.scenarios[1].infra_error is not None
    assert "daily" in run.scenarios[1].infra_error.lower()
    assert run.metadata.stop_reason is not None
    assert "daily provider quota" in run.metadata.stop_reason
    assert run.metadata.tokens_total == 15  # only the completed scenario's tokens
    assert run.aggregates().overall.scenarios == 1  # infra failures are not scored
    assert run.aggregates().overall.infra_errors == 1
    assert run.metadata.discarded_scenarios == []
    assert (tmp_path / "latest.md").exists()
    assert (
        "daily provider quota"
        in json.loads(path.read_text(encoding="utf-8"))["metadata"]["stop_reason"]
    )


def test_non_quota_errors_are_recorded_not_dropped(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = ["f1-orders-status", "f2-customers-email"]

    def fake(
        _settings: Settings,
        _options: runner.BenchOptions,
        scenario: Any,
        **_kwargs: Any,
    ) -> ScenarioRun:
        error = "LLMError: transient" if scenario.id == ids[0] else None
        return _result(scenario.id, error=error)

    _install(monkeypatch, fake)
    run, _path = runner.run_benchmark(settings, _options(tmp_path, ids=ids), sleep=lambda _s: None)
    assert [result.scenario_id for result in run.scenarios] == ids
    assert run.scenarios[0].error == "LLMError: transient"
    assert run.metadata.stop_reason is None
    assert run.aggregates().overall.errors == 1


def test_live_github_mode_is_forced_to_dry_run(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = settings.model_copy(update={"github_mode": "live"})
    seen: list[str] = []
    calls: list[str] = []

    def fake(
        effective: Settings,
        _options: runner.BenchOptions,
        scenario: Any,
        **_kwargs: Any,
    ) -> ScenarioRun:
        seen.append(effective.github_mode)
        calls.append(scenario.id)
        return _result(scenario.id)

    _install(monkeypatch, fake)
    run, path = runner.run_benchmark(live, _options(tmp_path, ids=["f1-orders-status"]))

    assert calls == ["f1-orders-status"]
    assert seen == ["dry_run"], "the benchmark always owns the effective GitHub mode"
    metadata = json.loads(path.read_text(encoding="utf-8"))["metadata"]
    assert metadata["settings"]["github_mode_env"] == "live"
    assert metadata["settings"]["github_mode"] == "dry_run"
    assert metadata["settings"]["github_mode_forced"] is True


def test_missing_contracts_are_refused(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.contracts_store, "load_approved", lambda _directory: {})
    calls: list[str] = []
    _install(monkeypatch, _plain(calls))
    with pytest.raises(runner.BenchError, match="no approved contracts"):
        runner.run_benchmark(settings, _options(tmp_path, ids=["f1-orders-status"]))
    assert calls == []


def test_scenario_selection_validates_ids_split_and_limit(tmp_path: Path) -> None:
    with pytest.raises(runner.BenchError, match="unknown scenario id"):
        runner._select_scenarios(_options(tmp_path, ids=["nope"]))
    with pytest.raises(runner.BenchError, match="is holdout, not dev"):
        runner._select_scenarios(_options(tmp_path, ids=["f7-orders-total-100"]))
    assert runner._select_scenarios(_options(tmp_path, limit=0)) == []
    dev = runner._select_scenarios(_options(tmp_path))
    assert dev and all(scenario.split.value == "dev" for scenario in dev)
    assert len(runner._select_scenarios(_options(tmp_path, split="all"))) == len(
        runner.scenarios_for("all")
    )


def test_token_throttle_sleeps_a_full_window_when_over_the_limit() -> None:
    now = [0.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    throttle = runner.TokenThrottle(10, clock=clock, sleep=sleep)
    throttle.record(6)
    throttle.record(6)
    assert throttle.wait() == pytest.approx(60.0)
    assert slept == [pytest.approx(60.0)]

    throttle.record(5)
    assert throttle.wait() == 0.0
    assert len(slept) == 1


def test_execute_scenario_drops_its_database_even_when_the_load_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    dropped: list[str] = []

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(runner.fast_reset, "clone_template", lambda _settings, _target: None)
    monkeypatch.setattr(runner.sim_pipeline, "load_day", boom)
    monkeypatch.setattr(
        runner.fast_reset, "drop_database", lambda _settings, name: dropped.append(name) or True
    )

    result = runner.execute_scenario(
        settings,
        _options(Path("."), ids=None),
        get_scenario("f1-orders-status"),
        contracts={},
    )

    scenario_db = "dqs_bench_f1_orders_status"
    assert dropped == [scenario_db]
    assert result.scenario_id == "f1-orders-status"
    assert result.error is not None
    assert "RuntimeError: loader exploded" in result.error
    assert result.detected is False


def test_execute_scenario_no_dbt_passes_the_flag_and_needs_no_llm(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}
    dropped: list[str] = []
    disposed: list[bool] = []
    fake_engine = SimpleNamespace(dispose=lambda: disposed.append(True))

    def fake_load(_settings: Settings, _date: Any, _seed: int, **kwargs: Any) -> None:
        calls.update(kwargs)

    monkeypatch.setattr(
        runner.fast_reset,
        "clone_template",
        lambda _settings, target: calls.update({"target": target}),
    )
    monkeypatch.setattr(runner.sim_pipeline, "load_day", fake_load)
    monkeypatch.setattr(runner.dbmod, "make_rw_engine", lambda _settings: fake_engine)
    monkeypatch.setattr(
        runner.monitor_mod,
        "run_monitor",
        lambda _engine, _contracts, _as_of: SimpleNamespace(created=[]),
    )
    monkeypatch.setattr(
        runner.fast_reset, "drop_database", lambda _settings, name: dropped.append(name) or True
    )

    result = runner.execute_scenario(
        settings,
        runner.BenchOptions(model="test-model", no_dbt=True),
        get_scenario("f9-orders"),
        contracts={},
    )

    assert calls["run_dbt"] is False
    assert calls["target"] == "dqs_bench_f9_orders"
    assert dropped == ["dqs_bench_f9_orders"]
    assert disposed == [True]
    assert result.error is None
    assert result.detected is False


def test_execute_scenario_rejects_the_warehouse_name_before_touching_postgres(
    settings: Settings,
) -> None:
    with pytest.raises(DatabaseError):
        runner.fast_reset.clone_database(settings, "dqs_template", "warehouse")


def test_execute_scenario_records_a_null_plan_attempt_without_an_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored attempt with no plan is a recorded outcome, not a scenario error.

    Dev run 2 (`f6-customers-08`) published the issue and then failed the whole
    scenario because `load_latest_fix_attempt` raised on `plan=NULL`, losing the
    token counters. Here the same shape must be scored with its tokens intact.
    """
    import uuid

    from dqs.agents.llm import BudgetTracker, LLMCallRecord
    from dqs.models.enums import ResolutionType
    from dqs.models.fix import CriterionResult, VerificationResult
    from dqs.publish.store import StoredFixAttempt

    incident_id = uuid.uuid4()
    verification = VerificationResult.from_criteria(
        [CriterionResult(name="fixer_output_invalid", passed=False, detail="empty reply")]
    )
    stored = StoredFixAttempt(attempt_no=3, plan=None, verification=verification)

    tracker = BudgetTracker(max_tool_calls=10, max_tokens=100_000, timeout_seconds=900)
    tracker.record_tool_call()
    fake_client = SimpleNamespace(
        budget=tracker,
        calls=[
            LLMCallRecord(
                provider="fake",
                model="fake-1",
                latency_ms=1,
                input_tokens=123,
                output_tokens=45,
                total_tokens=168,
            )
        ],
    )

    class _FakeToolContext:
        @classmethod
        def from_settings(cls, *_args: Any, **_kwargs: Any) -> _FakeToolContext:
            return cls()

        def close(self) -> None:
            pass

    class _FakeWorkflow:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeWorkflow:
            return self

        def __exit__(self, *_exc: Any) -> bool:
            return False

        def run(self, _incident_id: Any, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                resolution_type=ResolutionType.ISSUE, error=None, interrupted=False
            )

    fake_engine = SimpleNamespace(dispose=lambda: None)
    monkeypatch.setattr(runner.fast_reset, "clone_template", lambda *_a, **_k: None)
    monkeypatch.setattr(runner.sim_pipeline, "load_day", lambda *_a, **_k: None)
    monkeypatch.setattr(runner.dbmod, "make_rw_engine", lambda _settings: fake_engine)
    monkeypatch.setattr(runner.fast_reset, "drop_database", lambda *_a, **_k: True)
    monkeypatch.setattr(
        runner.monitor_mod, "run_monitor", lambda *_a, **_k: SimpleNamespace(created=[incident_id])
    )
    monkeypatch.setattr(
        runner.incidents_mod,
        "get_incident",
        lambda _engine, _incident_id: SimpleNamespace(
            incident_id=incident_id,
            table="orders",
            violations=[SimpleNamespace(check="schema.missing_column")],
        ),
    )
    monkeypatch.setattr(runner, "ToolContext", _FakeToolContext)
    monkeypatch.setattr(runner, "make_deps", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setattr(runner, "IncidentWorkflow", _FakeWorkflow)
    monkeypatch.setattr(runner, "load_fix_history", lambda *_a, **_k: (3, ["fixer_output_invalid"]))
    monkeypatch.setattr(runner, "_safe_step_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "_root_cause_from_incident", lambda *_a, **_k: None)
    monkeypatch.setattr(runner, "load_latest_fix_attempt", lambda *_a, **_k: stored)

    result = runner.execute_scenario(
        settings,
        runner.BenchOptions(model="test-model", no_dbt=True),
        get_scenario("f1-orders-status"),
        contracts={},
        client_factory=lambda *_a, **_k: fake_client,
    )

    assert result.error is None
    assert result.infra_error is None
    assert result.verifier_passed is False
    assert result.fix_attempts == 3
    assert result.failed_criteria == ["fixer_output_invalid"]
    assert result.tokens_in == 123
    assert result.tokens_out == 45


def test_two_consecutive_infra_errors_trip_the_circuit_breaker(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = ["f1-orders-status", "f2-customers-email", "f4-orders-status-20"]
    calls: list[str] = []

    def fake(
        _settings: Settings,
        _options: runner.BenchOptions,
        scenario: Any,
        **_kwargs: Any,
    ) -> ScenarioRun:
        calls.append(scenario.id)
        return _result(scenario.id, infra_error="HTTP 429 rate limit after retries")

    _install(monkeypatch, fake)
    run, path = runner.run_benchmark(settings, _options(tmp_path, ids=ids), sleep=lambda _s: None)

    assert calls == ids[:2]
    assert run.metadata.stop_reason is not None
    assert "circuit breaker" in run.metadata.stop_reason
    assert run.metadata.resume_hint is not None
    assert "--resume" in run.metadata.resume_hint
    assert run.aggregates().overall.infra_errors == 2
    assert run.aggregates().overall.scenarios == 0
    assert json.loads(path.read_text(encoding="utf-8"))["metadata"]["stop_reason"]

    # A resume re-runs infra records automatically (they were never scored).
    resumed_calls: list[str] = []
    _install(monkeypatch, _plain(resumed_calls))
    resumed, same_path = runner.run_benchmark(
        settings, _options(tmp_path, ids=ids, resume=path), sleep=lambda _s: None
    )
    assert same_path == path
    assert resumed_calls == ids
    assert [result.scenario_id for result in resumed.scenarios] == ids
    assert all(result.infra_error is None for result in resumed.scenarios)


def test_daily_token_ledger_accumulates_across_invocations(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "token_ledger.json"
    ledger_path.unlink(missing_ok=True)
    first_calls: list[str] = []
    _install(monkeypatch, _plain(first_calls, tokens=100))
    _first, _path = runner.run_benchmark(
        settings,
        _options(tmp_path, ids=["f1-orders-status"], ledger_path=ledger_path),
        sleep=lambda _s: None,
    )
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert sum(payload["days"][next(iter(payload["days"]))].values()) == 105

    second_calls: list[str] = []
    progress: list[str] = []
    _install(monkeypatch, _plain(second_calls, tokens=100))
    run, _ = runner.run_benchmark(
        settings,
        _options(
            tmp_path,
            ids=["f2-customers-email"],
            ledger_path=ledger_path,
            daily_token_cap=105,
        ),
        progress=progress.append,
        sleep=lambda _s: None,
    )
    assert second_calls == [], "the ledger must account for the earlier invocation"
    assert run.metadata.stop_reason is not None
    assert "daily token cap" in run.metadata.stop_reason
    assert any("token ledger: 105" in line for line in progress)


def test_repeats_run_each_scenario_n_times_with_distinct_keys(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []

    def fake(
        _settings: Settings,
        _options: runner.BenchOptions,
        scenario: Any,
        **kwargs: Any,
    ) -> ScenarioRun:
        repeat = int(kwargs["repeat"])
        calls.append((scenario.id, repeat))
        return _result(scenario.id, repeat=repeat)

    _install(monkeypatch, fake)
    run, path = runner.run_benchmark(
        settings,
        _options(tmp_path, ids=["f1-orders-status"], repeats=3),
        sleep=lambda _s: None,
    )
    assert calls == [("f1-orders-status", 0), ("f1-orders-status", 1), ("f1-orders-status", 2)]
    assert [result.repeat for result in run.scenarios] == [0, 1, 2]
    assert run.metadata.repeats == 3
    assert json.loads(path.read_text(encoding="utf-8"))["metadata"]["repeats"] == 3


def test_resume_refuses_a_different_repeat_count(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _plain([]))
    _run, path = runner.run_benchmark(
        settings,
        _options(tmp_path, ids=["f1-orders-status"], repeats=2),
        sleep=lambda _s: None,
    )
    with pytest.raises(runner.BenchError, match="repeats"):
        runner.run_benchmark(
            settings,
            _options(tmp_path, ids=["f1-orders-status"], repeats=3, resume=path),
            sleep=lambda _s: None,
        )


def test_holdout_refuses_a_changed_fingerprint_unless_acknowledged(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _previous_dev_file(tmp_path, prompt_hashes(), fingerprint="f" * 64)
    calls: list[str] = []
    _install(monkeypatch, _plain(calls))
    options = _options(tmp_path, split="holdout", ids=["f7-orders-total-100"], confirm_holdout=True)
    with pytest.raises(runner.BenchError, match="fingerprint changed"):
        runner.run_benchmark(settings, options, sleep=lambda _s: None)
    assert calls == []

    run, _path = runner.run_benchmark(
        settings,
        options.model_copy(update={"acknowledge_changes": "refactored the runner"}),
        sleep=lambda _s: None,
    )
    assert calls == ["f7-orders-total-100"]
    assert run.metadata.acknowledged_changes == "refactored the runner"
