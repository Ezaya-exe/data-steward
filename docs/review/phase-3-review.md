# Phase 3 review packet — lineage and impact analysis

Owner review aid, 2026-10-19. Full detail: `docs/phase-notes/phase-3.md`.
All acceptance evidence is from real runs (214 unit + 52 integration passing,
lint/mypy clean, dev warehouse byte-identical to the pre-phase state).

## Acceptance table

| Criterion | Verdict | Evidence (command → result) |
|---|---|---|
| Unit tests on fixture: downstream of `raw.orders` includes `stg_orders`, `fct_orders`, `fct_daily_revenue`, 3 exposures | PASS | `pytest tests/unit/test_lineage.py` → 14 passed; the exact set matches the real DAG (incl. `dim_customers` via `fct_orders` + 8 dbt tests) |
| Upstream queries work | PASS | `test_upstream_of_fct_orders` (3 stg models + 3 sources), `test_upstream_of_exposure_hits_the_raw_source` |
| Unknown node → clear error | PASS | `test_unknown_node_downstream_is_a_clear_error` → `LineageError: unknown lineage node …; known ids: …`; `test_nodes_for_raw_table_unknown_table_is_a_clear_error` |
| Integration on real manifest: `refs("raw.orders","customer_id")` = includes `stg_orders`, not `stg_products` | PASS | `test_lineage_manifest_e2e.py::test_column_reference_on_real_manifest` (auto-regenerates the real manifest via dbt compile when torn/missing) |
| Faulted F1 day → `dq.pipeline_runs` holds parsed failing nodes | PASS | `test_lineage_e2e.py` on a throwaway DB, real failing dbt build: `run_id 33 batch_20260206 failed parsed failed_nodes=2; …batch_20260204 failed parsed failed_nodes=1` |
| `dqs impact <incident>` prints models, failing tests (stored), exposures+owners, score | PASS | CLI samples in the phase note (F1: model `stg_orders`, test `not_null_stg_orders_customer_id` fail, 3 exposures with `Finance Analytics`/`CRM Team`/`Operations`, score 200.0) |
| Migration idempotency: `dqs db init` twice on existing DB | PASS | `test_db_init_twice_on_existing_db_is_idempotent`; `test_migration_002_applies_to_a_pre_002_database` (upgrade in place, twice); real `dqs db init; dqs db init` on dev DB exited 0 twice |
| Sample impact reports F1, F6, + volume/freshness | PASS | F1 (column scope, score 200.0), F6 (col scope w/ 2 failing tests), F9/volume (table scope, score 211.67) — captured in phase note |
| Lint, mypy, all tests green | PASS | ruff/format clean; mypy 81 files clean; 214 unit + 52 integration passed |
| Dev warehouse untouched by tests | PASS | after full suite: `dq.batches=(30, loaded, loaded, 2026-01-30)`, `dq.incidents=0`, `raw.orders=13,891` (pre-phase state per HANDOFF) |

## Sample reports

Full rendered output for F1 / F6 / F9 sits in `docs/phase-notes/phase-3.md`.

## The three weakest points

1. **Column-level lineage is narrow (precision-first).**
   `models_referencing_column` requires the model's compiled SQL to select
   *directly from the source*, so chained consumers (`fct_orders` reading
   `stg_orders`) are excluded from column-scope results; and there is no
   join/alias scoping, so a same-named column in another joined source could
   over-count. For this warehouse the results are exact, and the summary
   renders the `[column-level lineage applied]` marker, but a fancier
   warehouse would under-report column blast radius.
2. **Blast radius score is calibrated only to this DAG.** Score =
   100×Σmaturity_weight + 10×Σ1/distance (DECISIONS 60). It is deterministic
   and ranked correctly here, but the constants are heuristics; two exposures
   on `medium` maturity outweigh every model term combined. Bond-aware
   calibration would need real incident history (Phase 8 benchmark).
3. **Manifest regeneration runs `dbt compile` lazily and needs the dev
   database.** After any failed `dbt build`, the manifest is torn; `dqs
   impact` silently(ish) triggers a compile against the dev target — if
   Postgres is down, the CLI fails with the dbt excerpt (by design), but the
   staleness rule can surprise an operator by spawning a compile that takes
   ~10 s.

## Risks / hand-off notes for Phase 4 (investigator)

* `dq.pipeline_runs.failed_nodes` is the failure record the investigator
  should *lead* with (`get_pipeline_run` tool): it is structured, batch-stamped,
  and now unit-tested. Prefer it over log scraping.
* `ImpactReport` is persisted at assessment time (`dq.incidents.impact`);
  Phase 4's `assess_impact` should reuse `build_impact_report` + switch
  `render_summary`'s body to the single LLM call — the hook is documented in
  `lineage/impact.py` (DECISIONS 61). The deterministic template MUST stay the
  fallback.
* `LineageError` (new) joins the error hierarchy; agent tool wrappers for
  `get_lineage` should map it to a tool-result, not an agent-crash.
* The F9 sample shows failing nodes from *unrepaired* earlier faults
  persisting into later builds — when the investigator reads a run record,
  the dbt failures can implicate earlier-batch faults; do not attribute all
  failures to the current incident blindly.
* `LineageGraph.from_manifest()` re-reads the manifest each call. If Phase 4
  calls lineage tools repeatedly per investigation, build it once and pass
  the graph (the workflow state already carries the incident).
