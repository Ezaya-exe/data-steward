# Phase 2b — Check engine, incidents, monitor

Date: 2026-10-19 · Tasks 3–4 (Phase "2b" of `docs/PHASES.md`) · Status: **ready
for review**. Companion review packet: `docs/review/phase-2b-review.md`.

## What was built

* `src/dqs/checks/base.py` — `CheckContext`, the `Check` protocol, the shared
  `present_columns` guard, and `raw_profile` (wraps `contracts.profiler`, so the
  engine re-parses nothing).
* `src/dqs/checks/{schema,types,nulls,unique,fk,ranges,volume,freshness}.py` —
  every §7.3 check as a tiny class with `name`, `severity`, `run(ctx)`.
* `src/dqs/checks/engine.py` — `CheckEngine.run(batch_id)` (raw batch scope,
  all approved raw contracts + one batch-level freshness pass) and
  `CheckEngine.run_table(table)` (staged/marts snapshot scope for Phase 5);
  `from_directory` loads only **approved** contracts and warns when none exist.
* `src/dqs/models/violation.py` — `Violation` (§7.3, + `batch_id`, optional `table`).
* `src/dqs/models/incident.py` + `models/enums.py` — `Incident` (+ `batches`
  property) and the `IncidentStatus` enum.
* `src/dqs/incidents.py` — `fingerprint`, `build_drafts` (group by
  `(batch_id, table)`, max severity, deterministic representative),
  `persist_incidents` (create / merge on an open fingerprint), `list_incidents`,
  `get_incident`, `_merge` (de-dup by `(batch_id, check, column, message)`).
* `src/dqs/monitor.py` — the deterministic §8 loop: `default_as_of`,
  `unchecked_batches`, `run_monitor` (checks unchecked batches →
  `monitor.missing_batch` → persist incidents → flip batches to `checked`).
* `src/dqs/cli.py` — `dqs check batch`, `dqs monitor run [--as-of]`,
  `dqs incidents list|show`.
* `src/dqs/contracts/store.py` — `load_approved`; `src/dqs/errors.py` — `CheckError`.
* `src/dqs/models/contract.py` + `contracts/generator.py` — `Volume.initial_load_excluded`
  records the DECISION-36 exclusion so enforcement mirrors generation.
* `tests/unit/test_{checks,incidents,monitor}.py` and
  `tests/integration/test_monitor_e2e.py` (template DB).

## How to run it

```powershell
# a clone is used for every command below; the dev warehouse stays clean
.venv\Scripts\dqs contracts approve --all
.venv\Scripts\dqs check batch batch_20260131
.venv\Scripts\dqs monitor run
.venv\Scripts\dqs incidents list
.venv\Scripts\dqs incidents show <uuid>
.venv\Scripts\dqs dev lint ; .venv\Scripts\dqs dev test ; .venv\Scripts\dqs dev test-int
```

## Acceptance results

| Criterion | Verdict | Evidence |
|---|---|---|
| Zero false positives on baseline + clean 2026-01-31…02-04 → 0 incidents | PASS | `test_clean_baseline_and_heldout_days_produce_no_incidents` and real `dqs monitor run`: `as-of 2026-02-05T00:00:00+00:00 / batches checked: 35 / ok no incidents`, `incidents list` → `0 incident(s)` |
| F1–F10 fire on the expected table/column, nothing unrelated | PASS | parametrized `test_engine_flags_exactly_the_expected_violations` (exact `{(check, table, column, severity)}` equality per fault) |
| F10 warning only | PASS | `test_f10_column_added_is_a_warning_not_breaking` (severity set == {warning}) |
| F8 caught by `freshness.sla` | PASS | F8 row of the matrix asserts `freshness.sla` (table/column `None`) |
| Missing batch: skip 2026-02-01, `monitor run --as-of 2026-02-03T00:00Z` | PASS | `test_a_skipped_business_date_becomes_a_missing_batch_incident` (`missing_batches == [2026-02-01]`, violating batch has `batch_id=None`) |
| De-dup: same fault two days → one open incident, batch list grows | PASS | `test_same_fault_on_two_days_keeps_one_open_incident_and_grows_batches` (created→updated same id, `batches == [02-05, 02-06]`, 2 violations not 4) |
| Unit test per check incl. edge cases | PASS | `test_checks.py` — all-null column, empty table, single-row batch, unparseable values, column missing from the header, absent table, initial-load exemption |
| Lint + mypy + all tests green | PASS | `dqs dev lint` (ruff clean; mypy: 79 files, no issues) then `dqs dev check` (see below) |
| Dev warehouse unchanged | PASS | after the whole suite: `dq.batches` = 30 rows all `loaded`, `dq.incidents` = 0, `raw.orders` = 13 891 rows; no `dqs_*` clone DBs left |

Integration evidence (real run): **45 passed** (whole `tests/integration`),
including 15 in `test_monitor_e2e.py`. Unit: **172 passed**. See the review
packet for the full detection-matrix table and the weakest points.

## Design choices and why

Logged in `docs/DECISIONS.md` (entries 44–54):

* **No cascades**: value checks read only `present_columns ∩ contract.columns`
  (a missing column is reported once, by `schema.missing_column`); an absent
  table is owned by `volume.rows` when `required_in_batch` (F9 = one violation).
* **No duplicate parsing**: the engine builds profiles through the same
  `contracts.profiler` the generator uses.
* **Approved contracts only**; a loud warning when none exist.
* **`volume.initial_load_excluded`** so a clean baseline's day-1 initial load
  does not fail its own volume window (real false positive found while probing).
* **Mean signal needs a sample** (`_MIN_MEAN_SAMPLE = 2`): a 1-value "mean" is
  not a distribution shift (real false positive found by unit tests).
* **Fingerprint `sha1(table|check|column)`** from a deterministic representative
  violation, so consecutive-day faults merge into one incident.
* **Explicit `as_of` watermark**; missing-batch walks every date to `as_of−1`.
* **Raw batch scope only** in the monitor; staged scope exists as
  `run_table()` for Phase 5.

## Concepts used

* **Deterministic detection**: every §7.3 rule is a pure function of the
  contract plus already-profiled facts; only cross-batch uniqueness and FK
  anti-joins touch SQL. No LLM is anywhere near "is this batch wrong?".
* **De-duplication by identity, not by time**: `sha1(table|check|column)` is a
  *fault* identity independent of the batch, so "the same broken thing again
  tomorrow" is one incident whose batch list grows — the operational meaning of
  de-dup.
* **Watermark / event-time processing**: the batch's `business_date` and the
  run's `as_of` define which dates are expected; a missing date is provable
  against that window without guessing wall-clock time.
* **Representative selection for stable identity**: when several checks fire
  together, "highest severity, then sorted check/column/message" picks the same
  violation every day, keeping the fingerprint stable.
* **Generation/enforcement symmetry**: a bound derived excluding a batch must be
  enforced excluding the same batch; the contract now carries that fact, so the
  two sides cannot disagree.
* **Idempotent monitoring**: marking consumed batches `checked` means a second
  run touches only new batches, which is what makes the de-dup test deterministic.

## Deviations from the docs

* None blocking. New contract field `Volume.initial_load_excluded` needed to make
  DECISION 36 enforceable (DECISION 49); `Violation.batch_id`/optional `table`
  (DECISION 46); monitor raw-scope-only (DECISION 51); incident de-dup
  representative rule (DECISION 48).
* `docs/DECISIONS.md` contained six non-UTF-8 bytes that broke `ruff format
  --check .` repo-wide; repaired (DECISION 54).

## Known limitations / TODO

* `dqs monitor run` enforces raw contracts only; staged/marts contracts are
  reachable via `CheckEngine.run_table` but not yet run by the monitor (Phase 5).
* No cost/token bounds apply yet — Phase 2b is pure code; bounded autonomy is a
  Phase 4/8 concern.
* The integration suite seeds the 30-day baseline into a template once (~2.5 min
  for `test_monitor_e2e.py`); acceptable now, will grow with more scenarios.
* `range.bounds`' isolated-value signal and the `unique.pk` cross-batch query are
  covered at unit/integration level but not by a benchmark scenario yet.
