# HANDOFF — read this first in a fresh session

Last updated: 2026-10-20 (Phase 4a complete) · Do not treat this file as a
spec; AGENTS.md is rules, docs/PHASES.md is order, docs/ARCHITECTURE.md is
design.

## Current state

- **Phase 4a (LLM wrapper + read-only tools) is complete and ready for
  review.** Phase report: `docs/phase-notes/phase-4a.md`; review packet:
  `docs/review/phase-4a-review.md` (acceptance table, weakest points,
  Phase-4b risks). **Do not start Phase 4b** (investigator loop,
  `RootCause`, `dqs incident investigate`) until the owner says so.
- **Phase 4a deliverables**: `agents/llm.py` (only `init_chat_model` call;
  providers `anthropic|openai|google_genai|ollama|openai_compatible|fake`;
  per-call latency/tokens; budget tracker; redaction), `agents/smoke.py`
  (4 probes), `dqs llm smoke`, `tools/` (`base`, `sql_guard`,
  `readonly_sql`, `profiling`, `renames`, `dbt_tools`, `contracts_tool`,
  `lineage_tool`, `registry`), `dbt_run.latest_run`, DECISIONS 62–68.
  Tests: `test_llm.py`, `test_sql_guard.py`, `test_tools.py`,
  `test_sql_guard_db.py`.
- Tests: **288 unit + 58 integration** passing; ruff + format + mypy clean.
- Phase 3 (lineage/impact) remains complete: `lineage/{manifest,graph,
  columns,impact}.py`, `dqs impact`, parsed dbt failures in
  `dq.pipeline_runs`; report `docs/phase-notes/phase-3.md`, packet
  `docs/review/phase-3-review.md`, DECISIONS 55–61.
- Faults today: `sim/faults.py` with ONE injection registry covering all ten
  kinds. Contracts: all 13 in `contracts/` are `status: approved`.
- Warehouse state right now: exactly the clean contract baseline — 30 batches
  (`batch_20260101`…`batch_20260130`) in `dq.batches`, max business_date
  2026-01-30, no 2026-01-31+ batches, 0 incidents. Integration tests clean up
  their throwaway databases; no `dqs_*` clone persists as of this update.
- Phase-3 notes (DECISIONS 55–61): failing tests are stored per-run (never
  read from live files); impact scope = column (F1/F2/F10) / table (volume,
  F9) / warehouse (freshness/missing batch); manifest staleness includes
  missing `compiled_code` (a failed `dbt build` leaves a torn manifest —
  `dbt compile` regenerates); score formula documented; `models_referencing_
  column` is precision-first (direct-from-source only).

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
.venv\Scripts\dqs db init                    # idempotent (roles/schemas/dq tables + 002)

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

# Phase 3 flow (needs a faulted day first, then:)
.venv\Scripts\dqs impact <incident_uuid>     # deterministic blast radius from the manifest DAG
.venv\Scripts\python scripts\generate_manifest_fixture.py   # refresh the unit-test fixture

# Phase 4a flow (no key needed for the wiring probe)
$env:LLM_PROVIDER='fake'; .venv\Scripts\dqs llm smoke
# real endpoint: set LLM_PROVIDER=openai_compatible, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY
# then run the same `dqs llm smoke` and record provider/model/date in the report.
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


12. **A failed `dbt build` leaves a torn manifest** — children of broken
    nodes are skipped, so they carry no `compiled_code` (dbt 1.12, observed).
    The lineage loader treats that as *stale* and regenerates with
    `dbt compile` (needs Postgres up).
13. **dbt 1.12 writes `name: null` in `run_results.json`** — for readable
    node names use `unique_id.split(".")[2]`, never the hash tail.
14. **Windows console mojibake is a display artifact**, not file corruption:
    rich tables/Unicode print fine through pytest capture; `Get-Content` and
    `python -c` output look mangled in cp1252 consoles.

## File map — the 10 files that matter most

| File | What it does |
|---|---|
| `AGENTS.md` | Rules: protocol, guardrails (§7), testing rules (§8), phase-note template (§9) |
| `docs/PHASES.md` | Order of work + per-phase acceptance criteria (the checklist to satisfy) |
| `docs/ARCHITECTURE.md` | Design doc: §4 data model, §5 dbt, §6 faults, §7 contracts/checks, §10 agents, §11 verification, §14 benchmark |
| `docs/DECISIONS.md` | Open decisions I logged (68 entries); read the phase sections you extend |
| `src/dqs/sim/generator.py` | Deterministic 5-table generator; seeding + ID allocation invariants |
| `src/dqs/sim/loader.py` | Schema-evolving raw loader; atomic per-batch transaction; dq metadata |
| `src/dqs/dbt_run.py` | The ONE dbt entry point; records `dq.pipeline_runs` with 4 KB logs |
| `src/dqs/sim/pipeline.py` | seed/day orchestration: generate → load → dbt build |
| `src/dqs/config.py` | `Settings` (pydantic-settings; env aliases `PGHOST`-style) |
| `warehouse/dbt_project/models/staging/schema.yml` | Staging tests + relationships (clean-day detection baseline) |
| `src/dqs/cli.py` | Typer CLI: `db`, `dev`, `sim`, `dbt`, `contracts`, `check`, `monitor`, `incidents`, `impact`, `llm` groups |
| `src/dqs/db.py` | rw/ro engines, init/reset/ping |
| `src/dqs/checks/engine.py` | `CheckEngine.run(batch_id)` (raw batch scope) / `run_table` (staged, Phase 5); `from_directory` loads approved contracts |
| `src/dqs/incidents.py` | Violation → incident: `fingerprint`, `build_drafts`, `persist_incidents`, `_merge`, `list_incidents`, `get_incident` |
| `src/dqs/monitor.py` | The §8 loop: `run_monitor`, `default_as_of`, `unchecked_batches`, `parse_as_of` |

| `src/dqs/lineage/graph.py` | dbt manifest → networkx DAG; upstream/downstream/nodes_for_raw_table (Phase 3) |
| `src/dqs/lineage/impact.py` | deterministic ImpactReport builder + `render_summary` Phase-4 hook (Phase 3) |
| `src/dqs/agents/llm.py` | the ONLY provider construction; per-call tokens/latency; budgets; fake model (Phase 4a) |
| `src/dqs/tools/sql_guard.py` | deny-by-default SQL guard + read-only executor (Phase 4a) |
| `src/dqs/tools/registry.py` | the nine typed tools the investigator will see (Phase 4a) |

(There are also: `src/dqs/errors.py`, `src/dqs/dev.py`, `src/dqs/checks/{schema,types,nulls,unique,fk,ranges,volume,freshness}.py`,
`src/dqs/contracts/{store,generator,profiler,fitting,parsing,generation}.py`,
`src/dqs/models/{contract,violation,incident,enums}.py`, `tests/conftest.py`,
`tests/unit/test_*.py`, `tests/integration/test_*.py`, dbt `models/*.sql`,
`contracts/*.yml`, `docs/phase-notes/phase-{0,1,2a,2b,3,4a}.md`,
`docs/review/phase-{2b,3,4a}-review.md`.)

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
- `dqs sim day --fault`, contracts, check engine / monitor / incidents,
  `dqs impact`, and the Phase-4a LLM/tool layer all work and are tested
  (Phases 2a+b/3/4a). **Phase 4b** (investigator loop, `RootCause`,
  `dqs incident investigate`) is next but NOT started.
- Phase 4a has **not been run against a real model** in this environment: no
  Anthropic/OpenAI/LLM key is present, so only `LLM_PROVIDER=fake` smoke was
  executed. Do not quote it as model evidence (AGENTS §7.9).
- The tools are not wired to an agent yet; `ToolContext` must be built with
  `make_ro_engine` in Phase 4b (the `READ ONLY` transaction still blocks
  writes, but the role is not enforced by the constructors).
- SQL guard is parser + curated tests, not fuzzed; it may reject harmless
  `pg_*`/`lo_*`/`dblink*` names by design. See phase-4a review for the
  adversarial-testing gap.
- `dqs monitor run` enforces raw contracts only; staged/marts contracts are
  reachable via `CheckEngine.run_table` but not yet run by the monitor (Phase 5).
- `test_monitor_e2e.py` seeds the 30-day baseline into a template DB once
  (~2.5 min); `test_lineage_e2e.py` seeds its own module DB (~1 min) —
  acceptable now, revisit if scenarios multiply.
- Column-level lineage (`models_referencing_column`) is precision-first:
  only models selecting directly FROM the raw source count; no join scoping
  (DECISIONS 58). Chained models and dbt-visible `SELECT *` are missed.
- Pre-002 `dq.pipeline_runs` rows have `failed_nodes = NULL` — their
  incidents honestly report "no parsed dbt failure record".
- `dqs impact` may trigger a `dbt compile` manifest regeneration (torn
  manifest after a failed build); Postgres must be up.
- `dq.publications` has no primary key by spec (§4.3) — idempotency is
  enforced in application code (Phase 6).
- Windows console shows mojibake for some rich Unicode glyphs — cosmetic only.
- No git remote; push instructions live with the owner.
