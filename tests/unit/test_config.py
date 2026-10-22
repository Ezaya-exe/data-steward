"""Unit tests: settings loading (Phase 0 acceptance: "settings loading test")."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dqs.config import Settings

# every env var Settings may read — cleared so defaults are observable even on
# a shell where the user set something globally
ALL_ENV_VARS = [
    # Postgres aliases
    "PGHOST",
    "PG_HOST",
    "PGPORT",
    "PG_PORT",
    "PGDATABASE",
    "PG_DATABASE",
    "PGUSER",
    "PG_USER",
    "PGPASSWORD",
    "PG_PASSWORD",
    "PG_RO_USER",
    "PG_RO_PASSWORD",
    # LLM / GitHub / budgets / observability
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_MODEL_BENCH",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_MODE",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
    "GITHUB_BASE_BRANCH",
    "MAX_TOOL_CALLS",
    "MAX_FIX_ATTEMPTS",
    "MAX_TOKENS_PER_INCIDENT",
    "INCIDENT_TIMEOUT_SECONDS",
    "DQS_SEED",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_HOST",
    "DQS_SANDBOX_SCHEMA",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_defaults_from_architecture(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    assert s.pg_host == "localhost"
    assert s.pg_port == 5433
    assert s.pg_database == "warehouse"
    assert s.pg_user == "dqs"
    assert s.pg_ro_user == "dqs_ro"
    assert s.llm_provider == "anthropic"
    assert s.github_mode == "dry_run"
    assert s.max_tool_calls == 10
    assert s.max_fix_attempts == 3
    assert s.max_tokens_per_incident == 60000
    assert s.incident_timeout_seconds == 900
    assert s.dqs_seed == 42
    # secrets are empty, never fabricated
    assert s.anthropic_api_key == ""
    assert s.github_token == ""


def test_environment_overrides_defaults(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("PGPORT", "5434")
    clean_env.setenv("GITHUB_MODE", "live")
    clean_env.setenv("DQS_SEED", "7")
    s = Settings(_env_file=None)
    assert s.pg_port == 5434
    assert s.github_mode == "live"
    assert s.dqs_seed == 7


def test_loads_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PGPORT=5434\nLLM_PROVIDER=fake\n", encoding="utf-8")
    s = Settings(_env_file=str(env_file))
    assert s.pg_port == 5434
    assert s.llm_provider == "fake"


def test_non_integer_is_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("MAX_FIX_ATTEMPTS", "three")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_provider_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LLM_PROVIDER", "excel")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_github_mode_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("GITHUB_MODE", "yolo")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_urls_use_psycopg_and_both_roles(clean_env: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)
    rw = s.rw_database_url
    ro = s.ro_database_url
    assert rw.drivername == "postgresql+psycopg"
    assert rw.username == "dqs" and rw.port == 5433 and rw.database == "warehouse"
    assert ro.username == "dqs_ro" and ro.password == "dqs_ro"
