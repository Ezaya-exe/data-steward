# Phase 0 — Scaffold, environment, quality gates

Date: 2026-10-18 · Owner decisions applied: `dqs dev` commands added; `make`
not executed (not installed on this host); DB checks run live once Docker
Desktop was started.

## What was built

- `.gitignore`, `.gitattributes` (LF), `.env.example` (ARCHITECTURE §3 + `DQS_SANDBOX_SCHEMA`), `README.md` stub
- `pyproject.toml` — PEP 621, `src/` layout, deps in ranges per AGENTS §4, dev extras, ruff/mypy(pytest markers `integration`, `llm`) config, `dqs` console script
- `Makefile` — thin wrappers only, delegating to `dqs dev ...` (works without make installed)
- `docker-compose.yml` — postgres:16, host port 5433, named volume, `pg_isready` healthcheck, `sql/` mounted to `/docker-entrypoint-initdb.d`
- `sql/000_roles.sql` — roles `dqs`/`dqs_ro` (idempotent DO block), plus schemas (order-of-entrypoint reason, see DECISIONS 6)
- `sql/001_init.sql` — schemas + all 8 `dq.*` tables per §4.3, idempotent; SELECT grants + default privileges for `dqs_ro`
- `src/dqs/config.py` — `Settings` (pydantic-settings, aliases for `PGHOST`-style names, provider/mode validators, rw/ro SQLAlchemy URLs)
- `src/dqs/errors.py` — `DqsError` / `ConfigError` / `DatabaseError`
- `src/dqs/db.py` — rw/ro engine factories, `init_database` / `reset_database` / `ping` (side effects isolated, no identifiers from user input)
- `src/dqs/cli.py` + `__main__.py` — `dqs db init|reset|ping`, `dqs dev up|down|lint|test|test-int|check`; errors → clear message + exit 1
- `src/dqs/dev.py` — pure command builders + one `run()` subprocess wrapper (unit-testable, make-equivalent)
- Layout stubs (docstring-only, no logic) for §17 packages: `models/ sim/ contracts/ checks/ monitor incidents lineage/ tools/ agents/ verify/ publish/ workflow/ bench/ memory/ observability/`, `ui/app.py`, `.gitkeep` dirs
- `tests/unit/test_config.py` (settings), `tests/unit/test_dev.py` (command builders), `tests/integration/test_db.py` (ping, ro-role guardrails, init idempotency), `tests/conftest.py` (skip-if-unreachable fixtures)
- `docs/DECISIONS.md` (14 entries), `requirements.lock` (140 exact pins)

## How to run it

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"   # = make install
copy .env.example .env
dqs dev up                                        # = make up (docker compose up -d --wait postgres)
dqs db init          # twice — idempotent
dqs db ping          # version for rw + ro roles
dqs dev lint         # ruff check + ruff format --check + mypy src
dqs dev test         # unit
dqs dev test-int     # integration (needs Postgres)
dqs dev check        # all three, stops at first failure
```

## Acceptance results

| Criterion | Result | Evidence |
|---|---|---|
| install succeeds | **PASS** | `pip install -e ".[dev]"` clean resolve; `pip show` versions below |
| `docker compose up` + healthy | **PASS** | `dqs dev up` → "Container dqs-postgres Healthy"; `docker compose ps` → `Up (healthy) 0.0.0.0:5433->5432/tcp` |
| `dqs db init` idempotent | **PASS** | ran twice, then `dqs db reset` + `dqs db init` — no error any time |
| `dqs db ping` version for rw+ro | **PASS** | both printed `PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2) ... on x86_64-pc-linux-gnu` |
| `dqs_ro` cannot INSERT/CREATE, can SELECT dq.* | **PASS** | `pytest -m integration`: 5 passed (incl. `test_ro_role_cannot_insert`, `test_ro_role_cannot_create_tables` → `ProgrammingError`; `test_ro_role_can_select_dq_tables`) |
| `make lint` | **PASS (via `dqs dev lint`)** | "All checks passed! / 80 files already formatted / Success: no issues found in 71 source files" |
| `make test` | **PASS (via `dqs dev test`)** | `11 passed, 5 deselected in 0.07s` (settings + dev-command builders) |
| full gate `dqs dev check` | **PASS** | lint green + `16 collected / 5 selected` → `5 passed, 11 deselected` (integration) |
| `requirements.lock` committed | **PASS** | 140 exact pins (transitive), `pip freeze --exclude-editable` |

Note: `make` itself was not executed (binary not on host); every make target
wraps the exact commands listed above (DECISIONS 8–9). `dqs dev check` (the
full gate) ran end-to-end green.

## Gate — installed versions

Python 3.12.10 · langgraph 1.2.16 · langchain 1.4.6 · dbt-core 1.12.6 ·
dbt-postgres 1.11.0 · streamlit 1.66.0 · SQLAlchemy 2.1.5 · psycopg 3.3.6 ·
pandas 2.3.3 · pydantic 2.14.1 · sqlglot 29.0.1 · typer 0.27.3 · ruff 0.17.1 ·
mypy 1.20.2 · pytest 8.4.2 · docker engine 28.3.2 / compose v2.38.2

## Design choices and why

See `docs/DECISIONS.md` 1–14 this phase. Highlights: Python 3.12 not 3.13;
`000_roles.sql` creates schemas so first-boot grants don't reference missing
objects; `ALTER DEFAULT PRIVILEGES` keeps dbt tables readable for the ro role;
`dqs dev` added so make is optional.

## Concepts used

- **Idempotent bootstrap DDL** — every statement is `IF NOT EXISTS`, a `DO`
  block, or a grant: re-running is a no-op, so the same file safely runs at
  container init *and* via `dqs db init`.
- **Least-privilege roles** — `dqs_ro` gets `USAGE` + `SELECT` over exactly the
  four schemas and no write/Create rights, so a compromised/taken-over prompt
  still cannot mutate the warehouse (guardrail that later phases lean on).
- **Default privileges** — `ALTER DEFAULT PRIVILEGES` applies to tables created
  *in future* by `dqs`, which is how dbt models stay readable without rerunning
  grants each build.
- **Settings as an object graph boundary** — one pydantic-settings class with
  aliases/validators means no `os.environ` reads elsewhere and injection-friendly
  code (engines take `Settings`, not env).
- **Engine factories + markers as test taxonomy** — rw/ro engines are built from
  config and injected by fixtures; pytest markers (`integration`, `llm`) pick
  which suites run without network/Docker/keys by default.

## Deviations from the docs

- `000_roles.sql` creates the schemas too (DECISIONS 6; reasoning above).
- `dqs dev ...` commands added to the CLI surface (owner-directed; DECISIONS 8).
- `make` targets verified via their underlying commands (DECISIONS 9).
- `.gitattributes` added (not in the §17 layout; DECISIONS 3).
- `.env.example` adds `DQS_SANDBOX_SCHEMA` (referenced by ARCHITECTURE §5).

## Known limitations / TODO

- Raw business tables don't exist yet — the schema-evolving loader lands in Phase 1.
- `dqs db ping` output is `role: version()` lines; no structured output flag (not required).
- Nothing else from §15 exists yet by design (phases 1+).
- First-boot container init and `dqs db init` share the SQL files; any edit to
  grants must stay idempotent in both paths.
