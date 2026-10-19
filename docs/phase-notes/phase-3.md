# Phase 3 — Lineage and impact analysis

Date: 2026-10-19 · Phase 3 of `docs/PHASES.md` · Status: **ready for review**.
Companion review packet: `docs/review/phase-3-review.md`.

## What was built

* `src/dqs/lineage/manifest.py` — finds/validates the dbt manifest, staleness
  rule (missing, unparseable, older than `models/**`, or any model without
  `compiled_code`), regeneration via `dbt compile` with a loud error on dbt
  failure (DECISIONS 57).
* `src/dqs/lineage/graph.py` — `LineageGraph.from_manifest` (networkx
  DiGraph over nodes/sources/exposures via `parent_map`/`child_map` with a
  `depends_on` fallback), `downstream`/`upstream` (types filter, nearest
  first, deterministic order), `distance`, `nodes_for_raw_table`, clear
  unknown-node/unknown-table errors.
* `src/dqs/lineage/columns.py` — `models_referencing_column` via sqlglot
  (postgres dialect) on compiled SQL; documented best-effort heuristic
  (DECISIONS 58).
* `src/dqs/models/impact.py` + `src/dqs/lineage/impact.py` — `ImpactReport`
  (pydantic) and the deterministic builder: scope rules (column / table /
  warehouse, DECISIONS 56), blast-radius score (DECISIONS 60), template
  summary with the clearly marked **Phase-4 hook** `render_summary`
  (DECISIONS 61).
* `src/dqs/models/pipeline.py` + `src/dqs/dbt_run.py` — `run_results.json`
  parsed at build time; failed/errored node ids+statuses stored on the same
  `dq.pipeline_runs` row; `latest_run_for_batch` reader for the impact
  builder (DECISIONS 55 — the live file is never trusted).
* `sql/002_pipeline_run_failures.sql` + `db.py` `INIT_SQL_FILES` — idempotent
  migration; `dqs db init` applies it to fresh AND existing databases.
* `src/dqs/incidents.py` — `save_impact`; the incident reader now carries
  `root_cause`/`impact`/`resolution` jsonb (a stored report survives
  `get_incident`).
* `src/dqs/cli.py` — `dqs impact <incident_id>`: loads incident + stored
  record + manifest, builds the report, prints affected models / stored
  failing tests / exposures with owners / score, persists `dq.incidents.impact`.
* `scripts/generate_manifest_fixture.py` + `tests/fixtures/manifest_fixture.json`
  — checked-in fixture generated from the REAL manifest (complete real DAG,
  DECISIONS 59).
* `src/dqs/errors.py` — `LineageError`.

## How to run it

```powershell
.venv\Scripts\dqs db init                 # idempotent; applies 002
.venv\Scripts\dqs sim day --date 2026-02-04 --fault column_rename:orders.customer_id:new_name=cust_id
.venv\Scripts\dqs monitor run
.venv\Scripts\dqs impact <incident_uuid>
.venv\Scripts\dqs dev lint ; .venv\Scripts\dqs dev test
.venv\Scripts\python scripts\generate_manifest_fixture.py   # regenerate fixture
```

## Acceptance results

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Unit tests on fixture: downstream of `raw.orders` includes `stg_orders`, `fct_orders`, `fct_daily_revenue`, 3 exposures; upstream works; unknown node → clear error | PASS | `test_lineage.py` (14 tests). Realagraph fixture: full set = `{stg_orders, fct_orders, fct_daily_revenue, dim_customers, 3 exposures}` + 8 dbt tests; `dim_customers` is genuinely downstream via `fct_orders`. `test_unknown_node_downstream_is_a_clear_error` |
| 2 | Integration on real manifest: `models_referencing_column("raw.orders","customer_id")` → `stg_orders`, not `stg_products` | PASS | `test_lineage_manifest_e2e.py::test_column_reference_on_real_manifest` (regenerates the real manifest via `dbt compile` when missing/stale) |
| 3 | After faulted F1 day, `dq.pipeline_runs` holds parsed failing nodes; `dqs impact` prints models, stored failing tests, exposures+owners, score | PASS | `test_lineage_e2e.py` (real failing `dbt build` against a throwaway DB): `failed_nodes=1` (not_null_stg_orders_customer_id, fail). CLI sample below |
| 4 | Migration idempotency: `dqs db init` twice on an existing DB | PASS | `test_db_init_twice_on_existing_db_is_idempotent` + pre-002 upgrade test (`002` applied in place, twice) + real `dqs db init; dqs db init` on the dev-warehouse DB (exit 0 both) |
| 5 | Lint, mypy, all tests green; dev warehouse untouched | PASS | ruff + format clean, mypy 81 files clean, **214 unit + 52 integration passed**; after the full suite: `dq.batches = 30 rows all 'loaded'`, `dq.incidents = 0`, `raw.orders = 13,891 rows` |
| 6 | Sample reports for F1 and F6 (+ volume) | PASS | see below and `docs/review/phase-3-review.md` |

### Sample report — F1 (before fix — the CLI records the report into `dq.incidents.impact`)

```
impact for 70c60862  source raw.orders (column customer_id)
  summary: Blast radius for raw.orders (column customer_id): affected models:
  stg_orders; exposures impacted: customer_360_dashboard (CRM Team),
  ops_fulfilment_dashboard (Operations), revenue_dashboard (Finance
  Analytics); stored dbt record shows 1 failed/errored node(s); blast radius
  score 200.0.
model      | layer   | distance | column-lineage
stg_orders | staging | 1        | yes
failing dbt node (stored record) | status | batch
not_null_stg_orders_customer_id  | fail   | batch_20260204
exposure                 | owner             | maturity
customer_360_dashboard   | CRM Team          | medium
ops_fulfilment_dashboard | Operations        | low
revenue_dashboard        | Finance Analytics | high
blast radius score: 200.0
```

### Sample report — F6 (duplicate rows)

```
impact for a9f6ec05  source raw.orders (column order_id)
  summary: ... affected models: stg_orders; stored dbt record shows 2
  failed/errored node(s); blast radius score 200.0.
failing dbt node (stored record) | status | batch
not_null_stg_orders_customer_id  | fail   | batch_20260205
unique_stg_orders_order_id       | fail   | batch_20260205
```
(the not_null test keeps failing because day 02-04's renamed `cust_id`
column leaves NULL `customer_id` in rebuilt `stg_orders` — unrepaired
incidents propagate; honest, and exactly what a steward must say)

### Sample report — F9 (missing payments table → volume.rows, table scope)

```
impact for 76d0f2db  source raw.payments
  summary: Blast radius for raw.payments: affected models: stg_payments,
  fct_orders, dim_customers (+1 more); exposures impacted: ...; stored dbt
  record shows 2 failed/errored node(s); blast radius score 211.67.
model             | layer   | distance
stg_payments      | staging | 1
fct_orders        | marts   | 2
dim_customers     | marts   | 3
fct_daily_revenue | marts   | 3
```

## Design choices and why

Logged in `docs/DECISIONS.md` (55–61). Highlights:

* **Parsed failures live with the run** (55): the live `run_results.json` was
  observed torn after a failed build; the impact report must not depend on a
  file the next build rewrites.
* **Column lineage is precise, not complete** (58): only models reading the
  source directly (sqlglot-compiled proof). `fct_orders` is *not* listed for
  `raw.orders.customer_id` because its compiled SQL selects from
  `stg_orders`, not the source — wider union logic (all downstream) would
  inflate column-level impact; that stays for table-level scope.
* **Manifest can be torn** (57): after a failed `dbt build`, children of
  broken nodes have no compiled_code; `is_stale` detects it and `dbt
  compile` rebuilds. Warnings ("no compiled SQL for X; skipped") appear only
  when compile cannot help either (never silently skipped).
* **Unit tests never need dbt** (59): the fixture mirrors the real DAG
  including compiled_code, so `models_referencing_column` is unit-tested.

## Concepts used

* **Directed acyclic graph traversal**: dbt's manifest already encodes the
  dependency graph (`parent_map`); BFS reachability up/down gives blast
  radius as a pure, cheap computation — lineage queries are the kind of thing
  LLMs get wrong and graph algorithms get right.
* **Column-level lineage as SQL text analysis**: dbt has no column lineage,
  so we parse compiled SQL with sqlglot and check for *provably* selected
  sources + column references. Best-effort by nature: parsing sees text, not
  runtime semantics — heuristics belong in the report, described and limited.
* **Event-time anchoring of observability data**: storing parsed failures on
  the run row at build time is the same principle as the load-time profile
  capture in Phase 2 — observability belongs to the event that produced it,
  not to the mutable current state ("the last build").
* **Deterministic scoring for human comparison**: the score is a fixed
  formula over graph distances and exposure criticality, so two people see
  the same number for the same incident and the ranking is explainable line
  by line — an LLM could not be trusted to produce a stable number.
* **Graceful fallback with disclosure**: when the column heuristic finds
  nothing, the builder falls back to the table footprint and *says so* in the
  summary; degradation is disclosed, never silent.

## Deviations from the docs

* None blocking. Architecture §9 suggested `models_referencing_column` as a
  graph method (kept in `columns.py`; the graph exposes no such method) and
  "LLM summary optional" — implemented as the documented Phase-4 hook instead
  of shipping an unused LLM call-phase (DECISIONS 61). Warnings went to
  stderr through `logging` (§6).

## Known limitations / TODO

* `models_referencing_column` misses chained models (fct_orders reads
  stg_orders, not the source) and dbt-`SELECT *` columns; also no join
  scoping → possible over-count in multi-join staging models (documented,
  precision-first).
* After a failed `dbt build`, the impact CLI triggers a `dbt compile`
  regeneration (manifest torn rule); `dbt compile` needs the dev database up.
* Pre-002 `dq.pipeline_runs` rows have no parsed failures; their incidents
  report "no parsed dbt failure record was stored for this batch".
* `dqs impact` does not verify the incident's batch is the same batch as the
  stored record's `batch_id` beyond the reader's filter; a fully stale run
  (no rows for that batch) yields an empty failing-test list, loudly said.
* `.jsonb` impact/root_cause/resolution now round-trip through
  `get_incident`; `incidents show` doesn't print them yet (UI, Phase 7).
