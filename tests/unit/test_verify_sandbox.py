"""Unit tests for the sandbox child environment and dbt invocation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dqs.config import Settings
from dqs.verify import sandbox


def _settings(**overrides: object) -> Settings:
    data: dict[str, object] = {
        "pg_host": "db.example",
        "pg_port": 5433,
        "pg_user": "dqs",
        "pg_password": "db-password",
        "pg_database": "warehouse",
        "llm_api_key": "llm-secret-value",
        "openai_api_key": "openai-secret-value",
        "github_token": "ghp_secret_token_value",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)  # type: ignore[call-arg]


def test_child_env_is_minimal_and_has_no_api_tokens() -> None:
    source = {
        "Path": r"C:\Python\Scripts",
        "SYSTEMROOT": r"C:\Windows",
        "TEMP": r"C:\Temp",
        "LLM_API_KEY": "llm-secret-value",
        "OPENAI_API_KEY": "openai-secret-value",
        "ANTHROPIC_API_KEY": "anthropic-secret-value",
        "GITHUB_TOKEN": "ghp_secret_token_value",
        "PGDATABASE": "some-other-db",
        "RANDOM_UNRELATED": "x",
    }
    env = sandbox.sandbox_env(_settings(), "dqs_sandbox_abcd1234", source)

    assert env["PATH"] == r"C:\Python\Scripts"
    assert env["PGDATABASE"] == "warehouse"
    assert env["PGPASSWORD"] == "db-password"
    assert env["DQS_SANDBOX_SCHEMA"] == "dqs_sandbox_abcd1234"
    rendered = json.dumps(env)
    for secret in (
        "llm-secret-value",
        "openai-secret-value",
        "anthropic-secret-value",
        "ghp_secret_token_value",
    ):
        assert secret not in rendered
    assert "RANDOM_UNRELATED" not in env
    assert "PGDATABASE" in env


def test_dbt_command_isolates_target_and_log_paths(tmp_path: Path) -> None:
    cmd = sandbox.dbt_command(
        tmp_path,
        ["stg_orders", "stg_payments"],
        tmp_path / "target",
        tmp_path / "logs",
    )
    assert cmd[1] == "build"
    assert cmd[cmd.index("--target") + 1] == "sandbox"
    assert cmd[cmd.index("--target-path") + 1] == str(tmp_path / "target")
    assert cmd[cmd.index("--log-path") + 1] == str(tmp_path / "logs")
    select_at = cmd.index("--select")
    assert cmd[select_at + 1 :] == ["staging.*+"]
    assert "--fail-fast" in cmd
    assert "--no-use-colors" in cmd


def test_run_dbt_build_passes_only_with_run_results_and_no_failures(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def runner(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured.update(kwargs)
        target = Path(cmd[cmd.index("--target-path") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "run_results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {"unique_id": "model.x", "status": "success"},
                        {"unique_id": "test.x", "status": "pass"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    outcome = sandbox.run_dbt_build(
        tmp_path, ["stg_orders"], "dqs_sandbox_abcd1234", _settings(), timeout_s=12, runner=runner
    )
    assert outcome.passed is True
    assert outcome.exit_code == 0
    assert captured["shell"] is False
    assert captured["timeout"] == 12
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GITHUB_TOKEN" not in env


def test_run_dbt_build_reports_a_failing_run(tmp_path: Path) -> None:
    def runner(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        target = Path(cmd[cmd.index("--target-path") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "run_results.json").write_text(
            json.dumps({"results": [{"unique_id": "model.x", "status": "error"}]}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 1, stdout="Compilation Error", stderr="")

    outcome = sandbox.run_dbt_build(
        tmp_path, ["stg_orders"], "dqs_sandbox_abcd1234", _settings(), timeout_s=12, runner=runner
    )
    assert outcome.passed is False
    assert outcome.exit_code == 1
    assert "exited 1" in outcome.detail
    assert "model.x" in outcome.detail


def test_run_dbt_build_reports_a_timeout(tmp_path: Path) -> None:
    def runner(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, 1, output="partial output", stderr="still running")

    outcome = sandbox.run_dbt_build(
        tmp_path, ["stg_orders"], "dqs_sandbox_abcd1234", _settings(), timeout_s=1, runner=runner
    )
    assert outcome.timed_out is True
    assert outcome.passed is False
    assert "timed out" in outcome.detail or "timeout" in outcome.detail
    assert "partial output" in outcome.output
    assert "dbt build exceeded the 1s verification timeout" in outcome.detail


def test_excerpt_is_redacted_and_bounded() -> None:
    excerpt = sandbox.redacted_excerpt(("x" * 5000) + " token=super-secret-value", _settings())
    assert len(excerpt.encode("utf-8")) <= 4096
    assert "super-secret-value" not in excerpt


@pytest.mark.parametrize("name", ["staging", "marts", "dqs_sandbox_", "dqs_sandbox_BAD!"])
def test_refuses_to_drop_a_non_sandbox_schema(name: str) -> None:
    with pytest.raises(Exception, match="sandbox"):
        sandbox._sandbox_schemas(name)
