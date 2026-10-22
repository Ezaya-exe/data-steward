"""Unit tests: the dbt child environment (Phase 6b demo safety).

`dbt_env` is the boundary that keeps API keys out of a subprocess and guarantees
the build talks to the database named in Settings. The demo relies on the
second property: if dbt inherited a stale `PGDATABASE`, the 30-day seed would
land in the real warehouse.
"""

from __future__ import annotations

from pathlib import Path

from dqs.config import Settings
from dqs.dbt_run import dbt_env


def test_dbt_env_uses_settings_for_every_pg_variable(monkeypatch) -> None:
    monkeypatch.setenv("PGHOST", "stale-host")
    monkeypatch.setenv("PGDATABASE", "warehouse")
    settings = Settings(
        _env_file=None,
        pg_host="db.example",
        pg_port=6543,
        pg_database="dqs_demo",
        pg_user="tester",
        pg_password="pw",
    )
    env = dbt_env(settings)
    assert env["PGHOST"] == "db.example"
    assert env["PGPORT"] == "6543"
    assert env["PGDATABASE"] == "dqs_demo"
    assert env["PGUSER"] == "tester"
    assert env["PGPASSWORD"] == "pw"


def test_dbt_env_is_an_allowlist_not_an_inherited_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-must-not-leak")
    monkeypatch.setenv("DQS_CANARY", "not-allowlisted")
    monkeypatch.setenv("PATH", str(tmp_path))
    settings = Settings(_env_file=None, pg_database="dqs_demo")

    env = dbt_env(settings)

    assert env["PATH"] == str(tmp_path)
    assert env["PYTHONUTF8"] == "1"
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "DQS_CANARY" not in env
    assert "sk-must-not-leak" not in env.values()
    assert "ghp-must-not-leak" not in env.values()
