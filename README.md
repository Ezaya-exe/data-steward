# dqs — Agentic Data Quality Steward

Batch pipelines on Postgres + dbt, watched by **data contracts** that are
auto-generated from a clean baseline and enforced by deterministic checks.
When a load violates a contract, the system detects it, computes downstream
impact from real dbt lineage, proposes a fix (a dbt model SQL patch) or
escalates to a GitHub issue, verifies the fix in a sandbox, and — only after
human approval — opens a pull request.

Design principle: **deterministic code does everything that can be
deterministic; the LLM only reasons** (root-cause diagnosis, fix drafting,
human-readable summaries).

**Work in progress.** The build order and acceptance criteria live in
`docs/PHASES.md`, the design in `docs/ARCHITECTURE.md`, and per-phase reports
in `docs/phase-notes/`.

## Quickstart (Phase 0 scaffold)

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
copy .env.example .env
docker compose up -d --wait postgres   # or: .venv/Scripts/dqs dev up
.venv/Scripts/dqs db init              # idempotent: roles, schemas, dq tables
.venv/Scripts/dqs db ping              # server version via rw and ro roles
```

Windows note: `make` is optional. All wrap-targets exist as `dqs dev ...`
commands (`up|down|lint|test|test-int|check`) so the project does not depend
on make being installed.
