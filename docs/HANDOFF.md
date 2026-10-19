# HANDOFF — read this first in a fresh session

Last updated: 2026-10-19 · Do not treat this file as a spec; AGENTS.md is rules,
docs/PHASES.md is order, docs/ARCHITECTURE.md is design.

## Current state

- **Phase 2a and Phase 2b are complete and ready for review.** Phase reports:
  `docs/phase-notes/phase-2a.md`, `docs/phase-notes/phase-2b.md`; review packets:
  `docs/review/phase-2b-review.md` (2a packet's FAIL rows all closed). **Do not
  start Phase 3** (lineage/impact) until the owner says so.
- **Phase 2b (check engine + monitor)**: all ten §7.3 checks
  (`checks/{schema,types,nulls,unique,fk,ranges,volume,freshness}.py`) driven by
  `CheckEngine.run(batch_id)` against approved raw contracts, plus
  `src/dqs/incidents.py` (fingerprint/de-dup/merge) and `src/dqs/monitor.py`
  (the §8 loop). New CLI: `dqs check batch`, `dqs monitor run [--as-of]`,
  `dqs incidents list|show`. New contract field `Volume.initial_load_excluded`
  (DECISION 49) so a clean day-1 initial load is not a false positive.
- Faults today: `sim/faults.py` with ONE injection registry covering all ten
  kinds (`set(_INJECTORS) == set(FaultKind)` is tested), parser validates
  empty-params (`kind:table:` is an error) and orphan_keys FK pairs (`FK_MAP`,
  parse-time, F5 ids strictly above the cumulative parent max), F10 adds
  coupon-like text + nulls (DECISIONS 27–32).
- Contracts today: `contracts/parsing.py` (shared TEXT→type parsing),
  `contracts/profiler.py` (raw per-batch + staged snapshot, orphan anti-joins)
  and `contracts/generator.py` (§7.2 thresholds), `contracts/store.py`
  (deterministic YAML, strict load, approve), `contracts/generation.py`
  (one-pass `generate_all`), `contracts/fitting.py` (fit/sensitivity HARNESS
  only — thresholds are exported there for the check engine; it is NOT the
  check engine). CLI: `dqs contracts generate|show|approve`. The 13 contract
  YAMLs live in `contracts/`, **all `status: approved`** (the check engine and
  the monitor load approved contracts only; a byte-identity test guards them).
- Tests: **217 passed** = 172 unit + 45 integration. Integration files create
  throwaway DBs (`dqs_test`, or a cloned template for the monitor) and NEVER
  touch the dev warehouse: `test_faults_e2e.py` (4), `test_contracts_e2e.py`
  (16 — held-out fit on clean 2026-01-31…02-04, fault sensitivity F1–F7/F9/F10,
  FK_MAP match, catalog validation, unknown-key rejection, dev-warehouse regen
  byte-identity), `test_monitor_e2e.py` (15 — monitor acceptance: clean-day zero
  false positives, the F1–F10 detection matrix, missing batch, de-dup across
  days). Unique basenames among integration test files matter.
- Warehouse state right now: exactly the clean contract baseline — 30 batches
  (`batch_20260101`…`batch_20260130`) in `dq.batches`, max business_date
  2026-01-30, no 2026-01-31+ batches. Generating contracts against it
  reproduces the committed files byte-for-byte (tested).
- Phase-2b hooks (DECISIONS 33–54): bounds on measures only; dimension day-1
  excluded from volume and recorded as `Volume.initial_load_excluded`;
  `required_in_batch` only at 100% baseline presence; freshness batch-level via
  `dq.batches.arrived_at` (F8 is stat-invisible); staged contracts are snapshots
  (`batches: 1`). Violation fingerprint = `sha1(table|check|column)`; monitor
  marks consumed batches `checked` (idempotent, drives de-dup).
- Phase 3 (next, NOT started): lineage/impact — `lineage/manifest.py` +
  `lineage/graph.py` (dbt manifest → DAG), `lineage/columns.py` (column-level
  lineage), `ImpactReport` model, `dqs impact <incident_id>`.
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
.venv\Scripts\dqs sim day --date 2026-01-31 --fault orders:row_count_drop:ratio=0.5

# Phase 2b flow — NEVER run the monitor against the dev warehouse (it flips
# batch status to 'checked'); use a clone:  $env:PGDATABASE='dqs_accept'
.venv\Scripts\dqs contracts approve --all
.venv\Scripts\dqs check batch batch_20260131
.venv\Scripts\dqs monitor run                              # default --as-of = max date + 1d
.venv\Scripts\dqs monitor run --as-of 2026-02-03T00:00Z
.venv\Scripts\dqs incidents list
.venv\Scripts\dqs incidents show <uuid>
```

Entry point: the `dqs` console script (`.venv\Scripts\dqs.exe`); it exists because
`pip install -e .` ran. If commands are missing, re-run the install line.

## Gotchas that cost real time in Phases 0–2

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
9. **PowerShell mangles UTF-8 in captured subprocess output** (rich glyphs show
   as `�`) — a console-encoding artifact only; redirect to a file or read the
   DB to check real content. Related: a stray non-UTF-8 byte (`cp1252` em-dash)
   in a Markdown file breaks `ruff format --check .` repo-wide.
10. **Integration tests must not touch the dev warehouse.** `monitor run` flips
    `dq.batches.status` to `checked`; the monitor tests clone a seeded template
    DB (`dqs_monitor_template`) with `CREATE DATABASE … TEMPLATE` and drop it.
11. **The detection matrix needs contiguous days** — skipping a business date
    makes clean FK values look orphaned (customer ids are date-slotted), which
    would produce false positives unrelated to the injected fault.

## File map — the 10 files that matter most

| File | What it does |
|---|---|
| `AGENTS.md` | Rules: protocol, guardrails (§7), testing rules (§8), phase-note template (§9) |
| `docs/PHASES.md` | Order of work + per-phase acceptance criteria (the checklist to satisfy) |
| `docs/ARCHITECTURE.md` | Design doc: §4 data model, §5 dbt, §6 faults, §7 contracts/checks, §10 agents, §11 verification, §14 benchmark |
| `docs/DECISIONS.md` | Open decisions I logged (54 entries); read the phase sections you extend |
| `src/dqs/sim/generator.py` | Deterministic 5-table generator; seeding + ID allocation invariants |
| `src/dqs/sim/loader.py` | Schema-evolving raw loader; atomic per-batch transaction; dq metadata |
| `src/dqs/dbt_run.py` | The ONE dbt entry point; records `dq.pipeline_runs` with 4 KB logs |
| `src/dqs/sim/pipeline.py` | seed/day orchestration: generate → load → dbt build |
| `src/dqs/config.py` | `Settings` (pydantic-settings; env aliases `PGHOST`-style) |
| `warehouse/dbt_project/models/staging/schema.yml` | Staging tests + relationships (clean-day detection baseline) |
| `src/dqs/cli.py` | Typer CLI: `db`, `dev`, `sim`, `dbt`, `contracts`, `check`, `monitor`, `incidents` groups |
| `src/dqs/db.py` | rw/ro engines, init/reset/ping |
| `src/dqs/checks/engine.py` | `CheckEngine.run(batch_id)` (raw batch scope) / `run_table` (staged, Phase 5); `from_directory` loads approved contracts |
| `src/dqs/incidents.py` | Violation → incident: `fingerprint`, `build_drafts`, `persist_incidents`, `_merge`, `list_incidents`, `get_incident` |
| `src/dqs/monitor.py` | The §8 loop: `run_monitor`, `default_as_of`, `unchecked_batches`, `parse_as_of` |

(There are also: `src/dqs/errors.py`, `src/dqs/dev.py`, `src/dqs/checks/{schema,types,nulls,unique,fk,ranges,volume,freshness}.py`,
`src/dqs/contracts/{store,generator,profiler,fitting,parsing,generation}.py`,
`src/dqs/models/{contract,violation,incident,enums}.py`, `tests/conftest.py`,
`tests/unit/test_*.py`, `tests/integration/test_*.py`, dbt `models/*.sql`,
`contracts/*.yml`, `docs/phase-notes/phase-{0,1,2a,2b}.md`, `docs/review/phase-2b-review.md`.)

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

## Phase 2 invariants (now enforced — read before Phase 3+)

- **Generator stays a pure function of `(seed, date)`** — it must never read the
  DB or count loaded rows, so *skipped days* (e.g. the missing-batch scenario,
  skip 2026-02-01) still work and history can be replayed deterministically.
- **Fault injection happens AFTER generation, BEFORE loading** — faults are pure
  functions over the generated frames (arrival-time faults like F8 are the
  exception; they adjust metadata). ✅ resolved in 2a: `loader.load_batch` takes
  an optional `arrived_at` override and `pipeline.load_day(specs=…)` applies the
  faults; F8 returns that override (`dq.batches.arrived_at` + `_loaded_at` move,
  frames don't). Tested e2e (task 1).
- **Zero false positives is an acceptance criterion** — clean days
  2026-01-31…02-04 must produce 0 incidents. That's why clean-day generation
  (not the checks!) carries the burden: ranges/means/volume bounds come from the
  30-day baseline with headroom (§7.2 rules).
- **Contract generation must be byte-identical across runs** (sort keys, stable
  float formatting) — this is enforced by `.gitattributes` LF normalization (see
  DECISIONS 3) and tested in Phase 2 acceptance (now enforced by `test_regenerated_contracts_are_byte_identical_to_committed`).
- **`volume` for dimension tables excludes the initial-load batch** (customers
  day-1=2000 must not enter bounds); for products there are no non-initial
  batches — handle explicitly (don't crash on an empty histogram).
- **Detection-matrix discipline**: for each of F1–F10 the expected check fires
  on the expected table/column and NO unrelated breaking violation fires
  (warning-level F10 aside). Don't hand-code fault names anywhere (AGENTS §7.9).
- **Fingerprint** = `sha1(table | check_type | column)`; consecutive same-fault
  days must update ONE open incident, not two. ✅ `tests/unit/test_incidents.py`
  + `test_monitor_e2e.py::test_same_fault_on_two_days_keeps_one_open_incident…`.
- **CLI surface per §15 is complete through Phase 2**: `dqs contracts
  generate|show|approve`, `dqs check batch`, `dqs monitor run [--as-of]`,
  `dqs incidents list|show`, and `sim day --fault SPEC …`.
- **Unknown-key rejection is tested** (`test_contracts_e2e.py`).
- **Value checks never cascade** — a missing column is reported once
  (`schema.missing_column`); an absent `required_in_batch` table is owned by
  `volume.rows`. Keep this when adding checks in later phases.
- **`monitor run` mutates batch status** — never point it at the dev warehouse.
  Use a throwaway clone (`CREATE DATABASE … TEMPLATE`) as the integration tests do.

## Known limitations / half-done

- dbt project has no `sandbox` target exercised yet (target exists in
  `profiles.yml`; Phase 5 verifies sandbox behavior).
- `dqs sim day` requires the baseline to be seeded first (documented; no
  defensive workaround — intentional).
- `sim seed` always rebuilds all dbt models per day (30 × full build ≈ 3–4 min);
  benchmark resets may prefer a snapshot/restore strategy in Phase 8.
- `dqs sim day --fault`, contracts (`dqs contracts *`) and the check engine /
  monitor / incidents (`dqs check|monitor|incidents *`) all work and are tested
  (Phases 2a + 2b). Phase 3 (lineage/impact) is next but NOT started.
- `dqs monitor run` enforces raw contracts only; staged/marts contracts are
  reachable via `CheckEngine.run_table` but not yet run by the monitor (Phase 5,
  sandbox verification).
- `test_monitor_e2e.py` seeds the 30-day baseline into a template DB once
  (~2.5 min); acceptable now, revisit if scenarios multiply.
- `dq.publications` has no primary key by spec (§4.3) — idempotency is enforced
  in application code (Phase 6).
- Windows console shows mojibake for some rich Unicode glyphs in dbt output —
  cosmetic only.
- No git remote; push instructions live with the owner.
