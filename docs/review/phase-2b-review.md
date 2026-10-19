# Phase 2b review packet

Date: 2026-10-19 · Scope: Phase 2 tasks 3–4 (check engine, incidents, monitor)
**Status: COMPLETE — all acceptance criteria pass; ready for review.**

## 1. Acceptance checklist

| Criterion | Verdict | Command | One-line result |
|---|---|---|---|
| Zero false positives on baseline + clean 01-31…02-04 | PASS | `dqs monitor run` | `as-of 2026-02-05T00:00:00+00:00 / batches checked: 35 / ok no incidents`; `incidents list` → `0 incident(s)` |
| Detection matrix F1–F10 (expected table/column) | PASS | `pytest tests/integration/test_monitor_e2e.py` | parametrized test asserts an **exact** `{(check, table, column, severity)}` per fault (table below) |
| No unrelated-column violation | PASS | same | exact-set equality (not a subset), so any extra violation fails |
| F10 warning only | PASS | `test_f10_column_added_is_a_warning_not_breaking` | severities == `{warning}` |
| F8 caught by `freshness.sla` | PASS | matrix row `F8_late_batch` | `(freshness.sla, None, None, breaking)` |
| Missing batch (skip 02-01, `--as-of 2026-02-03`) | PASS | `test_a_skipped_business_date_becomes_a_missing_batch_incident` | `missing_batches == [2026-02-01]`, violation `batch_id is None` |
| De-dup (same fault two days → one incident, batches grow) | PASS | `test_same_fault_on_two_days_keeps_one_open_incident_and_grows_batches` | `created=[id]` then `updated=[id]`; `batches == [02-05, 02-06]`; 2 violations |
| Unit tests per check incl. edge cases | PASS | `pytest tests/unit/test_checks.py` | all-null, empty table, single row, unparseable text, column missing from header, absent table, initial load |
| Lint + mypy | PASS | `dqs dev lint` | ruff clean; `106 files already formatted`; `mypy: no issues in 79 source files` |
| Unit + integration tests | PASS | `dqs dev check` | `172 passed` (unit) + `45 passed` (integration) = **217** |
| Dev warehouse unchanged | PASS | post-suite query | `dq.batches` 30 rows all `loaded`, `dq.incidents` 0, `raw.orders` 13 891; no `dqs_*` DBs left |

## 2. Detection matrix (real run)

Each fault was injected alone on its own day; the table lists the **complete**
violation set the engine returned (from `test_monitor_e2e.py` and a manual probe).

| Fault | Spec | Violations (check · table · column · severity) |
|---|---|---|
| F1 column_rename | `column_rename:orders.customer_id:new_name=cust_id` | `schema.missing_column · raw.orders · customer_id · breaking`; `schema.unexpected_column · raw.orders · cust_id · warning` |
| F2 column_removed | `column_removed:payments.method` | `schema.missing_column · raw.payments · method · breaking` |
| F3 type_change | `type_change:orders.total_amount` | `type.logical · raw.orders · total_amount · breaking` |
| F4 null_spike | `null_spike:orders.status:fraction=0.5` | `nulls.rate · raw.orders · status · breaking` |
| F5 orphan_keys | `orphan_keys:payments.order_id:fraction=0.5` | `fk.integrity · raw.payments · order_id · breaking` |
| F6 duplicate_rows | `duplicate_rows:orders:fraction=0.05` | `unique.pk · raw.orders · order_id · breaking` |
| F7 scale_shift | `scale_shift:orders.total_amount:factor=100` | `range.bounds · raw.orders · total_amount · breaking` |
| F8 late_batch | `late_batch:payments:hours=24` | `freshness.sla · (batch-level) · breaking` |
| F9 missing_table | `missing_table:payments` | `volume.rows · raw.payments · breaking` |
| F10 column_added | `column_added:orders:new_column=coupon_code` | `schema.unexpected_column · raw.orders · coupon_code · warning` |

## 3. Evidence samples

Real CLI run against a clone (`PGDATABASE=dqs_accept`, dropped afterwards):

```
$ dqs check batch batch_20260131
ok no violations
$ dqs monitor run
as-of 2026-02-05T00:00:00+00:00
batches checked: 35
ok no incidents
$ dqs incidents list
0 incident(s)
# after loading a fault day and re-running against it:
$ dqs monitor run --as-of 2026-02-06T00:00Z
as-of 2026-02-06T00:00:00+00:00
batches checked: 1
  created 7653903a breaking raw.payments
$ dqs incidents show 7653903a-...
incident 7653903a-...  open
  fingerprint a3fabd70cd73e36c0638d32190e11df13864c054
  table raw.payments  batches batch_20260205
  ... schema.missing_column · breaking · raw.payments · method · batch_20260205
  1 violation(s)
```

Two real defects were caught during acceptance and fixed before claiming PASS:

1. **Day-1 initial-load volume false positive.** `dqs monitor run` over the clean
   baseline created 2 incidents (`raw.customers` 2000 vs bounds [21,68];
   `raw.products` 200 vs [0,20]). Root cause: the generator excludes the day-1
   initial load from dimension volume bounds (DECISION 36) but the check had no
   way to know. Fix: `Volume.initial_load_excluded` on the contract + the check
   exempts the batch whose business date is `baseline.first` (DECISION 49).
2. **Single-row mean false positive.** `range.bounds` judged the batch mean on a
   1-value sample. Fix: `_MIN_MEAN_SAMPLE = 2` (DECISION 50).

Both were found by running real data, not by reasoning — and both fixes carry a
unit test.

## 4. Honest self-critique (3 weakest points)

1. **The monitor enforces raw contracts only.** `CheckEngine.run_table` exists
   for staged/marts snapshots but nothing calls it yet, so a broken dbt model
   would not raise an incident — only the raw batch that fed it would. This is
   documented (DECISION 51) but it is the biggest gap between "the checks work"
   and "the pipeline is watched".
2. **Coverage is scenario-shaped.** The matrix proves each fault *kind* fires
   once, on one hand-picked day and parameter set. It does not prove the checks
   are robust to boundary parameters (e.g. `null_spike` at exactly the allowed
   rate, `duplicate_rows` large enough to also move volume, `scale_shift` small
   enough to stay inside `mean_bounds`). Those near-boundary behaviours are
   untested and are exactly where false positives/negatives will hide.
3. **`unique.pk`'s cross-batch query has no negative integration test.** The
   within-batch path is unit-tested, but the "id reused from a previous day"
   SQL path (`_cross_batch_collisions`) is only exercised indirectly. A bug
   there would be silent on all clean days.

Also weaker than ideal: `dqs check batch --as-of` is accepted but only
informational (the per-batch checks use the batch's own clock); the freshness
check uses `raw[0]`'s contract for the batch-level SLA (fine today because all
SLAs are 360, brittle if they diverge).

## 5. Deviations and new decisions

`docs/DECISIONS.md` entries 44–54. Notable:

- **44**: determinism test now normalizes only the `status:` line (owner approves
  contracts in place); a guard asserts the committed catalog is fully approved.
- **49**: `Volume.initial_load_excluded` — a new contract field required to make
  DECISION 36 enforceable; this changes the committed contracts (regenerated +
  re-approved).
- **54**: repaired six non-UTF-8 bytes that were breaking repo-wide lint.

## 6. Risks for Phase 3 (lineage + impact)

- **Incident → raw table → dbt nodes.** Phase 3 maps an incident to impacted
  models. `Violation.table` is `None` for batch-level checks (`freshness.sla`,
  `monitor.missing_batch`), so `dqs impact` must handle a *batch-level* incident
  with no table — either fan out to every downstream node or state "pipeline
  wide". This is the first Phase 3 design question.
- **Fingerprint column identity.** `fingerprint = sha1(table|check|column)` uses
  the **raw** column name. F1 renames `customer_id→cust_id`: the incident's
  representative column is `customer_id` (breaking beats the warning) but the
  observed header has `cust_id`. `models_referencing_column("raw.orders",
  "customer_id")` must therefore be driven from the *contract/expected* column,
  not the observed one — a correctness trap for the impact builder.
- **Missing manifest.** The lineage tests the phase calls for need `target/manifest.json`;
  it only exists after a dbt build. The monitor's acceptance used `run_dbt=False`
  clones, so those manifests are absent — Phase 3 must regenerate or ship the
  checked-in fixture exactly as PHASES.md requires.
- **Freshness/missing-batch incidents have no table**, which also means no dbt
  model to fix — Phase 3/4 must route them to an ISSUE, not a PR, early.
- **Staged scope unused** (weak point 1) may tempt Phase 3 to invent lineage for
  violations that were never produced; keep the raw batch scope explicit.
