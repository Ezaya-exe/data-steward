# Dev run 1 — scenario-level failure analysis

Read-only analysis of the newest dev results file. No code, prompt, contract, or
result file was changed; this document is the only new file.

- Source: `benchmarks/results/20261026T181316Z_deepseek-deepseek-v4.1-flash.json`
  (produced 2026-10-26T18:13:16Z → 19:14:32Z, run id `20261026T181316Z`)
- Model / provider: `deepseek/deepseek-v4.1-flash` via `openai_compatible`
- Harness `8b.1`, code fingerprint
  `17ac403b512ffd73d0b07df91047d55df846aa0e72c1c787d35315b91f1c6a26`
- Split `dev`, repeats `1`, 43 scenario records (37 faults + 6 clean controls)
- Git: `ce8b65bfb4f044db7848aa36a3d759cf016960d0`, **dirty worktree**
- Companion report: `benchmarks/results/latest.md`

This is a diagnostic, not benchmark evidence: the worktree was dirty and the
file's metadata label (see "Caveats") is inconsistent with its rows. It is not
quoted in `README.md` or `docs/CLAIMS.md`.

> **What the record stores, and what survives alongside it.** Each scenario is a
> `ScenarioRun` (`src/dqs/bench/scoring.py`): id, kind, split, repeat, `error`,
> `infra_error`, `detected`, the per-criterion correctness booleans,
> `verified_but_wrong`, expected/actual category, columns and resolution,
> `expected_checks`, `tool_calls`, `tokens_in`, `tokens_out`, `cost_usd`,
> `wall_s`. The JSON has **no** field for the verifier's criteria, the number of
> fix attempts, the investigator's final channel, or the `failed_generation`
> salvage path. The per-scenario Postgres databases were dropped (verified: no
> `dqs_bench_*` database remains), so `dq.agent_steps` / `dq.fix_attempts` are
> gone. However, the LangGraph checkpoints survive in
> `data/bench_checkpoints.sqlite`, and they retain the full graph state
> (`attempt_no`, `attempts`, `fix_attempts`, `verification_passed`, `errors`).
> That file was read read-only for this analysis; it supplies the attempt counts
> and verifier criteria in the section below. The `failed_generation` salvage
> step is **not** in the checkpoints, so that one remains unrecorded.

## Headline

| Metric | Value |
|---|---|
| Scenarios scored | 43 (37 faults, 6 controls) |
| Fully correct | 23 (17 faults + 6 controls) |
| Not fully correct | **20** (20 faults, 0 controls) |
| Detected | 33/37 (the 4 misses are all `late_batch`) |
| Category correct | 21/37 |
| Columns correct | 27/27 scored (duplicate rows are not column-scored) |
| Resolution correct | 31/37 |
| Verifier passed | 7/13 PR-expected |
| Semantically correct | 7/13 |
| Verified-but-wrong | 0/13 |
| False positives on controls | 0/6 |
| Errors / infra errors | 0 / 0 |
| Real tokens | 811,557 (731,773 in + 79,784 out) |
| Wall clock | 2,155.955 s (≈ 35 min 56 s) |

"Fully correct" counts the 6 controls as correct (no false positive), plus the 17
fault scenarios that met every scored criterion. The 20 failures below are all
fault scenarios; no control raised an incident.

All 20 failures show two failure modes, and together they explain every reported
shortfall (16 category misses, 4 detection misses, 6 resolution misses, and 6
PR-expected scenarios that ended without a verified fix). The six
`verifier_passed = False` rows inside Group A are **not** failed verifications:
those scenarios never reached a fix attempt (see below).

## Every scenario that was not fully correct

Legend: **n/s** = not scored (duplicate rows have no acceptable-column list, so
`rc_columns_correct` is `None`); **None** in the verifier column means
`verifier_passed = None` (not a PR-expected scenario); **False** means
PR-expected but no verified fix. Tokens are `tokens_in + tokens_out`; wall is
`wall_s`.

Selection rule: a row is listed if any of `rc_category_correct`,
`rc_columns_correct`, `resolution_correct` or `verifier_passed` is `False`,
`verified_but_wrong` is `True`, `error`/`infra_error` is set, or it is a fault
scenario with `detected = False`. That yields the 20 rows below. No row had a
wrong column, a verified-but-wrong fix, an error, or an infra error.

### Group A — investigation stopped at the 10-call budget (16 scenarios)

| # | Scenario | Kind | Answer key: category · columns · resolution · check | Recorded result: category · columns · resolution · verifier | Tools | Tokens | Wall (s) | One-line hypothesis |
|---:|---|---|---|---|---:|---:|---:|---|
| A1 | `f3-order-items-unit-price` | type_change | SOURCE_TYPE_CHANGED · unit_price · PR · `type.logical` | **UNKNOWN** · unit_price · **ISSUE** · verifier **False** | 10 | 27,978 | 37.2 | The 10-call cap was reached before the final answer; the two passing type_change runs used 7 calls. |
| A2 | `f4-customers-state-35` | null_spike | UPSTREAM_NULLS_INTRODUCED · state · ISSUE · `nulls.rate` | **UNKNOWN** · state · ISSUE · verifier None | 10 | 14,345 | 31.2 | The 10-call cap was reached; the two passing null-spike runs used 8 calls. |
| A3 | `f4-payments-method-20` | null_spike | UPSTREAM_NULLS_INTRODUCED · method · ISSUE · `nulls.rate` | **UNKNOWN** · method · ISSUE · verifier None | 10 | 18,866 | 32.5 | Same cap; the nulls.rate violation and the column were already identified. |
| A4 | `f4-order-items-quantity-60` | null_spike | UPSTREAM_NULLS_INTRODUCED · quantity · ISSUE · `nulls.rate` | **UNKNOWN** · quantity · ISSUE · verifier None | 10 | 16,112 | 33.9 | Same cap; no final `submit_root_cause` was reached. |
| A5 | `f4-orders-customer-20` | null_spike | UPSTREAM_NULLS_INTRODUCED · customer_id · ISSUE · `nulls.rate` | **UNKNOWN** · customer_id · ISSUE · verifier None | 10 | 23,831 | 43.8 | Same cap; the run spent all calls on evidence gathering. |
| A6 | `f4-order-items-unit-price-35` | null_spike | UPSTREAM_NULLS_INTRODUCED · unit_price · ISSUE · `nulls.rate` | **UNKNOWN** · unit_price · ISSUE · verifier None | 10 | 22,394 | 34.9 | Same cap; a different column of the same table also exhausted the budget. |
| A7 | `f5-orders-customer-35` | orphan_keys | REFERENTIAL_INTEGRITY_BROKEN · customer_id · ISSUE · `fk.integrity` | **UNKNOWN** · customer_id · ISSUE · verifier None | 10 | 15,177 | 32.5 | All 5 FK runs hit the cap, so the parent/anti-join path needs more calls than the budget allows. |
| A8 | `f5-order-items-order-35` | orphan_keys | REFERENTIAL_INTEGRITY_BROKEN · order_id · ISSUE · `fk.integrity` | **UNKNOWN** · order_id · ISSUE · verifier None | 10 | 22,954 | 38.7 | Same FK budget pressure; detection and column were correct. |
| A9 | `f5-payments-order-20` | orphan_keys | REFERENTIAL_INTEGRITY_BROKEN · order_id · ISSUE · `fk.integrity` | **UNKNOWN** · order_id · ISSUE · verifier None | 10 | 21,652 | 43.1 | Same FK budget pressure. |
| A10 | `f5-payments-order-35` | orphan_keys | REFERENTIAL_INTEGRITY_BROKEN · order_id · ISSUE · `fk.integrity` | **UNKNOWN** · order_id · ISSUE · verifier None | 10 | 19,457 | 81.6 | Same FK budget pressure; also the slowest orphan run. |
| A11 | `f5-order-items-product-35` | orphan_keys | REFERENTIAL_INTEGRITY_BROKEN · product_id · ISSUE · `fk.integrity` | **UNKNOWN** · product_id · ISSUE · verifier None | 10 | 21,597 | 43.3 | Same FK budget pressure. |
| A12 | `f6-orders-03` | duplicate_rows | DUPLICATE_RECORDS · — (n/s) · PR · `unique.pk` | **UNKNOWN** · order_id (n/s) · **ISSUE** · verifier **False** | 10 | 21,193 | 53.3 | The 10-call cap was reached; the one passing duplicate run used 9 calls. |
| A13 | `f6-order-items-05` | duplicate_rows | DUPLICATE_RECORDS · — (n/s) · PR · `unique.pk` | **UNKNOWN** · order_item_id (n/s) · **ISSUE** · verifier **False** | 10 | 22,721 | 35.1 | Same cap; duplicate identity was found (`unique.pk` fired) but not submitted. |
| A14 | `f6-payments-15` | duplicate_rows | DUPLICATE_RECORDS · — (n/s) · PR · `unique.pk` | **UNKNOWN** · payment_id (n/s) · **ISSUE** · verifier **False** | 10 | 21,421 | 60.1 | Same cap. |
| A15 | `f6-customers-08` | duplicate_rows | DUPLICATE_RECORDS · — (n/s) · PR · `unique.pk` | **UNKNOWN** · customer_id, email (n/s) · **ISSUE** · verifier **False** | 10 | 20,264 | 53.1 | Same cap; the forced UNKNOWN named two columns but never reached a verdict. |
| A16 | `f6-order-items-15` | duplicate_rows | DUPLICATE_RECORDS · — (n/s) · PR · `unique.pk` | **UNKNOWN** · order_item_id (n/s) · **ISSUE** · verifier **False** | 10 | 20,922 | 37.6 | Same cap; the only duplicate-row pass (`f6-order-items-03`) finished at 9 calls. |

Group subtotal: 330,884 tokens, 691.7 s, 16/16 with `tool_calls = 10`.

### Group B — batch-level `late_batch` cannot match the table-scoped detection rule (4 scenarios)

| # | Scenario | Kind | Answer key: category · columns · resolution · check | Recorded result: category · columns · resolution · verifier | Tools | Tokens | Wall (s) | One-line hypothesis |
|---:|---|---|---|---|---:|---:|---:|---|
| B1 | `f8-customers-30` | late_batch | LATE_OR_MISSING_BATCH · — · ISSUE · `freshness.sla` | LATE_OR_MISSING_BATCH · — · ISSUE · verifier None · **detected False** | 6 | 24,305 | 53.9 | `freshness.sla` is a batch-level violation (`table=None`), but detection requires an incident on the scenario's table, so it can never match. |
| B2 | `f8-payments-48` | late_batch | LATE_OR_MISSING_BATCH · — · ISSUE · `freshness.sla` | LATE_OR_MISSING_BATCH · — · ISSUE · verifier None · **detected False** | 4 | 13,686 | 31.5 | Same scope mismatch; diagnosis and resolution were still correct. |
| B3 | `f8-order-items-24` | late_batch | LATE_OR_MISSING_BATCH · — · ISSUE · `freshness.sla` | LATE_OR_MISSING_BATCH · — · ISSUE · verifier None · **detected False** | 8 | 35,466 | 72.2 | Same scope mismatch; this is also the most expensive scenario in the run. |
| B4 | `f8-order-items-30` | late_batch | LATE_OR_MISSING_BATCH · — · ISSUE · `freshness.sla` | LATE_OR_MISSING_BATCH · — · ISSUE · verifier None · **detected False** | 5 | 20,021 | 63.4 | Same scope mismatch; the incident itself was raised and investigated correctly. |

Group subtotal: 93,478 tokens, 221.0 s.

## Verifier criteria and attempts

The requirement was to list the failed criteria and attempt counts per scenario.
The **saved JSON** contains neither: `ScenarioRun.verifier_passed` is a single
`bool | None`, and the runner copies only `attempt[1].passed` from
`load_latest_fix_attempt(...)` (`src/dqs/bench/runner.py:361-362`). The
LangGraph checkpoints, however, were not cleaned up. `data/bench_checkpoints.sqlite`
holds 401 checkpoints / 2,315 writes, including **37 threads from this run**
(12 older threads from the 2026-10-23 run were excluded by checkpoint
timestamp). Each thread's state carries `attempt_no`, `attempts`,
`fix_attempts`, `verification_passed` and the full `VerificationResult`, so the
attempt history can be recovered exactly. The file was opened read-only.

**How `verifier_passed` is computed.** It is only populated for PR-expected
scenarios: `run.verifier_passed = observation.verification_passed is True`
(`src/dqs/bench/scoring.py:221-222`). When no `dq.fix_attempts` row exists the
runner records `observation.verification_passed = None`
(`src/dqs/bench/runner.py:361-362`), and `None is True` is `False`. So `False`
means **"no verified fix"**, which covers both "the verifier ran and failed" and
"the verifier never ran". `semantically_correct` is populated in the same branch
(`src/dqs/bench/scoring.py:223`), so the six also read `False` there — the same
`None is True` artifact, not a semantic mismatch. `verified_but_wrong` requires
`verification_passed is True`, so it stays `False`.

**What the checkpoints show for this run:**

| Observation | Count |
|---|---:|
| Threads belonging to this run | 37 |
| Threads with at least one fix attempt | 7 |
| Verification results that failed | **0** |
| Retries (`attempt_no > 1`) | **0** |
| Verified PRs (attempt 1, all criteria passed) | 7 |
| Threads with no attempt at all | 30 |

The 7 attempts are exactly the 7 PR scenarios that passed. Every one recorded
`attempt_no = 1` and a `VerificationResult` whose five criteria were all `True`:

| Criterion | Result in all 7 |
|---|---|
| `dbt_build` | pass |
| `contract_conformance` | pass |
| `row_preservation` | pass, 100.0% in every case (e.g. 14,362/14,362 orders; 35,957/35,957 order_items) |
| `targeted_check` | pass (e.g. 0 duplicate/NULL `order_item_id`; `amount` casts to numeric) |
| `sandbox_cleanup` | pass |

There were **no failed criteria anywhere in the run**, so there is no criterion
list to report for the 20 incomplete scenarios. The mechanism is proven by a
counter-example in the same checkpoint file from the 2026-10-23 run: one thread
there has three stored attempts, all failed on `contract_conformance`
(null rate 96.68% on non-nullable `method`), `attempt_no = 3`, final ISSUE. A
failure in this run would have left exactly that trace; none exists.

**The six `verifier_passed = false` rows.** All six are Group A scenarios that
were forced to `UNKNOWN` → `ISSUE` before `plan_fix` was reached, so
`dq.fix_attempts` never got a row: attempts = 0 and the verifier never ran.

| Scenario | Expected resolution | Actual resolution | `verifier_passed` | Failed criteria | Attempts |
|---|---|---|---|---:|---|---|
| `f3-order-items-unit-price` | PR | ISSUE | False | none — verifier never ran | 0 |
| `f6-orders-03` | PR | ISSUE | False | none — verifier never ran | 0 |
| `f6-order-items-05` | PR | ISSUE | False | none — verifier never ran | 0 |
| `f6-payments-15` | PR | ISSUE | False | none — verifier never ran | 0 |
| `f6-customers-08` | PR | ISSUE | False | none — verifier never ran | 0 |
| `f6-order-items-15` | PR | ISSUE | False | none — verifier never ran | 0 |

That also explains the only apparent contradiction in the record: these six are
PR-expected, ended as ISSUE because no actionable diagnosis was produced, and
were therefore scored `False` by the `is True` coercion. They are a downstream
symptom of Cause 1, **not** an independent verification failure.

Every other row either has `verifier_passed = True` (the 7 verified PRs) or
`verifier_passed = None` (the 24 non-PR fault scenarios plus the 6 controls).
The 30 fault threads with no attempt are those 24 non-PR scenarios plus the six
rows above. `None` means "not applicable", not "passed".

## Cause groups

### Cause 1 — the investigator's tool-call cap ends the run before a final answer (16 / 20)

Evidence:

- All 16 have `tool_calls = 10`, exactly `max_tool_calls` from the run settings.
- The two passing `null_spike` runs used 8 calls; the two passing `type_change`
  runs used 7; the one passing `duplicate_rows` run used 9.
- `investigate()` breaks out of the loop when the tracker reaches the cap and
  then returns `_unknown_result(...)`, which is `UNKNOWN` + `ISSUE`
  (`src/dqs/agents/investigator.py:538-541`, `686-698`, `257-274`). It never
  asks the model for a last answer once the cap is hit.
- The correlation is exact (16 UNKNOWN ↔ 16 at-cap runs) and the outcome matches
  the code path, so this is the leading explanation, not merely a guess.
- Detection was still correct in all 16 (the expected check fired), and the
  column was right wherever columns are scored; only the final diagnosis was
  lost.

Sub-patterns (all the same root cause, different call cost):

| Kind | Failing / total | Calls used by failures | Calls used by passes | Reading |
|---|---:|---|---|---|
| `null_spike` | 5 / 7 | 10, 10, 10, 10, 10 | 8, 8 | Needs ~8–10 calls; the budget cuts the tail. |
| `orphan_keys` | 5 / 5 | 10 × 5 | — | FK diagnosis consistently exceeds 10; no pass to compare. |
| `duplicate_rows` | 5 / 6 | 10 × 5 | 9 | Right at the boundary; one run finished in time. |
| `type_change` | 1 / 3 | 10 | 7, 7 | Single outlier at the cap. |

### Cause 2 — `late_batch` detection is structurally unmatchable (4 / 20)

Evidence:

- `FreshnessSlaCheck` emits a violation with `table=None`
  (`src/dqs/checks/freshness.py:40-57`), because freshness is a batch property.
- Incidents are grouped by `(batch_id, violation.table)`, so that incident's
  `table` is `None` (`src/dqs/incidents.py:94-113`).
- `_detected` requires `_table_name(incident.table) == scenario.table.lower()`
  (`src/dqs/bench/scoring.py:149-159`). A `None` table can never equal
  `"customers"`, `"payments"`, or `"order_items"`.
- The consequence is visible in the data: `detected=False` in all four, while
  the root cause (`LATE_OR_MISSING_BATCH`) and resolution (`ISSUE`) are both
  correct. This is a **scoring/scope mismatch**, not an agent miss.
- It is also why `latest.md` shows `late_batch` at 0/4 detection above a 4/4
  category and 4/4 resolution.

### Not causes

- **Columns:** 27/27 scored column answers were right. No failure is a
  column-attribution error.
- **Verified-but-wrong / false positives:** 0/13 and 0/6. No "fix that passes
  verification but changes the answer" and no clean-control incident.
- **Infrastructure:** `error` and `infra_error` are empty in all 43 rows, and
  `stop_reason` is null; there is no quota/rate-limit contamination.

## `failed_generation` salvage usage

**Not recorded — the count is unknown, not zero.**

- The salvage path exists for OpenAI-compatible gateways that answer a malformed
  final tool call with HTTP 400 `tool_use_failed`. It records a `system` step
  named `provider_tool_use_failed` with `output.path = "salvaged"` or
  `"fallback_final_channel"` and, on success, uses the parsed root cause
  (`src/dqs/agents/investigator.py:557-578`).
- That step is written to `dq.agent_steps` **inside the scenario database**.
  The results record keeps no such field, and the scenario databases were
  dropped (`SELECT datname FROM pg_database` now lists only `warehouse`,
  `dqs_demo`, `dqs_template`, `postgres`, `template0`, `template1`).
- The investigator's `final_channel` is likewise not persisted, so
  `failed_generation` cannot be inferred from the `UNKNOWN` rows either.
- The retained `data/bench_checkpoints.sqlite` was checked as a second source: it
  stores graph state only (`attempts`, `fix_attempts`, `errors`, …), not the
  per-step log, so it does not contain the salvage event either.
- With 0 `error`/`infra_error` rows there is no indirect signal of provider
  tool-call failures. Any claim that it "never happened" would be unsupported.

## Average cost per fault kind

Averages are per scenario over the whole run (`tokens = in + out`). The clean
row is 6 control runs with no agent call.

| Kind | n | Avg tokens | Avg wall (s) | Total tokens | Total wall (s) |
|---|---:|---:|---:|---:|---:|
| clean | 6 | 0.0 | 23.0 | 0 | 137.8 |
| column_added | 4 | 16,776.5 | 91.7 | 67,106 | 367.0 |
| column_removed | 4 | 18,749.0 | 48.5 | 74,996 | 193.9 |
| column_rename | 4 | 27,546.0 | 68.7 | 110,184 | 274.7 |
| duplicate_rows | 6 | 23,472.5 | 50.1 | 140,835 | 300.5 |
| late_batch | 4 | 23,369.5 | 55.2 | 93,478 | 221.0 |
| null_spike | 7 | 21,140.9 | 37.3 | 147,986 | 261.2 |
| orphan_keys | 5 | 20,167.4 | 47.8 | 100,837 | 239.1 |
| type_change | 3 | 25,378.3 | 53.6 | 76,135 | 160.7 |

- Overall average: 18,873.4 tokens and 50.1 s per scenario (811,557 / 43;
  2,155.955 / 43).
- Faults only: 21,934.0 tokens and 54.5 s per fault scenario (37 rows).
- The 20 failing scenarios consumed **424,362 tokens (52.3% of the run)** and
  **912.7 s (42.3%)**. Group A alone is 330,884 tokens (40.8%); Group B is
  93,478 (11.5%).
- The cheapest fault kind on average is `column_added` (16.8k tokens) but it has
  the highest average wall time (91.7 s, driven by one 225.8 s outlier); the
  most expensive per scenario is `column_rename` (27.5k tokens).

## Real totals

Recomputed by summing every scenario record and compared with the file's
metadata:

| Quantity | Sum of records | `metadata` | Match |
|---|---:|---:|---|
| Input tokens | 731,773 | — | — |
| Output tokens | 79,784 | — | — |
| **Total tokens** | **811,557** | **811,557** | exact |
| Estimated cost (USD) | 0.075157 | 0.075157 | exact |
| Wall seconds | 2,155.955 | 2,155.955 | exact |

The cost is an estimate at list price for the recorded token counts; the report
says the free tier was used and nothing was billed. Total tokens are the real
measured usage, not an estimate.

## Caveats about this file (why it is diagnostic only)

1. **Dirty worktree.** `git_dirty = true` at `ce8b65b`. The code fingerprint
   matches the current tree in every fingerprinted path, so the code that
   scored these rows is consistent with this analysis, but a dirty tree is not
   reproducible byte-for-byte and is not benchmark evidence.
2. **Inconsistent catalogue label.** Metadata says `catalogue_size = 4`,
   `fault_count = 3`, `control_count = 1`, while the file contains 43 scored
   scenarios (37 faults + 6 controls) and the report table has n = 43. The
   label is wrong; the rows are the source of truth. This was already logged
   in `docs/DECISIONS.md` 144 and `docs/HANDOFF.md`.
3. **Partial provenance.** Attempt counts and verifier criteria had to be
   recovered from `data/bench_checkpoints.sqlite`; the results JSON alone does
   not carry them. `final_channel` and `failed_generation` salvage events are
   still not persisted anywhere (the scenario Postgres databases were dropped
   after the run), so those remain unknown.
4. **`repeats = 1`.** There is no variance measurement; each scenario is a
   single sample. A different sample of the same model could move the boundary
   cases (the 9-vs-10-call duplicates in particular).

## What I would check next (no changes made here)

1. Whether the cap-forced `UNKNOWN` disappears with a larger
   `MAX_TOOL_CALLS` or by asking for a best-effort answer before the loop
   breaks — the 16 rows suggest the budget, not the reasoning, is the binding
   constraint.
2. Add `final_channel`, `budget_exceeded`, attempt count and `failed_criteria`
   to the per-scenario record so a future analysis does not depend on the
   checkpoint file.
3. Decide whether `_detected` should be batch-scoped for `late_batch` (accept a
   `table=None` incident for the faulted table), or whether the answer key
   should expect `monitor.missing_batch`; either way the current pair is
   structurally incompatible.
4. Persist `failed_generation` salvage events outside the scenario database (they
   currently live only in `dq.agent_steps`, which is dropped with it), so the
   salvage rate can be measured at all.
