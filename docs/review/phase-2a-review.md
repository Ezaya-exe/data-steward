# Phase 2a review packet

Date: 2026-10-18 · Scope: Phase 2 tasks 1–2 (fault injectors + contracts)
**Status: PARTIAL — task 2 (contracts) is NOT started; this packet reports honestly.**

## 1. Acceptance checklist

| Criterion | Verdict | Command | One-line result |
|---|---|---|---|
| Injectors F1–F10 + ground truth + parser + `sim day --fault` | PASS (code only) | `dqs dev lint` / see caveats | lint clean on `src/` incl. mypy; F10 has a registry bug (§5) |
| Unit test per fault (exact mutation) | **PARTIAL** | pytest on corrected draft copy | 28 passed / 6 failed — 3 failures are real code bugs, 3 are test bugs |
| Parser tests incl. malformed specs w/ clear errors | **PARTIAL** | same run | negatives work except: trailing `:` accepted; non-FK `orphan_keys` fails only at inject-time, not parse-time |
| Pure-function: skip-day purity (day 3 after skipping day 2) | **FAIL (not written)** | — | the dedicated test was never written; generator purity does have 5 Phase-1 tests that pass |
| Contracts: profiler/generator/store + `contracts generate\|show\|approve` | **FAIL (not started)** | `ls src\dqs\contracts` | only `Placeholder module` stubs exist (285 B each), all 13 tables ungenerated |
| Contract determinism (byte-identical, equal hashes) | **FAIL (not started)** | — | no contract files exist |
| Unknown YAML keys rejected | **FAIL (not started)** | — | `models/contract.py` exists with `extra="forbid"` but store/loader/tests absent |
| Held-out fit (clean 01-31…02-04 inside bounds) | **FAIL (not started)** | — | warehouse still at exactly the clean 30-batch baseline (max business_date 2026-01-30) |
| `sim day --fault column_rename…` → `dq.batch_tables` shows `cust_id` | **FAIL (not run)** | — | no e2e fault-load test; CLI flag parsing verified only via a typer probe |
| Lint equivalent + unit tests pass | **PARTIAL** | `dqs dev lint` / `dqs dev test` | `src/` clean; draft `test_faults.py` has 7 ruff findings + import errors |
| Integration tests for the above pass | **FAIL (not written)** | `dqs dev test-int` (draft stashed) | 10 passed = Phase-1 regression only |

## 2. State

- `git log --oneline` (phase work): `c67b2e2 feat: fault injectors F1-F10, spec
  parser, ground-truth records, --fault CLI` — the only phase-2a commit.
- `git status`: clean except `?? tests/unit/test_faults.py` (uncommitted draft,
  never executed in-repo).
- Tests: unit **22 passed** (committed state); integration **10 passed**
  (Phase-1 loader/db tests). Phase-2a integration tests: **0**.
- e2e probes this session: `dqs db ping` OK (Postgres 16.15); typer probe for a
  repeatable `--fault` (`Annotated[list[str]|None, Option]`) collected
  `['a:orders', 'b']` and absent → `None`.

## 3. Evidence samples

- `contracts/raw.orders.yml`: **does not exist** — cannot paste.
- Ground truth records (real run: date 2026-02-01, seed 42):
  - F1 `{"kind":"column_rename","table":"orders","column":"customer_id",
    "parameters":{"new_name":"cust_id"},"expected_checks":
    ["schema.missing_column","schema.unexpected_column"],
    "expected_category":"SOURCE_COLUMN_RENAMED","expected_resolution":"PR",
    "fix_shape":"staging: coalesce(original, renamed) then drop the renamed column"}`
  - F6 `{"kind":"duplicate_rows","table":"orders","column":null,
    "parameters":{"fraction":0.1},"expected_checks":["unique.pk"],
    "expected_category":"DUPLICATE_RECORDS","expected_resolution":"PR",
    "fix_shape":"staging: row_number() dedupe by PK keeping the latest _loaded_at"}`
- `dq.batch_tables` row with `cust_id`: **not produced** — F8/F9/F1 e2e loads untested.
- Held-out fit table: **not produced** (contracts don't exist).

## 4. Contract thresholds

Not implemented. Planned per AREA §7.2: nullable-else `max_null_rate = 1.5×max
+ 0.01`; range `[min − 0.25·span, max + 0.25·span]` clipped at 0 for non-negative
amounts; `mean_bounds = mean ± 4·std` (±25 % fallback); volume `[0.6·min,
1.5·max]` with day-1 excluded for dimension tables; allowed_values when
distinct ≤ 12; `freshness.max_delay_minutes = 360`.
Planned deviation to log: raw-table profiles derived from the deterministic
generator, not the DB (staging/marts still from Postgres) — concrete contract
mine doesn't say; will be DECISIONS entry at task-2 time.

## 5. Honest self-critique (3 weakest points)

1. **Sequencing failure**: contracts (task 2) untouched; approval flow,
   determinism hashes and the held-out-fit guard are all pending. Nothing in
   this phase is protected against overfit contracts.
2. **F10 `column_added` injector missing from the register** — parse
   validates it, but `apply_faults` hits `KeyError` in `_INJECTORS`; three
   draft tests fail exactly there. Found only by finally running tests.
3. **Testing loop broken**: I wrote the test file and committed task 1 without
   running the unit gate. The draft also has real test bugs — F5's non-orphan
   positional comparison (values are right, ordering assumption wrong) — and
   the spec grammar is under-defined: `null_spike:orders.order_ts:` (empty
   params after the second colon) currently parses as "no parameters" rather
   than erroring, so the trailing-colon negative I invented fails.

Also weaker than it should be: F8 is only covered at injector level (no e2e
arrival-time override in Postgres test); the CLI's fault-echo output and
`loader.arrived_at` override have no tests; parser accepts unknown empty
params block.

## 6. Deviations and new decisions

- DECISIONS.md: **no entries added yet for 2a** (the 2a trade-offs need to be logged
  before or during task 2). Needed-entries: F5 domain limit (4 known FK pairs),
  null_spike key rejection scope (per-table PK only), loader `arrived_at` override (doc-required, is not a deviation), and the generator-vs-DB raw profiling choice.
- Documented deviations from the docs: none yet declared; potential ones
  under consideration are above.

## 7. Risks for Phase 2b (false positives / missed faults)

- **FP risk — dimension volume**: contracts must exclude the day-1 initial
  load; on the products table (0 rows after day 1) an empty histogram must
  become `min_rows: 0` explicitly or every clean day fails `volume.rows`.
- **FP risk — expanding key columns**: contiguous-expanding ids mean a static
  `range` bound on e.g. `raw.customers.customer_id` will fail within a few
  days — contracts should either skip range checks on such keys or treat
  them like a growing domain; this must be decided before generation.
- **Miss risk — F5 coverage limited to 4 known FK columns** (`orders.customer_id`,
  `order_items.order_id`, `payments.order_id`, `order_items.product_id`);
  benchmark variants outside those will inject-time errors.
- **Miss risk — `dq.batch_tables` header only** reflects the loader's
  per-batch view; F9's "table absent" never produces a row — the check engine
  must treat absence (not 0 rows) as the fault, per §7.3 `volume.rows`.
- **Miss risk — parent lookup via `dq.batches` staleness**: orphan detection
  (fk.integrity) needs the *cumulative* parent set; it must include all loaded
  batches (incl. day-1 initial loads) and stay load order-agnostic.
- **Sim-text vs cast mismatch**: raw contracts profiled from the generator's
  Python floats vs the loader's `str()` round-trip must stay inside the same
  parse-rate thresholds; any typed-vs-text divergence (e.g. profiled as
  `1234.5` but loaded text `"1234.5"`) in real columns is a false-positive
  source the held-out fit test must catch.
