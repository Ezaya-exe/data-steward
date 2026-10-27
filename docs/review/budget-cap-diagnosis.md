# Budget-cap diagnosis - four capped dev scenarios

Read-only diagnosis of *why* the investigator ends with `UNKNOWN`/`ISSUE` on
scenarios that a human can diagnose from the same warehouse. One diagnostic run
of four scenarios, one per capped fault family from dev-run-1. No prompt, tool,
contract, scenario or injector changed before the run.

- Results: `benchmarks/results/20261027T125219Z_deepseek-deepseek-v4.1-flash.json`
- Metadata: `diagnostic=true` ("diagnostic selection; not a representative
  benchmark run"), split `dev`, repeats `1`, harness `8c.1`, code fingerprint
  `e56a665138a589f2`, git `cbe85cd`, clean worktree
- Model/provider: `deepseek/deepseek-v4.1-flash` via `openai_compatible`; the
  same model as dev-run-1, so the cap behaviour is comparable
- Limits: `MAX_TOOL_CALLS=10`, `MAX_TOKENS` unchanged, daily cap 180,000
  (0 tokens spent earlier that UTC day)
- Command: `dqs bench run --split dev --model deepseek/deepseek-v4.1-flash
  --ids f5-orders-customer-35,f4-customers-state-35,f6-orders-03,f3-order-items-unit-price
  --diagnostic`

This file is diagnostic evidence. It is not a benchmark result and must not be
quoted as one.

## What was measured

| scenario | fault | expected resolution | observed | tool calls | LLM calls | tokens in/out | wall |
|---|---|---|---|---:|---:|---|---:|
| `f5-orders-customer-35` | orphan keys, 35% | ISSUE | UNKNOWN / ISSUE | 10 | 4 | 19,425 / 1,420 | 40.0 s |
| `f4-customers-state-35` | null spike, 35% | ISSUE | UNKNOWN / ISSUE | 10 | 4 | 18,200 / 1,167 | 101.7 s |
| `f6-orders-03` | duplicate rows, 3% | PR | UNKNOWN / ISSUE | 10 | 4 | 17,468 / 1,133 | 35.0 s |
| `f3-order-items-unit-price` | type change, 50% | PR | UNKNOWN / ISSUE | 10 | 4 | 18,266 / 1,364 | 37.9 s |

All four stopped at exactly the tool-call limit, after four model turns. The
only recorded stop event in every `step_summary` is `budget_exhausted`. There
was no `root_cause_tool`, `root_cause_tool_invalid`, `provider_tool_use_failed`
or `final_channel_fallback` event in any record.

Every scenario was detected correctly (`detected=true`). Category accuracy is
0/4 because the only category ever produced is `UNKNOWN`. Resolution looks 2/4
correct only because the safe `UNKNOWN` fallback always routes to ISSUE, which
happens to match the two scenarios that expect an issue; neither run produced
the diagnosis that would make the routing meaningful.

## Tool sequences

`get_contract` and `get_batch_profile` were always first. The full sequences
from the result records:

**Orphan keys (`f5-orders-customer-35`)**
`get_contract, get_batch_profile, get_lineage, get_contract, compare_profiles,
sample_rows, get_batch_profile, run_readonly_sql, run_readonly_sql,
run_readonly_sql`

**Null spike (`f4-customers-state-35`)**
`get_contract, get_batch_profile, find_rename_candidates, sample_rows,
compare_profiles, run_readonly_sql, run_readonly_sql, run_readonly_sql,
get_pipeline_run, run_readonly_sql`

**Duplicate rows (`f6-orders-03`)**
`get_contract, get_batch_profile, sample_rows, run_readonly_sql,
compare_profiles, run_readonly_sql, run_readonly_sql, get_pipeline_run,
get_model_sql, get_lineage`

**Type change (`f3-order-items-unit-price`)**
`get_contract, get_batch_profile, sample_rows, compare_profiles, get_model_sql,
run_readonly_sql, run_readonly_sql, run_readonly_sql, get_pipeline_run,
get_lineage`

## Evidence sufficiency at the stop

The per-scenario database and its `dq.agent_steps` rows are dropped after each
scenario; the checks below use the stored `step_summary` (tool names) plus the
partial findings copied into the deterministic issue previews
(`out/issue_previews/`). Those previews keep the last five observations and
truncate each one, so this is evidence that *enough was available*, not a
transcript of everything the model saw.

- **Orphan keys.** The last successful query returned `raw.orders.customer_id`
  in `[1, 999]` (`n=3197`, `d=3197`) while `raw.customers.customer_id` is
  `[3162, 3197]` (`n=36`, `d=36`). The violation already states a 35.03% orphan
  rate against `raw.customers.customer_id`. That is a direct, decisive orphan
  signature; the correct answer needed no further tool.
- **Null spike.** The decisive query returned `null_state=13` of 36 rows
  (36.11%) for `batch_20260131`, and 0 of 38 and 0 of 36 for the two prior
  batches. `get_batch_profile` plus the state distribution shows the spike is
  confined to the failing batch. Nothing further was required to name a
  `null_spike` on `state`.
- **Duplicate rows.** `unique_stg_orders_order_id` failed with "Got 14 results"
  in the stored dbt run, and a SQL result listed the same `order_id`s with
  `n=2`. The duplicate-key evidence was already complete; the last two calls
  (`get_model_sql`, `get_lineage`) added no discriminating fact.
- **Type change.** The stored dbt error is `invalid input syntax for type
  numeric: "₹761.38"`; the query results show 193 `₹`, 185 `$` and 178 `€`
  first characters in `raw.order_items.unit_price`, next to 304/298/297 rows
  starting with digits in the clean comparison. A currency symbol in a numeric
  column is the type-change signature.

In all four runs the model was still *collecting* at the cap, but the evidence
needed for the answer was already on the table. The cap, not missing evidence,
decided the outcome.

## Repeats and avoidable work

- `get_contract` was called twice in the orphan-keys run; `get_batch_profile`
  was also called twice there.
- Every scenario spent three or four of its ten calls on `run_readonly_sql`.
- `get_lineage` was the *last* call in the duplicate-rows and type-change runs,
  after `compare_profiles` and `get_model_sql`. Impact analysis already computes
  lineage deterministically after the investigation, so this call cannot change
  the diagnosis.
- The null-spike run called `find_rename_candidates` although the violation was
  a null-rate spike. Trying to falsify a rename hypothesis is legitimate, but
  it consumed 10% of the budget.

## Failed or rejected tool calls

At least 6 of the 40 executed calls (15%) returned a deterministic error instead
of evidence:

| error | count | scenarios |
|---|---:|---|
| `column "batch_id" does not exist` (the column is `_batch_id`) | 3 | f5, f4, f3 |
| `function 'and' is not allowed in read-only agent SQL` | 2 | f5, f6 |
| `schema 'information_schema' is not readable` | 1 | f4 |

The counts are a lower bound: the issue preview keeps only the last five
observations, so an earlier error in `f6-orders-03` may exist. These are
tool-usability findings (a common column-name mistake, an over-strict boolean
guard, a schema the model reasonably expected to read), not budget-policy
findings. They are recorded here but are outside the investigator fix below.

## Truncation and compaction

- Every tool return is wrapped in `<data>…</data>` and capped at 4 KB
  (`tools/base.py`), so a large `get_batch_profile` payload is cut inside a
  nested object. In the orphan-keys run only the first column's profile is
  visible in the preview before the cut.
- The runner printed one compaction warning:
  `f5-orders-customer-35 sent 6180 input tokens in one call (> 6000); check
  tool-result compaction`. The other three runs did not cross that threshold.
- Truncation did not cause the stop: the cap is on tool *calls*, and the
  decisive facts above were present in the returned (and previewed) results.

## Rejected finish attempts

None. There were four model turns in every run and the last turn asked for more
tools, not for `RootCause`. No finish attempt was rejected, malformed or
salvaged. This rules out the "the model tried to answer and the harness threw it
away" explanation.

## Cause, stated plainly

The loop checks `tool_calls >= max_tool_calls` at the **top** of the next
iteration. When the tenth tool call is recorded the loop breaks and immediately
emits the deterministic `UNKNOWN` escalation; the model is never asked for an
answer. The model is not stuck in an unproductive loop: in all four runs it had
already observed the fact that decides the diagnosis, and it was still spending
calls on confirmations, duplicate lookups, a wrong hypothesis and avoidable SQL
errors. Dev-run-1 showed the same shape in 16 of its 20 failures, and none of
its 20 reported a verifier failure.

So the dominant cause is **a finalization gap at the tool-call ceiling**, not a
reasoning deficit: the model has enough evidence but gets no turn to say so.
Secondary contributors observed here: two duplicate lookups, one wrong-hypothesis
probe, at least 6/40 wasted calls on guard/SQL errors, and a lineage call that
duplicates deterministic impact analysis.

## What this supports

This supports the conditional investigator fix:

1. expose `calls_remaining` in every tool result so the model can see the
   ceiling approaching;
2. at the ceiling, make **one** tools-disabled call that asks for the
   `RootCause` from the evidence already gathered, with the same schema,
   provenance filter and confidence rules;
3. only if that call fails or returns invalid output, fall back to
   `UNKNOWN` + `ISSUE`.

It does *not* support raising `MAX_TOOL_CALLS`, adding tools, or tuning prompts
towards the four fault names: raising the ceiling pays for more of the same
wasted calls, and the evidence was already sufficient.

## After the fix: one-scenario confirmation

The conditional fix was implemented and checked on the *same* orphan-keys
scenario with the same model, limits and diagnostic flag. This is a wiring
confirmation, not a benchmark result (n=1, one repeat).

- Results: `benchmarks/results/20261027T131407Z_deepseek-deepseek-v4.1-flash.json`
  (diagnostic, harness `8c.1`, code fingerprint `9104f1938c16`)
- Tool sequence: the same 10-call ceiling was reached
  (`get_contract, get_batch_profile, get_pipeline_run, get_lineage,
  get_batch_profile, sample_rows, get_contract, run_readonly_sql ×3`)
- Events: `budget_exhausted`, then `budget_synthesis=native_structured`,
  then `final_channel=budget_synthesis:native_structured`
- Answer: `REFERENTIAL_INTEGRITY_BROKEN` on `raw.orders.customer_id`,
  confidence **0.65**, `ISSUE` (the expected resolution and category; the
  scorer reports `rc_category=1.0`)
- Seven evidence items, every citation a tool that was actually called; the
  answer explicitly records that `sample_rows` returned no violating rows.

The ceiling itself is unchanged: the run still spent all ten calls (30,955
tokens versus 20,845 before). The fix does not make the model cheaper; it stops
the ceiling from throwing away a diagnosis that is already supported. One
scenario cannot show how often the synthesis call succeeds or whether its
confidence is calibrated; that needs the next clean benchmark run.

## Limits of this diagnosis

- One repeat per scenario, one model, one day. Scenario-level variance is not
  measured here.
- `step_summary` stores tool names and counts, not arguments or full outputs;
  the previews are truncated at the last five observations.
- The four scenarios were chosen because dev-run-1 capped on their fault
  families; this is a directed diagnostic, not a random sample.
- `f6-orders-03` was recorded as `detected=true` but its check is the dbt
  unique test; detection here means the monitor raised the expected check, not
  that the model named it.
