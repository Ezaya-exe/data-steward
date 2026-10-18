# Phase 2a — Fault injectors + data contracts

Date: 2026-10-18 · Tasks 1–2 (Phase "2a" of `docs/PHASES.md`) · Status: **ready
for review**. Mid-phase review packet: `docs/review/phase-2a-review.md` (its
FAILs are closed below).

## What was built

* `src/dqs/sim/faults.py` — fault injectors F1–F10: one pure-function injection
  registry per `FaultKind` (uniform signature → `(frames, arrival_override,
  GroundTruth)`), the `kind:table[.column][:k=v]` spec parser with
  parse-time validation (fault kind, tables, identifiers, params in (0,1],
  F5 restricted to `FK_MAP` pairs, per-table PK guard for F4, `kind:table:`
  rejected), and `dq`-loadable ground-truth records per injection.
* `src/dqs/cli.py` — `sim day --date ... [--fault SPEC ...]` (repeatable
  option) now reachable end-to-end: generate → inject → load → record.
* `src/dqs/contracts/parsing.py` — shared TEXT→type parsing (parse_rate per
  logical type, type inference order). The profiler and (from Phase 2b) the
  check engine use this one module.
* `src/dqs/contracts/profiler.py` — batch profiling from the loaded warehouse:
  per-batch stats for raw tables (TEXT parse, per-batch min/max/mean,
  distinct/uniqueness, FK orphan anti-joins per `FK_MAP`) and whole-snapshot
  stats for staging/marts (types from Postgres). Re-exposes `FK_BY_TABLE`
  (re-indexed `FK_MAP`) and `SYSTEM_COLUMNS`.
* `src/dqs/contracts/generator.py` — the §7.2 rules as a pure function
  `(TableProfile, Window) → Contract`: measure-only range/mean bounds,
  ×1.5+0.01 null slack, ±25 %-of-baseline range & ±4σ mean windows (with a σ
  floor fallback), volume [0.6·min, 1.5·max] with the dimension day-1 initial
  load excluded, `products` special-cased (0..ceil(0.1×initial)),
  `required_in_batch` only at 100% baseline presence, `allowed_values` at
  ≤12 distinct + ≥99.5% coverage, FK references at ≥99% coverage + ≤1% orphan
  baseline tolerance, freshness 360 min.
* `src/dqs/contracts/store.py` — deterministic YAML (sorted keys, 6-dp floats,
  no aliases, LF, window-derived `generated_at`), strict load (unknown keys are
  errors), `approve` (draft→approved).
* `src/dqs/contracts/generation.py` — one CLI+test entry point
  (`generate_all`): 5 raw + 8 staged contracts; `[]` staged before the first
  dbt build.
* `src/dqs/contracts/fitting.py` — fit/sensitivity harness for the acceptance
  tests (`compare`, `margins`, `near_bound`) + the check-engine thresholds
  (`FK_ORPHAN_MAX 0.005`, `TYPE_PARSE_MIN 0.99`, `NEAR_BOUND_SHARE 0.10`).
  Deliberately NOT the Phase 2b check engine.
* `src/dqs/models/contract.py` — strict Pydantic v2 contract models
  (`extra="forbid"` everywhere; `Volume` gained `required_in_batch`;
  `ColumnReference` validates `schema.table` + identifier regex).
* `contracts/` — the 13 committed contract files (5 raw + 8 staged), all
  `status: draft`.
* CLI: `dqs contracts generate | show | approve`.
* Tests: unit `test_sim_faults` (63 total unit incl. 38 parsing, 29 generator,
  10 store), integration `test_faults_e2e.py` (4) and `test_contracts_e2e.py`
  (16, throwaway DB `dqs_test` — never touch the dev warehouse).

## How to run it

```powershell
.venv\Scripts\dqs contracts generate      # writes contracts/*.yml
.venv\Scripts\dqs contracts show          # catalog + full contract view
.venv\Scripts\dqs contracts approve       # draft → approved (all or by table)
.venv\Scripts\dqs sim day --date 2026-02-05 --fault "column_rename:orders.customer_id:new_name=cust_id"
.venv\Scripts\dqs dev lint ; .venv\Scripts\dqs dev test ; .venv\Scripts\dqs dev test-int
```

## Acceptance results

| Criterion | Verdict | Evidence |
|---|---|---|
| Contracts: profiler/generator/store + CLI | PASS | `dqs contracts generate` wrote 13 files; `show`/`approve` round-trip |
| Byte-identical regeneration | PASS | integration `test_regenerated_contracts_are_byte_identical_to_committed`: two fresh regenerations over the dev baseline had identical bytes **and** matched the committed files (1st run) + SHA256 equal (earlier manual run) |
| Strict loading / unknown keys | PASS | `test_unknown_keys_are_rejected_when_loading` (bogus key → `ContractError`); all 13 files validate (`test_thirteen_contract_files_exist_and_validate`) |
| Held-out fit (clean 2026-01-31…02-04) | PASS | `test_clean_heldout_days_fit_every_raw_contract` — 0 findings on first run, no tolerance widening needed; near-bound report (`out/reports/phase-2a-contract-fit.txt`) shows only `raw.products` volume at margin 0 (expected: a near-empty dimension) |
| Fault sensitivity F1–F7 + F9 | PASS | parameterized `test_fault_pushes_a_stat_outside_the_contract_bounds` — F1/F2→schema.*, F3→type.logical, F4→nulls.rate, F5→fk.integrity, F6→unique.pk, F7→range.bounds+mean_bounds, F9→volume.rows; F10→schema.unexpected_column (bonus) |
| FK inference matches the fault catalog | PASS | `test_contract_files_match_the_fault_catalog_fk_map` (all 4 `FK_MAP` pairs) |
| 13 contracts exist | PASS | names enumerated exactly in the test |
| `sim day --digit`-level e2e fault loads | PASS | `test_faults_e2e.py`: renamed header lands in `dq.batch_tables`, ALTER-branch on clean follow-up, F8 moves `arrived_at`, F9 removes the table (absent ≠ 0 rows) |
| Lint + tests | PASS | ruff check/format clean, `mypy src` clean, **170 passed** (140 unit + 30 integration, one run: `pytest tests -q`) |

## Design choices and why

All logged in `docs/DECISIONS.md` (entries 27–43):

* Contracts come from **loaded DB rows** (through the shared parser), not the
  simulator — so generation and check-time see the same reality.
* **Bounds only on measures**; keys/FK/date columns get uniqueness/null/
  allowed_values rules instead — noise-free contracts.
* **Dimension volume excludes the day-1 initial load**; `products` explicit
  0..ceil(0.1×initial); `required_in_batch` only at 100% baseline presence.
* **Freshness is batch-level** via `dq.batches.arrived_at` (per-table part of
  `late_batch:<table>` is vestigial — DECISION 37).
* `generated_at` derives from the baseline window → byte-identical output.
* Staged layer = whole-table snapshot (`batches: 1`, no FK references).
* **F8 is stat-invisible by design**: the sensitivity test documents empty
  findings while the arrival delay exceeds `freshness.max_delay_minutes`
  ("invisible faults" are reported, not test-fudged; freshness owns it in 2b).
* σ-floor fallback (`_STD_EPS = 1e-9`) fixes a real generator bug found by the
  harness (spread collapsing to ±0 for identical batch means).

## Concepts used

* **Data contracts as *derived rules***: every threshold is computed from an
  observed window (`[0.6·min, 1.5·max]`, ±4σ, ×1.5 null max) — the contract is
  a hypothesis about "normal", provably too loose to reject clean variation
  (headroom shares chosen ≫ day-to-day variance) but tight enough that the ×35
  / ×100 fault factor drives stats outside.
* **Batch-level profiling granularity**: raw tables accumulate per day, so
  every statistic is a *per batch* slice; staging/marts are rebuilt whole by
  dbt, so one snapshot is one observation — mixing the two would invent
  phantom drift.
* **Structural vs statistical detection**: F1/F2/record-layout faults are
  *structural* (headers, presence) and detectable with certainty; value faults
  are *statistical* (rates, means) and need the generation headroom above — the
  fit test proves clean data fits while the sensitivity test proves faults do
  not fit, i.e. the two failure directions are separated.
* **Determinism as a contract artifact requirement**: two DB-state reads yield
  byte-identical files only if nothing wall-clock or object-identity-dependent
  enters the document (sorted keys, no aliases, window-derived timestamps).
* **Anti-join integrity measurement**: FK health = count of non-null child
  values with no parent over non-null count (SQL `NOT EXISTS`) — cheap,
  batchable, index-friendly, and null-safe (nulls are a null-rate concern).
* **Margin reporting (NEAR_BOUND_SHARE = 10%)**: a stat "within 10% of a
  bound" is a generation-quality alarm (too little headroom) even when it
  passes — hence the near-bound evidence file rather than a pass/fail flip.

## Deviations from the docs

* None blocking. Documented: F8's per-table spec part is parseable but
  vestigial (DECISION 37); `Volume.required_in_batch` added beyond the §7.2
  sketch (DECISION 36); staged contracts carry `batches: 1` snapshot semantics
  (DECISION 39); `generate_staged_contracts([])` before the first dbt build
  (DECISION 42).
* One transient environment crash during the first full-suite run: the Docker
  Postgres backend aborted (`stack smashing detected`, signal 6) and the server
  restarted itself mid-suite; WAL recovery kept the dev warehouse intact
  (re-verified: 30 batches / 13 891 order rows) and the full suite then passed
  cleanly. Not a code defect; noted for awareness (watch for recurrence).

## Known limitations / TODO

* `contracts/fitting.py` is intentionally narrow (test harness); the Phase 2b
  check engine must implement the §7.3 severity/fingerprint/incident model on
  top of the shared thresholds.
* Staged contracts snapshot today's state; a dbt model change makes the store
  stale by design until regeneration (human-approved contracts are the
  prevention, not auto-rebase).
* Contract approval state (`draft`) is committed as generated; the owner runs
  `dqs contracts approve` before Phase 2b checks start gating.
* Held-out fit covers raw-layer contracts only in `dqs_test`; staged contract
  fitness is implicitly covered by the dev-warehouse byte-identity test plus
  unit tests.
