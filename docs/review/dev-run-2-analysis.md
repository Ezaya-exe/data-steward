# Dev run 2 — scenario-level failure analysis

Read-only analysis of the newest full dev results file. No code, prompt, scenario,
contract, oracle or result file was changed; this document is the only file added.

- Source: `benchmarks/results/20261028T130606Z_deepseek-deepseek-v4.1-flash.json`
  (2026-10-28T13:06:06Z → 13:40:55Z, run id `20261028T130606Z`)
- Model / provider: `deepseek/deepseek-v4.1-flash` via `openai_compatible`
- Harness `8c.1`, code fingerprint `9104f1938c160646`
- Git `ea9a8f2e76c2f16a70ef8e47684aabb5bb071e13`, `git_dirty=false`
- Split `dev`, repeats `1`, 43 scenario records (37 faults + 6 clean controls), 0 holdout
- `GITHUB_MODE=dry_run`; the run wrote 11 PR previews and 22 issue previews under `out/`
- Companion report: `benchmarks/results/latest.md`

> **What the record keeps, and what had to be recovered separately.** Each
> scenario is a `ScenarioRun` in the JSON: id, kind, split, `error`,
> `infra_error`, `detected`, the per-criterion booleans, category/column/
> resolution comparisons, `tool_calls`, `tokens_in/out`, `cost_usd`, `wall_s`,
> and (new in 8c) a `step_summary` with tool order, per-node tokens, failed
> criteria and event names. The JSON has **no** field for the fixer's per-attempt
> plan, the verifier's per-criterion detail, `finish_reason`, or reasoning
> tokens. The per-scenario Postgres databases are dropped at the end of each
> scenario (`bench/runner.py` → `fast_reset.drop_database`), so `dq.agent_steps`
> and `dq.fix_attempts` are gone. The LangGraph checkpoints survive in
> `data/bench_checkpoints.sqlite` and hold the full graph state, including
> `fix_attempts` with the stored plan and `VerificationResult`. That file was
> read read-only here, which is what makes the attempt-level section below
> possible.

## Headline

| Metric | Value |
|---|---|
| Scenarios scored | 43 (37 faults, 6 controls) |
| Fully correct | 39 (33 faults + 6 controls) |
| Detection | 100.0% (37/37) |
| Root-cause category | 94.6% (35/37) |
| Root-cause columns | 100.0% (27/27) |
| Resolution type | 94.6% (35/37) |
| Fix delivered (all PR-expected) | 84.6% (11/13) |
| Verifier pass (attempted) | 92.3% (12/13) |
| Semantically correct (verified) | 100.0% (12/12) |
| Verified but wrong | 0.0% (0/12) |
| Control false positives | 0.0% (0/6) |
| Infrastructure errors / scenario errors | 0 / 1 |
| Tokens (recorded / corrected) | 982,084 / ≈1,033,904 |
| Estimated cost (recorded / corrected) | $0.09638 / ≈$0.102422 |
| Wall clock | 2,086.868 s (34 m 47 s) |

Four scenarios are not fully correct: `f2-customers-email`,
`f5-order-items-product-35`, `f6-customers-08`, `f6-order-items-15`. Two are
model errors, one is answer-key/environment ambiguity plus a model error, and
two independent harness defects sit on top of `f6-customers-08` and
`f6-order-items-15`.

## 1. `f2-customers-email` — wrong category (F2, column removed)

| | |
|---|---|
| Fault kind | `column_removed` (F2 drops `email` from the batch header) |
| Answer key | `SOURCE_COLUMN_REMOVED`, resolution `ISSUE` |
| Actual | `UPSTREAM_NULLS_INTRODUCED`, resolution `ISSUE` |
| Confidence | 0.90 |
| Steps | 9 tool calls in 4 LLM turns: `get_contract` → `get_batch_profile` → `find_rename_candidates` → `compare_profiles` → `sample_rows` → `get_lineage` → `get_model_sql` → `sample_rows` → `run_readonly_sql` |
| Final channel | `root_cause_tool` (native structured answer, no synthesis) |
| Fix attempts | 0 (resolution was `ISSUE`, so the fixer never ran) |
| Failed criteria | none (no verification happened) |
| Cost | 20,004 tokens, 71.6 s |

**Was decisive evidence available? Yes.** `find_rename_candidates` returned
`missing_columns=['email']`, `new_columns=[]`, and `compare_profiles` reported
`missing_in_b` (present in the older batch, absent in this one). A contract
column that disappeared from the header, was not renamed, and has no
replacement column *is* the F2 signature.

**What went wrong.** The model rejected `SOURCE_COLUMN_REMOVED` with the
sentence "the email column is still present in the batch profile (null_rate
1.0), so it was not dropped from the schema". That is a real property of the
tool output: `get_batch_profile` lists every physical column of `raw.customers`
for the batch, and a column absent from this batch's *header* is loaded as
`NULL`, so it appears in `columns` with `null_rate=1.0`, `non_null=0`,
`distinct=0`. The same payload also says `"header": ["customer_id",
"signup_date", "state"]`. The tool result therefore contained two contradictory
signals, and the model took the numeric one. It is a model error, aggravated by
a profile presentation ambiguity: absence is a schema fact, not a null rate.

**Hypothesis.** One-line fix would be to mark header-absent columns explicitly
(e.g. `in_header: false` and no null stats) so the profile cannot claim a
column "exists, all NULL" when it does not exist — out of scope for this change.

## 2. `f5-order-items-product-35` — wrong category (orphan keys, static dimension)

| | |
|---|---|
| Fault kind | `orphan_keys` (F5 re-appends `order_items` rows with unmatched `product_id`) |
| Answer key | `REFERENTIAL_INTEGRITY_BROKEN`, resolution `ISSUE` |
| Actual | `LATE_OR_MISSING_BATCH`, resolution `ISSUE` |
| Confidence | 0.62 |
| Steps | 9 tool calls in 4 LLM turns: `get_contract` → `get_batch_profile` → `get_contract` → `get_pipeline_run` → `get_batch_profile` → `sample_rows` → 3 × `run_readonly_sql` |
| Final channel | `root_cause_tool` |
| Fix attempts | 0 (resolution `ISSUE`) |
| Failed criteria | none |
| Cost | 23,898 tokens, 34.0 s |

**Was decisive evidence available? Partly — and one contract field points the
other way.** The model did see the decisive number: `raw.order_items`
`product_id` `orphan_rate=0.34982` against `raw.products`, and the failing dbt
`relationships_...ref_stg_products_` test with 389 orphan results. It still
chose `LATE_OR_MISSING_BATCH`, and its two reasons are both defensible from the
tools it had:

1. `get_batch_profile raw.products` for `batch_20260131` returns `row_count=0`
   with a header present, and `run_readonly_sql` confirms `raw.products` only
   ever holds `batch_20260101` (200 rows, `product_id` 1–99).
2. The `raw.products` contract says `required_in_batch: true`, so "the parent
   batch is absent" passes as a contract breach.

But `raw.products` is the day-1 static dimension by design (DECISIONS 36 and
49: dimensions carry a day-1 initial load only; `min_rows: 0` for products;
`required_in_batch` is derived from **baseline** presence, and the baseline
window is where the day-1 load lives). So the tools legitimately show "0 rows
in this batch" for a table that is never supposed to have rows in later
batches, while the generated contract still asserts `required_in_batch: true`.
The orphan rate, not the missing parent batch, is the correct reading.

**Classification.** Answer-key/environment ambiguity compounded by a model
error: the discriminating evidence (FK orphans with a present parent contract)
existed, but a contract field the project itself generated contradicts the
scenario's intended answer. Confidence 0.62 is the model's own signal that it
was unsure; it is the lowest-confidence fault in the run.

**Hypothesis.** Either the generated contract should not set
`required_in_batch: true` for a day-1-only dimension, or the oracle should treat
that combination as ambiguous. Not changed here.

## 3. `f6-customers-08` — no valid plan, then a harness error (F6, duplicate rows)

| | |
|---|---|
| Fault kind | `duplicate_rows` |
| Answer key | `DUPLICATE_RECORDS`, resolution `PR` |
| Actual | `DUPLICATE_RECORDS` (correct), resolution `ISSUE` |
| Confidence | 0.90 |
| Steps | 9 tool calls in 10 LLM turns: `get_contract` → `get_batch_profile` → `sample_rows` → `run_readonly_sql` ×4 → `compare_profiles` → `get_model_sql` (interleaved with 3 fixer calls) |
| Final channel | `root_cause_tool` |
| Fix attempts | 3, all failed |
| Failed criteria | `exact_match` (attempts 1, 2), `fixer_output` (attempt 3) |
| Cost | 0 tokens recorded, **51,820 tokens actually spent**, 66.5 s |

**Diagnosis was correct and well evidenced** (3 keys duplicated exactly,
duplicates only in `batch_20260131`, `stg_customers` a pass-through). The
failure is entirely in the fixer loop and in what the harness did with it.

### Attempt by attempt

| Attempt | Plan produced | Failure | `finish_reason` | Completion / reasoning tokens |
|---|---|---|---|---|
| 1 | `FixPlan` (dedupe by `row_number()` over all four columns) | `exact_match`: "`old` must occur exactly once in `models/staging/stg_customers.sql`, found 0" | (not recorded) | (not recorded) |
| 2 | `FixPlan` (dedupe by `row_number()` over `customer_id`) | `exact_match`: same message, found 0 | (not recorded) | (not recorded) |
| 3 | **none** | `fixer_output`: "structured output could not be parsed: Structured Output response does not have a 'parsed' field nor a 'refusal' field" | `error` | 3,948 / **3,948 (all reasoning)** |

Attempt-level detail recovered from the checkpoint:

- Attempt 1's `edits[0].old` reproduces the model file with **runs of spaces
  where the newlines are** (`select` followed by dozens of spaces before
  `customer_id::integer`). Attempt 2's `old` is the same file **flattened onto
  one line** (`select customer_id::integer  as customer_id, lower(email) ...`).
  Both are close to the file text and neither matches it byte-for-byte, which is
  exactly what a strict `old`-must-occur-once check rejects.
- Attempt 1 and 2 never reached dbt: their criteria lists contain only
  `exact_match` (failed) and `sandbox_cleanup` (passed), with
  `dbt_exit_code=None`.
- Attempt 3 received empty `content`, `tool_calls=[]`, `finish_reason='error'`,
  `completion_tokens=3948` and `completion_tokens_details.reasoning_tokens=3948`.
  The provider spent the whole completion budget on reasoning and returned no
  message body. The reply was therefore **empty and truncated by the reasoning
  budget**, not schema-invalid-for-substantive-reasons.

### The two harness defects behind it

1. **The rejected plan is stored as an error dict.** The invalid attempt is
   persisted as `{"incident_id": ..., "error": "the model did not return a
   schema-valid fix plan: ..."}`, i.e. a dict in the `plan` column that is not a
   `FixPlan`. The issue preview renders this role fine, but every reader that
   assumes `plan` is a plan breaks.
2. **The runner then fails the whole scenario after publishing.**
   `bench/runner.py` calls `publish.store.load_latest_fix_attempt`, which raises
   `PublicationError` ("the latest fix attempt does not validate: 8 validation
   errors for FixPlan"). The issue preview had already been written, so the
   scenario is scored as `error` (detection recovered from the checkpoint, but
   `tokens_in/out = 0`), and the run under-reports 51,820 tokens and about
   $0.0060.

**Hypothesis.** The model's real error is the whitespace-mangled `old` blocks
(a fixer-context/prompt problem, out of scope here); the attempt-3 empty reply
is a provider/decoding limit (needs an output-token cap so reasoning cannot eat
the whole budget); and the harness must tolerate a null plan instead of turning
a published issue into a scenario error.

## 4. `f6-order-items-15` — correct fix routed to an issue (F6, duplicate rows)

| | |
|---|---|
| Fault kind | `duplicate_rows` |
| Answer key | `DUPLICATE_RECORDS`, resolution `PR` |
| Actual | `DUPLICATE_RECORDS` (correct), resolution `ISSUE` |
| Confidence | 0.82 |
| Steps | 10 tool calls (the ceiling) in 7 LLM turns: `get_contract` → `get_batch_profile` → `get_pipeline_run` → `sample_rows` → `compare_profiles` → `get_model_sql` → `run_readonly_sql` ×4 |
| Final channel | `budget_synthesis:native_structured` |
| Fix attempts | 1, **verified** |
| Failed criteria | none |
| Cost | 46,749 tokens, 97.1 s |

The incident, category, columns and fix were all correct. Attempt 1 produced a
`row_number()` dedupe on `stg_order_items` and the verifier passed every
criterion: `dbt_build`, `contract_conformance` (0 violations),
`row_preservation` (35,957 vs 35,957 expected, 100%), `targeted_check`
(0 duplicates, 0 null keys), `sandbox_cleanup`; `dbt_exit_code=0`, and the dbt
log ends `Done. PASS=34 WARN=0 ERROR=0 SKIP=0`.

The graph still ended on `build_issue` because `route_after_verification`
(`src/dqs/workflow/graph.py`) checks `budget_exceeded` **before**
`verification_passed`:

```
if budget_exceeded:      return "budget"     # ← taken, because 10 tool calls was the ceiling
if verification_passed:  return "passed"
```

`budget_exceeded` is set by the investigator hitting `MAX_TOOL_CALLS=10`; the
synthesis at the ceiling then produced a correct answer, but the flag stayed
set for the rest of the run — including through a *successful* verification.

**Hypothesis.** Pure routing-order bug: a verified fix must win over a budget
flag that only affected investigation. It costs a resolution and a PR here, and
it can hide a good fix whenever the investigator runs to the ceiling.

## 5. Failure grouping by cause

| Cause | Scenarios | Impact on the scored metrics |
|---|---|---|
| Model error (misread available evidence) | `f2-customers-email` | 1 category miss (35/37) |
| Answer-key/environment ambiguity + model error | `f5-order-items-product-35` | 1 category miss (35/37) |
| Model error (whitespace-mangled patch `old`) | `f6-customers-08` attempts 1–2 | forced a 3rd attempt |
| Provider limit (reasoning consumed the whole completion) | `f6-customers-08` attempt 3 | no plan → `fixer_output` |
| Harness bug (invalid plan stored as a dict; runner raises) | `f6-customers-08` after publish | scenario `error`, 0 tokens recorded |
| Harness bug (routing order: budget before verified) | `f6-order-items-15` | 1 resolution miss (35/37), 1 PR not opened |

So of the 4 wrong scenarios: 2 are model errors, 1 is ambiguity plus model error,
and 2 harness defects (one of which overlaps the model error in
`f6-customers-08`). Infrastructure errors: 0. Control false positives: 0.

## 6. Synthesis and `failed_generation` salvage

| Path | Uses | Scenarios | Outcome |
|---|---|---|---|
| `root_cause_tool` (native structured answer) | 34/37 | all but the three below | 31 correct, 3 wrong (`f2`, `f5`, `f6-customers-08`) |
| `budget_synthesis=native_structured` (one tools-disabled answer at the 10-call ceiling) | 3/37 | `f5-orders-customer-35`, `f5-payments-order-35`, `f6-order-items-15` | all 3 category-correct; 2 fully correct, 1 (F6-15) misfiled by the routing bug |
| `failed_generation` salvage | **0/37** | — | no provider `tool_use_failed` error occurred in this run |

The ceiling synthesis is doing real work: the two `f5` orphan-keys scenarios that
hit the ceiling were both fully correct, and the ceiling fired on only 3 of 37
faults (8.1%). No `failed_generation` event appears in any `step_summary`, so
the salvage path was not exercised.

## 7. Cost and effort by fault kind

Averages over the recorded token counts (the `f6-customers-08` undercount
described in §9 is included, which lowers `duplicate_rows` by about 8,637
tokens per scenario):

| Kind | n | Avg tokens | Avg wall | Total tokens | Total wall |
|---|---:|---:|---:|---:|---:|
| clean | 6 | 0 | 22.9 s | 0 | 137.5 s |
| column_added | 4 | 16,200.2 | 31.2 s | 64,801 | 125.0 s |
| column_removed | 4 | 22,947.0 | 54.5 s | 91,788 | 217.9 s |
| column_rename | 4 | 25,126.8 | 53.7 s | 100,507 | 214.9 s |
| duplicate_rows | 6 | 33,325.5 | 71.6 s | 199,953 | 429.4 s |
| late_batch | 4 | 23,317.2 | 32.3 s | 93,269 | 129.3 s |
| null_spike | 7 | 26,791.0 | 61.3 s | 187,537 | 429.4 s |
| orphan_keys | 5 | 29,225.0 | 42.6 s | 146,125 | 213.1 s |
| type_change | 3 | 32,701.3 | 63.5 s | 98,104 | 190.4 s |
| **all 43** | 43 | 22,839.2 | 48.5 s | 982,084 | 2,086.9 s |

Clean controls cost no tokens by design (the investigator is never invoked once
the batch is clean), so they only contribute wall-clock: 137.5 s, 6.6% of the
run. `duplicate_rows` is the most expensive fault kind, and it is also the kind
with the two failures — both involve a full 1,279-row / 39-row dedupe
verification.

## 8. Confidence versus correctness

Scenario-level correctness (category **and** columns **and** resolution, plus
delivery when a PR was expected):

| Confidence bucket | n | Correct | Wrong | Wrong scenarios |
|---|---:|---:|---:|---|
| 0.0 – 0.5 | 0 | 0 | 0 | — |
| 0.5 – 0.8 | 4 | 3 | 1 | `f5-order-items-product-35` (0.62) |
| 0.8 – 1.0 | 33 | 30 | 3 | `f6-order-items-15` (0.82), `f2-customers-email` (0.90), `f6-customers-08` (0.90) |
| **all faults** | 37 | 33 | 4 | |

The reported confidence is close to useless as a filter at the top: 3 of the 4
failures sit at ≥ 0.82, and the run's most decisive failure (`f2`, confidence
0.90) is one of them. Only `f5-order-items-product-35` (0.62) is honestly
uncertain. Calibration is also mildly overconfident overall (mean confidence
≈ 0.89 against 33/37 = 89.2% scenario correctness — numerically fine, but the
errors are not concentrated in the low-confidence tail where a review gate
would catch them). Two of those three high-confidence wrongs are harness
defects, not model uncertainty, which explains part of the gap.

## 9. Token accounting: the recorded total is 5% low

`f6-customers-08` recorded `tokens_in=0, tokens_out=0` because the runner raised
before copying the client's counters, yet its `step_summary.tokens_by_node`
shows:

| Node | In | Out |
|---|---:|---:|
| `investigate` | 33,910 | 2,133 |
| `plan_fix` | 7,411 | 8,366 |
| **total** | **41,321** | **10,499** |

That is 51,820 tokens spent but not counted (5.3% of the recorded run), about
$0.0060 at the configured list price. Corrected totals:

| | Recorded | Corrected |
|---|---:|---:|
| Tokens | 982,084 | ≈1,033,904 |
| Estimated cost | $0.09638 | ≈$0.102422 |
| Avg tokens / fault | 26,542.8 | ≈27,943.4 |
| Avg tokens / scenario | 22,839.2 | ≈24,044.3 |

The four failing scenarios account for 90,651 recorded tokens (9.2% of the run)
and ≈142,471 corrected tokens (13.8%), plus 269.2 s of wall clock (12.9%).

## 10. Comparison with dev run 1

Baseline: `benchmarks/results/20261026T181316Z_deepseek-deepseek-v4.1-flash_rescore.json`
(harness `8b.1`, dirty worktree, diagnostic rescore — not a clean measurement).
Both runs are dev, 43 scenarios, repeats 1, same model.

| Metric | Dev run 1 (rescored) | Dev run 2 | Δ |
|---|---:|---:|---:|
| Detection | 33/37 (89.2%) | 37/37 (100%) | +4 |
| RC category | 21/37 (56.8%) | 35/37 (94.6%) | +14 |
| RC columns | 27/27 | 27/27 | 0 |
| Resolution | 31/37 (83.8%) | 35/37 (94.6%) | +4 |
| Fix delivered (all PR-expected) | 7/13 (53.8%) | 11/13 (84.6%) | +4 |
| Verifier pass (attempted) | 7/7 | 12/13 | 5 more passes |
| Semantic (verified) | 7/7 | 12/12 | +5 |
| Verified but wrong | 0/7 | 0/12 | 0 |
| Control FP | 0/6 | 0/6 | 0 |
| Infra errors | 0 | 0 | 0 |
| Scenario errors | 0 | 1 | +1 |
| Tokens | 811,557 | 982,084 (≈1,033,904) | +21% (+27%) |
| Wall clock | 2,155.955 s | 2,086.868 s | −3% |
| Estimated cost | $0.075157 | $0.09638 (≈$0.1024) | +28% (+36%) |

Category accuracy is the big jump (21 → 35), and it is the metric the Phase 8c
prompt and tool work targeted. Cost rose faster than wall clock because more
scenarios now reach the fixer and the verifier (5 more verified fixes, 4 more
delivered PRs), which is the intended trade. The single new scenario error is
the `PublicationError` in §3, not a model regression.

## 11. What this implies for the next change

In scope for this change (harness only; no prompt, tool, scorer, scenario,
oracle or injector edits):

1. Persist an invalid fixer reply as a real attempt with `plan=NULL`,
   `passed=false`, criterion `fixer_output_invalid` and a redacted reason —
   never an error dict in the plan column.
2. Feed that failure back into the next attempt, count it toward
   `MAX_FIX_ATTEMPTS`, and file an issue carrying all failed attempts when the
   attempts run out.
3. Let the publisher, issue builder, PR body, UI service layer and benchmark
   runner read a null-plan attempt without raising (and record the scenario's
   tokens/cost even when publishing already happened).
4. Cap the fixer's completion tokens (`FIXER_MAX_OUTPUT_TOKENS`) and record
   `finish_reason` plus reasoning tokens, so an all-reasoning empty reply is
   visible as such instead of only as "could not be parsed".

Found here but **not** changed, because it is outside the requested scope:

5. `route_after_verification` must check `verification_passed` before
   `budget_exceeded`; a verified fix should not be filed as an issue because
   the investigator ran out of tool calls (`f6-order-items-15`).
6. `get_batch_profile` should mark header-absent columns as such instead of
   reporting `null_rate=1.0` for them (`f2-customers-email`), and
   `required_in_batch` should probably not be `true` for a day-1-only dimension
   (`f5-order-items-product-35`).
7. The fixer prompt/context should make it clear that `old` must be copied
   byte-for-byte, newlines included (`f6-customers-08` attempts 1–2).

## Caveats

- One repeat per scenario: the confidence intervals in `latest.md` come from a
  single Bernoulli sample per scenario; treat ranking between kinds as weak.
- Attempt-level facts come from `data/bench_checkpoints.sqlite` (read-only),
  not from the results file. The checkpoint's last state per thread was used;
  no `dq.fix_attempts` row survives for this run.
- `f6-customers-08`'s token/cost figures are reconstructed from
  `step_summary.tokens_by_node`, not from the (zeroed) counters.
- Cost is an estimate at the configured list price. Actual billing depends on
  the provider.
- `f6-customers-08`'s attempt 1 and 2 `finish_reason` and reasoning-token
  counts were never persisted in 8c.1 (the reason field only exists on the
  argument-error path), so that column is blank for those two rows.
