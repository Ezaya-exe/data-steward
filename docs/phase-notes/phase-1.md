# Phase 1 — Simulator, loader, dbt warehouse

Date: 2026-10-18 · Seed 42 throughout (DQS_SEED default).

## What was built

- `src/dqs/sim/generator.py` — deterministic per-date generation of the 5 raw
  tables. Day 1 = initial dimension load (2000 customers, 200 products); later
  days append ~35–45 customers, 0 products; orders 400–600/day, items 1–4/order,
  payments 1/order (~5% split 60/40). Seeding: one RNG per table as
  `[seed, date.toordinal(), k]`; children derived from the generated orders
  frame; customer ids contiguous across days, other ids in per-date slots.
- `src/dqs/sim/loader.py` — Fivetran-style schema evolution into `raw.*`:
  CREATE on first sight (all TEXT + `_batch_id`/`_loaded_at`), ALTER ADD COLUMN
  for unknown columns, missing columns simply not inserted (older rows NULL),
  whole batch + `dq.batches` + `dq.batch_tables` in ONE transaction, duplicate
  batch id rejected, identifier regex validation, observed header recorded
  without loader-owned system columns.
- `src/dqs/dbt_run.py` — single place that runs dbt: `build --target <t>
  --project-dir --profiles-dir`, records every run (success or fail) to
  `dq.pipeline_runs` with started/finished and ~4 KB log excerpt.
- `src/dqs/sim/pipeline.py` — seed/day orchestration (generate → load → dbt
  build), pure-ish and testable; CLI stays presentation-only.
- dbt project `warehouse/dbt_project/`: `dbt_project.yml`, `profiles.yml`
  (dev + sandbox targets), `macros/schema_name.sql` (dev: custom schema as-is;
  sandbox: `<target.schema>_custom` namespacing), `models/sources.yml` (5 raw
  sources, `loaded_at_field: _loaded_at`), 5 staging models with plain casts
  (`::integer`, `::numeric(12,2)`, `::timestamptz` — no defensive logic so
  faults genuinely break), 3 marts (`fct_orders`, `fct_daily_revenue`,
  `dim_customers`), `models/staging/schema.yml` (26 tests: unique/not_null,
  relationships, accepted_values), `models/exposures.yml` (3 dashboards with
  owner + maturity).
- CLI: `dqs sim seed [--days N] [--seed S]`, `dqs sim day --date YYYY-MM-DD
  [--/--no-dbt]`, `dqs dbt build [--target dev|sandbox]`.
- Tests: `tests/unit/test_generator.py` (10), `tests/integration/test_loader.py`
  (6) probing a dedicated `raw.loader_probe` table with cleanup.
- `.gitignore`: dbt `logs/` and `.user.yml` artifacts excluded.

## How to run it

```
.venv\Scripts\dqs db reset
.venv\Scripts\dqs sim seed --days 30
.venv\Scripts\dqs dbt build
.venv\Scripts\dqs sim day --date 2026-02-01   # next clean day (no fault args yet — Phase 2)
.venv\Scripts\python -m pytest -m "not integration"
.venv\Scripts\python -m pytest -m integration  # needs `dqs dev up`
```

## Acceptance results

| Criterion | Result | Evidence |
|---|---|---|
| `db reset && sim seed --days 30` completes, `dq.batches` = 30 | **PASS** | `dq.batches rows: 30` (all `loaded`); 30/30 dbt builds `dbt_exit=0`; `ok baseline seeded` |
| raw row counts in §4.2 ranges | **PASS** | customers 3161 (2000 + 29×~40), products 200, orders 13891 (~463/day avg, within 400–600/day), order_items 34845 (2.5×orders), payments 14590 (≈1.05×orders) |
| Determinism: same seed+date ⇒ byte-identical | **PASS** | `test_same_seed_and_date_reproduces_batch_byte_identically` (byte-identical CSVs for every table); `test_different_seed_changes_the_batch` for the negative |
| `dqs dbt build` passes on clean data | **PASS** | `Done. PASS=34 WARN=0 ERROR=0 SKIP=0 NO-OP=3 REUSED=0 TOTAL=37` (8 models + 26 tests) |
| `target/manifest.json`: 3 exposures, 5 sources, 8 models | **PASS** | sources 5, models 8 (+ 26 tests), exposures `[revenue_dashboard, customer_360_dashboard, ops_fulfilment_dashboard]` |
| Loader schema evolution | **PASS** | integration: extra column appears and old rows read NULL (`('1','x',None)…`); missing column → header `['a']` while the table keeps b/c; duplicate batch → `DatabaseError` "already loaded"; invalid column name rejected pre-transaction (0 rows written) |

Gate extras: example `dq.batch_tables` rows
`(batch_20260101, customers, 2000, [customer_id, email, state, signup_date])`,
`(batch_20260101, orders, 472, [order_id, customer_id, order_ts, status,
total_amount])`, `(batch_20260101, products, 200, [product_id, category,
price])`. `dqs sim day --date 2026-01-31` loaded day 31 and recorded
`('batch_20260131', 'success', 'dbt build --target dev')` to `dq.pipeline_runs`.

## Design choices and why

See `docs/DECISIONS.md` 15–26. Headlines: structural (not probabilistic) FK
integrity via contiguous customer ids [the slot-based first version produced
1901 orphans that the dbt `relationships` test caught]; children derived from
the orders frame so payment sums equal order totals exactly; duplicate batches
rejected instead of silently appended; sources/exposures under `models/`
because dbt 1.12 doesn't parse root-level yml; generic tests use dbt ≥1.11
`arguments:` nesting.

## Concepts used

- **Deterministic seed derivation** — `np.random.default_rng` accepts a
  `[seed, date, k]` sequence; numpy folds it into one stream. Combined with
  per-table `k` salts, one date's batch is a pure function of (seed, date),
  which is what makes fault replay and the benchmark meaningful.
- **Structural vs statistical integrity** — instead of "FK *likely* holds", id
  allocation guarantees it: contiguous customer ids across days make
  `customer_id ∈ 1..cumulative(date)` true by construction. Sibling tables are
  built *from* the orders frame, so sums and joins cannot drift. Tests then
  verify structure stays true, rather than tolerating exceptions.
- **Schema evolution on write** — the loader treats the batch header (not the
  table definition) as schema truth: unknown header columns are added via
  ALTER, absent header columns are simply not written for those rows. Old rows
  naturally read NULL — the exact behaviour the contracts of Phase 2 reason
  about.
- **Atomic batch landing** — the whole batch (all tables + metadata rows) is
  one transaction, so a crash mid-load leaves no half-materialized batch and
  `dq.batches` never records something the warehouse didn't fully ingest.
- **Runs-as-data** — dbt outcomes are recorded into `dq.pipeline_runs` with log
  excerpts, mirroring what an on-call engineer reads first; deterministic code
  (not an agent) captures this, the agent merely consumes it later.
- **Namespaced sandbox targets** — the schema-name macro keeps `dev` stable and
  prefixes sandbox schemas, which is what will let Phase 5 verify fix branches
  without mutating published tables.

## Deviations from the docs

- `dbt_run.py` and `sim/pipeline.py` added to the layout (§17 has no dbt/seed
  module) — DECISIONS 15.
- `sources.yml`/`exposures.yml` under `models/` (dbt 1.12 parsing) — DECISIONS 17.
- `profiles.yml` dev target uses `schema: dev` (ignored by the custom macro in
  dev for named schemas) — keeps dbt defaults sane elsewhere.
- `sim day --date` used so runs record per-day pipeline runs to
  `dq.pipeline_runs`; no `--fault` argument yet (arrives with `sim/faults.py`,
  Phase 2).

## Known limitations / TODO

- Baseline seeding always rebuilds all models per day (30 × full build); a
  `--no-dbt` shortcut for `sim seed` could speed benchmark resets (Phase 8 may
  snapshot-restore instead).
- `sim day` is documented as requiring a seeded baseline; loading day > 1 on a
  fresh warehouse fails model tests by design (no defensive workaround).
- User-visible floats are `str(float)` formatted; INR formatting is fine for
  the demo but prices are hash-derived, not from a realistic catalogue.
- Fresh `db reset` rebuilds `target/` only on first build (manifest persists
  between builds); stale manifests are accepted by dbt's partial parse.
