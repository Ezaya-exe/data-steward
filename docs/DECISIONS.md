# DECISIONS

Log of every decision the docs leave open, with date and reason. One short
entry each; newest last.

---

## Phase 0 (2026-10-18)

1. **Repo root is this directory** (`D:\study\python\data steward`), not a new
   nested `dq-steward/` folder. Reason: `AGENTS.md` and `docs/` already live
   here; §17's `dq-steward/` prefix reads as the project name, not a required
   subdirectory.
2. **Python 3.12.10 for `.venv`** (`py -3.12 -m venv .venv`). Reason: AGENTS §4
   says "use the newest of 3.11/3.12"; the host default `python` is 3.13.5, and
   dbt/langgraph ecosystems were only documented for 3.11/3.12.
3. **`.gitattributes` pins LF** for text files. Reason: host git has
   `core.autocrlf=true`; future byte-identical-output requirements (contract
   YAML determinism, Phase 2) should not fight line-ending rewriting.
4. **`requirements.lock` = `pip freeze --exclude-editable`** of the full dev
   install (140 pins, transitive included). Reason: `uv` is not installed;
   AGENTS §4 accepts plain pip; exact versions after a working install.
5. **`sql/001_init.sql` creates schemas + `dq` tables only** — no `raw.*` DDL.
   Reason: ARCHITECTURE §4.2 says the loader creates raw tables on first sight
   (schema-evolving), so Phase 0 has nothing valid to say about their columns.
6. **`sql/000_roles.sql` also creates the four schemas** (deviation from the
   PHASES.md task wording, which puts schemas only in `001_init.sql`). Reason:
   the docker entrypoint runs files alphabetically — 000 before 001 — and the
   `GRANT`/`ALTER DEFAULT PRIVILEGES` statements reference the schemas, which
   would abort a first-boot init with "schema does not exist".
7. **`ALTER DEFAULT PRIVILEGES` grants `SELECT` to `dqs_ro` on future tables**
   in all four schemas. Reason: dbt-created `staging`/`marts` tables (Phase 1+)
   must stay readable by the read-only agent role without re-running grants.
8. **`dqs dev up|down|lint|test|test-int|check` added** (owner-directed).
   Reason: `make` is not installed on this Windows host; the project must not
   depend on it (AGENTS §5). These commands are make-equivalent wrappers and
   the Makefile keeps pointing at them. Noted as an addition to the ARCHITECTURE
   §15 CLI surface, not scope creep.
9. **Phase 0 acceptance evidence for `make install` / `make up` comes from the
   underlying commands** (`py -3.12 -m venv`, `pip install -e ".[dev]"`,
   `dqs dev up` = `docker compose up -d --wait postgres`). Reason as (8);
   Makefile can be exercised once make is installed — it only wraps these.
10. **Model names in `.env.example` (`claude-haiku-5-5`, `claude-sonnet-5-5`)
    kept exactly as documented but not validated** against installed provider
    catalogs. Reason: Phase 0 only stores strings (`Settings` validates the
    provider enum); real model IDs are verified in Phase 4 when the LLM client
    is built.
11. **`db init` runs each SQL file as one multi-statement round trip**
    (`Connection.exec_driver_sql`). Reason: psycopg3 allows multi-statement
    executes; splitting on `;` would break `DO $$ ... $$` blocks.
12. **mypy uses the pydantic plugin** (`plugins = ["pydantic.mypy"]`). Reason:
    without it, mypy treats `Settings()` as missing constructor arguments even
    though every field has a default.
13. **pytest marker expressions passed as a single argument**
    (`-m "not integration"`). Reason: pytest's `-m` takes one argument; the
    split form makes pytest treat `integration` as a test path.
14. **Resolved versions (2026-10-18, Python 3.12.10):** langgraph 1.2.16,
    langchain 1.4.6, langchain-core 1.6.12, dbt-core 1.12.6, dbt-postgres
    1.11.0, streamlit 1.66.0, SQLAlchemy 2.1.5, psycopg 3.3.6, pandas 2.3.3,
    numpy 2.5.4, pydantic 2.14.1, sqlglot 29.0.1, typer 0.27.3, ruff 0.17.1,
    mypy 1.20.2, pytest 8.4.2. Full transitive set in `requirements.lock`.
