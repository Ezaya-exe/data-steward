# HANDOFF — read this first in a fresh session

Last updated: 2026-10-18 · Do not treat this file as a spec; AGENTS.md is rules,
docs/PHASES.md is order, docs/ARCHITECTURE.md is design.

## Current state

- **Phase 2a is mid-flight, NOT complete.** Review packet:
  `docs/review/phase-2a-review.md` (honest PASS/FAIL per criterion).
- Done + committed: **Phase 2 task 1 code** — `sim/faults.py` (parser +
  F1–F9 injectors + ground truth), `models/enums.py`/`contract.py`/
  `faults.py` (strict Pydantic), loader `arrived_at` override,
  `pipeline.load_day(specs=…)`, CLI `sim day --fault` (repeatable). Commit
  `c67b2e2`. `src/` is ruff+mypy clean.
- **Known code bug (top of the next session):** `column_added` (F10) is
  parsed and validated but has NO registered injector —
  `apply_faults` raises `KeyError`. Grep `@register` in `sim/faults.py`.
- **Uncommitted draft:** `tests/unit/test_faults.py` — never executed in-repo;
  it has (a) two import errors (`from dqs import generator|faults` → must be
  `dqs.sim.…`), (b) a wrong F5 positional-ordering assertion, (c) a
  trailing-colon negative that current parser behavior deliberately treats as
  "no params" (decide: reject or accept — log in DECISIONS), (d) 6 E501 lines.
  Evaluated via a corrected temp copy: 28 passed / 6 failed.
- **Not started (Phase 2 task 2 + check engine wiring out of scope for 2a):**
  `contracts/profiler.py|generator.py|store.py` are placeholder stubs; 13
  contract YAMLs, `dqs contracts generate|show|approve`, the skipped-day
  purity test, contract-determinism test, unknown-key test and the held-out
  fit test (clean days 2026-01-31…2026-02-04) are all missing.
- **Warehouse state right now:** Docker Postgres up, exactly
  `dqs db reset && dqs sim seed --days 30` — 30 batches
  (`batch_20260101`…`batch_20260130`), all dbt builds `success` in
  `dq.pipeline_runs`, no 2026-01-31+ batches. That is also the contract
  baseline Phase 2 needs.
- Repo is at `D:\study\python\data steward` (git `main`, remote: none).
- `.env` exists locally (copy of `.env.example`), is **gitignored, never commit it**.

## Running the project (PowerShell, Windows)

```powershell
cd 'D:\study\python\data steward'

# one-time (venv is Python 3.12 — NOT the shell-default 3.13):
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env

# every session:
docker compose up -d --wait postgres        # or: .venv\Scripts\dqs dev up
.venv\Scripts\dqs db ping                    # rw + ro must both print PostgreSQL 16.x
.venv\Scripts\dqs db init                    # idempotent (roles/schemas/dq tables)

# quality gates (equivalents: make lint / test / test-int / check)
.venv\Scripts\dqs dev lint                   # ruff check + ruff format --check + mypy src
.venv\Scripts\dqs dev test                   # unit only
.venv\Scripts\dqs dev test-int               # needs Postgres up
.venv\Scripts\dqs dev check                  # all three

# Phase 1 flow
.venv\Scripts\dqs db reset                   # wipes raw/staging/marts/dq!
.venv\Scripts\dqs sim seed --days 30         # ~3–4 min: 30 × (generate+load+dbt build)
.venv\Scripts\dqs dbt build --target dev     # standalone build
.venv\Scripts\dqs sim day --date 2026-01-31  # --date (not positional); --/--no-dbt flag
```

Entry point: the `dqs` console script (`.venv\Scripts\dqs.exe`); it exists because
`pip install -e .` ran. If commands are missing, re-run the install line.

## Gotchas that cost real time in Phases 0–1

1. **dbt property files must live under `models/`** — `sources.yml`/`exposures.yml`
   at the project root silently don't parse in dbt 1.12 ("source named
   'raw.customers' which was not found"). They live in `models/` now.
2. **Generic test args need `arguments:` nesting** (dbt ≥1.11):
   `accepted_values: {arguments: {values: [...]}}`, `relationships: {arguments:
   {to: ref('x'), field: id}}`. Top-level args only warn today, break later.
3. **Customer IDs must be contiguous across days** (`cumulative(day-1)+1 …
   cumulative(day)`), NOT slot-per-date: the first slot-based version left id
   gaps and the dbt `relationships` test failed with 1901 orphans on *clean*
   data. Other tables keep per-date slots (safe for them).
4. **pytest `-m` takes ONE argument**: pass `-m "not integration"` as a single
   string (splitting makes pytest treat `integration` as a path).
5. **mypy needs the pydantic plugin** (`plugins = ["pydantic.mypy"]` in
   pyproject) or `Settings()` reports bogus missing-argument errors.
6. **Docker Desktop must actually be running** (the CLI error for a stopped
   daemon is cryptic: `%2F%2F.pipe%2FdockerDesktopLinuxEngine ... file not
   found`). Start Docker Desktop first, then `dqs dev up`. WSL/hosts: compose
   binds port 5433 (host) → 5432; a local real Postgres on 5432 is fine.
7. **`make` is not installed** on this host — use `dqs dev …` (identical
   behavior); do not add logic that only works via make.
8. **dbt executable resolution**: `dbt_run.dbt_executable()` looks next to the
   running interpreter — never assume PATH.

## File map — the 10 files that matter most

| File | What it does |
|---|---|
| `AGENTS.md` | Rules: protocol, guardrails (§7), testing rules (§8), phase-note template (§9) |
| `docs/PHASES.md` | Order of work + per-phase acceptance criteria (the checklist to satisfy) |
| `docs/ARCHITECTURE.md` | Design doc: §4 data model, §5 dbt, §6 faults, §7 contracts/checks, §10 agents, §11 verification, §14 benchmark |
| `docs/DECISIONS.md` | Open decisions I logged (26 entries); read the phase sections you extend |
| `src/dqs/sim/generator.py` | Deterministic 5-table generator; seeding + ID allocation invariants |
| `src/dqs/sim/loader.py` | Schema-evolving raw loader; atomic per-batch transaction; dq metadata |
| `src/dqs/dbt_run.py` | The ONE dbt entry point; records `dq.pipeline_runs` with 4 KB logs |
| `src/dqs/sim/pipeline.py` | seed/day orchestration: generate → load → dbt build |
| `src/dqs/config.py` | `Settings` (pydantic-settings; env aliases `PGHOST`-style) |
| `warehouse/dbt_project/models/staging/schema.yml` | Staging tests + relationships (clean-day detection baseline) |
| `src/dqs/cli.py` | Typer CLI: `db`, `dev`, `sim`, `dbt` groups |
| `src/dqs/db.py` | rw/ro engines, init/reset/ping |

(There are also: `src/dqs/errors.py`, `src/dqs/dev.py`, `tests/conftest.py`,
`tests/unit/test_*.py`, `tests/integration/test_*.py`, dbt `models/*.sql`,
`docs/phase-notes/phase-{0,1}.md`.)

## DECISIONS.md entries Phase 2 must build on

- **5**: `sql/001_init.sql` creates schemas + `dq.*` only (no raw DDL) — the
  loader owns raw table creation.
- **18–19**: customer ids contiguous; orders/items/payments use per-date slots
  (1000/5000/2000). Any contract/uniqueness/FK check must respect this.
- **20**: children derive from the generated orders frame — payment sums equal
  order totals exactly; a fault touching one table will desync its children by
  design (that's what the checks must detect).
- **21**: products ship 0 rows after day 1 but the header stays in every batch
  (this is what makes F9 `missing_table` unambiguous).
- **22**: duplicate `batch_YYYYMMDD` in `dq.batches` → load error; re-seeds must
  go through `dqs db reset`.
- **23**: `dq.batch_tables.columns` records the observed batch header WITHOUT
  `_batch_id`/`_loaded_at` — contract schema checks compare against this list.
- **16–17**: dbt quirks (arguments nesting, yml under models/) — keep in mind
  when staging contract generation touches manifest/schema.yml.

## Phase 2 watch-outs (from the docs + hard-won invariants)

- **Generator stays a pure function of `(seed, date)`** — it must never read the
  DB or count loaded rows, so *skipped days* (e.g. the missing-batch scenario,
  skip 2026-02-01) still work and history can be replayed deterministically.
- **Fault injection happens AFTER generation, BEFORE loading** — faults are pure
  functions over the generated frames (arrival-time faults like F8 are the
  exception; they adjust metadata). ✅ resolved in 2a: `loader.load_batch` takes
  an optional `arrived_at` override and `pipeline.load_day(specs=…)` applies the
  faults; F8 returns that override (`dq.batches.arrived_at` + `_loaded_at` move,
  frames don't). Still untested e2e.
- **Zero false positives is an acceptance criterion** — clean days
  2026-01-31…02-04 must produce 0 incidents. That's why clean-day generation
  (not the checks!) carries the burden: ranges/means/volume bounds come from the
  30-day baseline with headroom (§7.2 rules).
- **Contract generation must be byte-identical across runs** (sort keys, stable
  float formatting) — this is enforced by `.gitattributes` LF normalization (see
  DECISIONS 3) and tested in Phase 2 acceptance.
- **`volume` for dimension tables excludes the initial-load batch** (customers
  day-1=2000 must not enter bounds); for products there are no non-initial
  batches — handle explicitly (don't crash on an empty histogram).
- **Detection-matrix discipline**: for each of F1–F10 the expected check fires
  on the expected table/column and NO unrelated breaking violation fires
  (warning-level F10 aside). Don't hand-code fault names anywhere (AGENTS §7.9).
- **Fingerprint** = `sha1(table | check_type | column)`; consecutive same-fault
  days must update ONE open incident, not two.
- New CLI surface per §15 to add: `dqs contracts generate|show|approve`,
  `dqs check batch`, `dqs monitor run`, `dqs incidents list|show`; `sim day`
  gains `--fault SPEC ...` (currently absent — Phase 2 adds it; loading without
  it is already clean).
- Add unknown-key rejection tests when `Contract` (Pydantic) lands (strictness
  is in ARCHITECTURE §7.1).

## Known limitations / half-done

- dbt project has no `sandbox` target exercised yet (target exists in
  `profiles.yml`; Phase 5 verifies sandbox behavior).
- `dqs sim day` requires the baseline to be seeded first (documented; no
  defensive workaround — intentional).
- `sim seed` always rebuilds all dbt models per day (30 × full build ≈ 3–4 min);
  benchmark resets may prefer a snapshot/restore strategy in Phase 8.
- `dqs sim day --fault` now exists (2a: injectors + CLI) but the e2e fault-load
  integration tests and ALL of contracts (task 2 + `dqs contracts *`) are still missing.
- `dq.publications` has no primary key by spec (§4.3) — idempotency is enforced
  in application code (Phase 6).
- Windows console shows mojibake for some rich Unicode glyphs in dbt output —
  cosmetic only.
- No git remote; push instructions live with the owner.
