# Dev run 3 analysis — `20261031T115043Z` (harness `8c.2`)

Read-only review of the newest full dev benchmark. No benchmark was run for this
analysis. The scenario databases had already been dropped, so attempt-level facts
come from `data/bench_checkpoints.sqlite`; the single non-retryable harness failure
was reproduced only as far as the recorded evidence allows (see §3.6).

## 1. Run identity and headline

| field | value |
|---|---|
| result file | `benchmarks/results/20261031T115043Z_deepseek-deepseek-v4.1-flash.json` |
| run | `20261031T115043Z`, split `dev`, repeats 1 |
| model | `deepseek/deepseek-v4.1-flash` via `openai_compatible` |
| harness | `8c.2` |
| git | `b1e475f709c4267f91fad3f22a3da6a9eb7aaf7c`, **dirty worktree** |
| code fingerprint | `a2677ce3a77acbb3` |
| prompts | unchanged vs the previous dev run (`fixer=e22cd76a004d`, `investigator=5dfee302b290`) |
| catalogue | 43 scenarios: 37 faults, 6 controls, 0 holdout |
| started / finished | 2026-10-31T11:50:43Z / 14:29:00Z |
| totals | 1,105,668 tokens, $0.116565 estimated at list price, 3,584.0 s |

Headline (deterministic comparison against the answer key):

| scope | n | detection | RC category | RC columns | resolution | fix delivered (all PR) | verifier pass (attempted) | semantic (verified) | verified wrong | FP rate | infra | errors |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| overall | 43 | 37/37 | 35/37 | 27/27 | 33/37 | 9/13 | 9/11 | 9/9 | 0/9 | 0/6 | 0 | 1 |

Two metadata facts matter for the numbers below:

- The run was **resumed**. `bench-dev3b.log` starts with `resuming … 5 completed,
  38 to run`; scenario threads 1–5 ran 11:51–12:10Z and threads 6–37 ran
  13:47–14:26Z. The ≈97-minute gap is the operator pause, not agent time, and is
  why the per-scenario wall times do not sum to `wall_s_total`.
- The tree was dirty, so this run cannot be quoted as a clean-tree benchmark. It
  is still the recorded evidence for what the agents did.

## 2. The seven report lines are six scenarios

`latest.md` lists seven lines under “Scenarios to look at”, but
`f6-order-items-03` appears twice: once as the reported error and once as a
detected-but-wrong-resolution scenario. The unique set is:

| # | scenario | kind | answer-key resolution | actual resolution | category correct | fix delivered |
|---|---|---|---:|---:|---|---|
| 1 | `f6-order-items-03` | `duplicate_rows` | PR | `None` (workflow error) | yes | no |
| 2 | `f1-orders-status` | `column_rename` | PR | ISSUE | yes | no |
| 3 | `f2-customers-email` | `column_removed` | ISSUE | ISSUE | no (`UNKNOWN`) | n/a |
| 4 | `f2-payments-amount` | `column_removed` | ISSUE | ISSUE | no (`UPSTREAM_NULLS_INTRODUCED`) | n/a |
| 5 | `f6-orders-03` | `duplicate_rows` | PR | ISSUE | yes | no |
| 6 | `f6-order-items-05` | `duplicate_rows` | PR | ISSUE | yes | no |

Every one of them was detected. Four of the six have a correct diagnosis and fail
later (fix planning or infrastructure); two have a wrong category but the right
resolution.

## 3. Scenario-by-scenario

Confidence below is `RootCause.confidence` from the thread's final checkpoint.
“Tools/LLM” comes from the stored step summary.

### 3.1 `f1-orders-status` — wall-clock, not a fixer failure

- **Fault / answer key:** rename `raw.orders.status` → `order_status`; expected
  `SOURCE_COLUMN_RENAMED`, expected fix is a staging `coalesce(status, order_status)`
  that keeps the `status` name, resolution PR.
- **What happened:** the diagnosis is correct and well evidenced: rename score
  0.85, name similarity 0.667, value overlap 1.0, identical value domain, dbt run
  green, 8 evidence findings, confidence 0.95. Category and columns are correct.
- **Step summary:** 9 tools, 6 LLM calls, 33,495 tokens, 331.2 s; events
  `budget_exhausted`, `root_cause_tool`, `final_channel=root_cause_tool`; tool
  sequence `get_contract, find_rename_candidates, sample_rows, get_model_sql,
  get_pipeline_run, compare_profiles, run_readonly_sql, get_lineage, run_readonly_sql`.
- **Fix attempts:** zero stored attempts. The only attempt in the graph state is a
  `planning` failure with no plan:
  `wall-clock budget exceeded: 305.6s > 300s (INCIDENT_TIMEOUT_SECONDS)`.
  The fixer was never called; the workflow wrapped up as an ISSUE. Because the
  budget failure is returned before persistence, `dq.fix_attempts` has 0 rows.
- **Empty/cut-off reply:** no fixer reply exists.
- **Verdict:** harness budget, not the model. The wall-clock limit is checked
  *after* a call returns, so the scenario overshot 300 s by 5.6 s inside the
  investigation and by ~31 s in total; the fix never got a turn.

### 3.2 `f2-customers-email` — wall-clock before the first tool call

- **Fault / answer key:** remove `customers.email`; expected
  `SOURCE_COLUMN_REMOVED`, resolution ISSUE.
- **What happened:** category `UNKNOWN`, confidence 0.00, resolution ISSUE
  (correct). No tools ran at all.
- **Step summary:** 0 tools, 1 LLM call, 3,265 tokens, 380.5 s; events
  `budget_exhausted`, `final_channel=budget_unknown`. The single call took
  ~353.5 s, and the budget was only checked when it returned:
  `wall-clock budget exceeded: 353.5s > 300s`, so the workflow escalated to
  UNKNOWN with no evidence.
- **Fix attempts:** none (0 rows); the ISSUE is the budget answer.
- **Empty/cut-off reply:** none; there is one investigator call, not a fixer
  reply.
- **Verdict:** provider latency plus a budget that cannot interrupt an in-flight
  call. Run 2 failed this scenario for a different reason (see §5); the two runs
  are not evidence of a regression in the same mechanism.

### 3.3 `f2-payments-amount` — the column-removal ambiguity

- **Fault / answer key:** remove `payments.amount`; expected
  `SOURCE_COLUMN_REMOVED`, resolution ISSUE.
- **What happened:** category `UPSTREAM_NULLS_INTRODUCED`, confidence 0.90,
  resolution ISSUE (correct). The summary says the column is “physically present
  … but 100% NULL”, and the model explicitly rejected
  `SOURCE_COLUMN_REMOVED` because `run_readonly_sql` returned `amount` in the
  table.
- **Why the evidence points that way:** the `column_removed` injector drops the
  column from the batch header/frame; the loader has already created the
  physical `raw.payments.amount` column on an earlier day, and only columns in
  the new header are inserted. The new batch therefore has `amount = NULL` for
  every row while the column still exists. The header metadata
  (`dq.batch_tables.columns`) and the `schema.missing_column` check carry the
  removal signal; a SQL profile cannot distinguish “removed from the feed” from
  “feed stopped populating it”.
- **Step summary:** 9 tools, 5 LLM calls, 27,096 tokens, 52.1 s; events
  `root_cause_tool`, `final_channel=root_cause_tool`; tools end with two
  `run_readonly_sql` calls that confirmed the physical column.
- **Fix attempts:** none, because the answer key expects an ISSUE and the
  workflow correctly did not plan a fix. `failed_criteria` is empty.
- **Empty/cut-off reply:** n/a.
- **Verdict:** model error *given the evidence*, rooted in a known fixture
  ambiguity (the same one recorded for run 2's `f2-customers-email`). It is not
  a verifier or fixer-context flaw, and it is not fixed in this pass.

### 3.4 `f6-orders-03` — fixer produced an `old` that is not in the file

- **Fault / answer key:** 3% duplicate rows in `raw.orders`; expected
  `DUPLICATE_RECORDS`, expected fix a `row_number()` dedupe by `order_id`,
  resolution PR.
- **What happened:** correct category (0.78) and diagnosis. 10 tools, 7 LLM
  calls, 39,419 tokens, 105.6 s; events `budget_exhausted`,
  `budget_synthesis=native_structured`,
  `final_channel=budget_synthesis:native_structured`. The investigation hit the
  10-call ceiling, then produced a valid final answer at the ceiling.
- **Fix attempt 1** (`native_structured`, plan present): summary “Add DISTINCT to
  the stg_orders select …”; edit
  `models/staging/stg_orders.sql`, `old = "select distinct"`,
  `new = "select distinct\n    order_id::integer             as order_id,"`.
  Verification failed `exact_match`: `'old' must occur exactly once in
  models/staging/stg_orders.sql, found 0`; `sandbox_cleanup` passed. Nothing was
  written or built.
- **Why:** the file starts with `select\n    order_id::integer …`; it has no
  `select distinct`. The model invented the anchor and also reversed the edit
  (it wanted to add `distinct`). The candidate model was correct
  (`stg_orders` directly selects `source('raw','orders')`) and the context
  contains the file verbatim (see §4).
- **Empty/cut-off reply:** no; the reply parsed into a plan. No `finish_reason`
  is recoverable because the step log lived in the dropped scenario DB.
- **Verdict:** fixer capability, not context or verifier. `exact_match` correctly
  refused an anchor that would not apply.

### 3.5 `f6-order-items-05` — three whitespace-mangled anchors

- **Fault / answer key:** 5% duplicate rows in `raw.order_items`; expected
  `DUPLICATE_RECORDS`, `row_number()` dedupe by `order_item_id`, resolution PR.
- **What happened:** correct category (0.88) and diagnosis. 8 tools, 8 LLM
  calls, 46,198 tokens, 72.6 s; events `root_cause_tool`,
  `final_channel=root_cause_tool`. Budget was not exhausted; all three allowed
  attempts were used.
- **Attempts (all `native_structured`, all plans present, all fail
  `exact_match`):**
  1. idea: add `DISTINCT`; `old = "select distinct"` → found 0.
  2. idea: dedupe with a CTE and `row_number()`; `old = "select
     order_item_id::integer    as order_item_id,"` (the model joined the first
     two file lines with spaces) → found 0. `sandbox_cleanup` passed.
  3. idea: `row_number()` subquery; same space-joined `old` prefix → found 0.
     `sandbox_cleanup` passed.
- **Why:** the real file is:
  ```
  select
      order_item_id::integer    as order_item_id,
  ```
  The model collapsed the newline into spaces in all three anchors. It never
  reproduced the file text byte-for-byte, as the (unchanged) prompt requires.
- **Empty/cut-off reply:** none; all three replies parsed.
- **Verdict:** fixer capability. The candidate (`stg_order_items`) is correct,
  the context is verbatim, and `exact_match` is doing its job.

### 3.6 `f6-order-items-03` — the real dbt error was a native CPython crash

- **Fault / answer key:** 3% duplicate rows in `raw.order_items`; expected
  `DUPLICATE_RECORDS`, dedupe fix, resolution PR.
- **What happened:** the investigation completed correctly (category 0.92, 9
  tools, 5 LLM calls, 31,211 tokens; tool sequence includes `get_model_sql` but
  no `get_lineage`). The workflow then called the impact node, which built the
  lineage graph; `load_manifest` judged the manifest stale and ran
  `dbt compile`. That process died, so `LineageError: dbt compile failed while
  regenerating the manifest` killed the workflow before any fixer attempt
  (`fix_attempts=0`, resolution `None`). 36.9 s total.
- **What the harness saw** was only three stdout lines (the 8c.2 code kept the
  last 1,500 characters):
  ```
  Running with dbt=1.12.6
  Registered adapter: postgres=1.11.0
  Unable to do partial parsing because profile has changed
  ```
  There is no dbt `Compilation Error` or `Database Error` because dbt never got
  to report one.
- **Real cause, from the dbt log and Windows Error Reporting:**
  - `warehouse/dbt_project/logs/dbt.log` contains the invocation at
    `19:44:43.661` local time (14:14:43Z), invocation id
    `d5b2c6fe-7d86-4ac1-98ce-0ae9c1269de2`. It printed the same three lines and
    then **nothing**; the next invocation begins at `19:45:10` (the next
    scenario).
  - The Windows Application log has an `Application Error` at `19:44:44` local
    (14:14:44Z): `python.exe` 3.12.10150.1013, faulting module `python312.dll`,
    exception code `0xc0000005` (access violation), fault offset `0x78f61`,
    report id `6719a71c-ef63-48c9-bde6-db3972a76453`. The WER report for that id
    lists `dbt_extractor.pyd` among the loaded modules, which ties the crashed
    process to the dbt child (the benchmark process itself kept running).
  - The crashed process start time decodes to 14:14:42.11Z — the moment the
    workflow asked for the graph; dbt's own log starts 1.5 s later after imports.
- **Conclusion:** a native crash of the dbt child (CPython access violation
  during project parsing/partial parsing, with dbt's Rust `dbt_extractor`
  loaded), not a model error, not a SQL error, and not a broken project. Running
  the same command today on the same tree (`dbt compile --project-dir
  warehouse/dbt_project --profiles-dir warehouse/dbt_project`) exits 0. The
  harness had no retry and no way to distinguish this from an agent failure, so
  the scenario was scored `error`.
- **Contributing factor:** `manifest.regenerate` does not receive `Settings`; it
  inherits the parent process environment. The scenario's `dbt build` runs with
  `PGDATABASE` set to the scenario database (`dbt_env(settings)`), while the
  lineage compile inherits the parent environment (normally `warehouse`). That
  is why dbt reports “profile has changed” and discards the partial parse on
  every such regeneration. It is not the crash itself (the compile is not
  data-dependent), but it is why the full parse path is exercised inside a
  scenario. Documented, not changed in this pass.

### 3.7 Empty or cut-off fixer replies in this run

- Among the six scenarios above: **no empty reply and no cut-off reply**. Five
  produced no fixer call at all (`f1`, both `f2`s, `f6-order-items-03`); the two
  F6 fixers produced parseable plans with bad anchors.
- Across all 37 fault scenarios there is exactly one invalid fixer reply:
  `f6-order-items-15` attempt 1. Its criterion says
  `LengthFinishReasonError: Could not parse response content as the length limit
  was reached - CompletionUsage(completion_tokens=8192, …,
  completion_tokens_details=…reasoning_tokens=4132…)`.
  That is a **cut-off** at the 8,192-token ceiling after 4,132 reasoning tokens.
  Attempt 2 then succeeded and the scenario delivered a verified PR.
- No empty (zero-content / all-reasoning) reply occurred in run 3, unlike run 2's
  `f6-customers-08`.
- Recovery limit: `finish_reason` / `reasoning_tokens` were written to
  `dq.agent_steps`, which lived in the scenario database and was dropped. The
  checkpoint stores attempts, not step rows, so only the exception text in the
  criterion remains. The cut-off is unambiguous, but the structured fields are
  not re-readable after the run. Stage 2 keeps them in the failure record too.

## 4. F6: capability, context, or verifier?

The three F6 failures split cleanly:

| scenario | affected table | candidate selected | correct file? | failure |
|---|---|---|---|---|
| `f6-orders-03` | `raw.orders` | `stg_orders` | yes — it is the only staging model selecting that source | `old` does not exist (`select distinct`) |
| `f6-order-items-05` | `raw.order_items` | `stg_order_items` | yes | `old` reflowed (newline → spaces), 3 times |
| `f6-order-items-03` | `raw.order_items` | n/a | n/a | dbt child crashed before planning |

- **Not a fixer-context flaw.** `candidate_staging_models` resolves the affected
  raw table through the manifest, keeps staging models that directly depend on
  it, and reads each file with `read_text` — the full, exact SQL is in the
  `<data>` block. The root cause's `affected_table` was correct in both cases, so
  the right file was offered. There is no truncation in this path (the only
  bound is the 4 KB data envelope, and both models are far smaller).
- **Not a verifier mismatch.** `exact_match` requires the anchor to occur exactly
  once. Both anchors occur zero times, so no edit could have been applied
  deterministically; accepting them would either fail the patch or apply it to
  the wrong text. The verifier is correct to reject.
- **Fixer capability.** The model either invented `select distinct` or collapsed
  a newline into spaces despite the prompt's “copied byte-for-byte” rule. A
  contributing presentation factor is that the SQL arrives inside compact JSON
  (newlines are `\n` escapes), which makes literal copying harder; that is a
  usability observation, not incorrect context. The classification is capability,
  so Stage 2 proceeds and the prompt stays frozen.

## 5. Run 2 vs run 3

Run 2 = `20261028T130606Z` (harness `8c.1`, clean tree, 43 scenarios). Fault
scenarios that were not fully correct:

| scenario | run 2 | run 3 | note |
|---|---|---|---|
| `f2-customers-email` | **fail** — category `UPSTREAM_NULLS_INTRODUCED` | **fail** — category `UNKNOWN` (wall-clock, 0 tools) | failed in both, different mechanisms |
| `f5-order-items-product-35` | **fail** — `LATE_OR_MISSING_BATCH` | pass | run-2 contract/day-1-empty-products ambiguity |
| `f6-customers-08` | **fail** — empty all-reasoning reply + `PublicationError` on the stored error dict | pass | fixed by the 8c.2 null-plan work |
| `f6-order-items-15` | **fail** — verified fix mis-routed to ISSUE by `budget_exceeded` | pass | 9 tools in run 3, so the route bug stayed latent |
| `f1-orders-status` | pass | **fail** — wall-clock | new |
| `f2-payments-amount` | pass | **fail** — `UPSTREAM_NULLS_INTRODUCED` | same column-removed ambiguity as run-2 `f2-customers-email` |
| `f6-orders-03` | pass | **fail** — bad `old` anchor | new |
| `f6-order-items-05` | pass | **fail** — bad `old` anchor ×3 | new |
| `f6-order-items-03` | pass | **fail** — dbt child native crash | new, infrastructure |

So: **one scenario failed in both runs** (`f2-customers-email`, for unrelated
reasons), **three failed only in run 2** and now pass, and **five failed only in
run 3**. The run-3 additions are one budget overrun, one fixture-ambiguity
misdiagnosis, two fixer formatting failures, and one native dbt crash. Controls
were clean in both runs.

## 6. Confidence vs correctness

“Fully correct” means detected, category, columns where applicable, resolution,
and a valid delivered fix for PR-expected scenarios. `confidence` is the stored
`RootCause.confidence`; 37 fault scenarios partition into the three buckets.

| confidence | n | fully correct | rate | category correct | rate |
|---|---:|---:|---:|---:|---:|
| 0.0–0.5 | 1 | 0 | 0% | 0/1 | 0% |
| 0.5–0.8 | 5 | 4 | 80% | 5/5 | 100% |
| 0.8–1.0 | 31 | 27 | 87% | 30/31 | 97% |

The failures by confidence: `f2-customers-email` 0.00; `f6-orders-03` 0.78; then
`f1-orders-status` 0.95, `f2-payments-amount` 0.90, `f6-order-items-05` 0.88, and
`f6-order-items-03` 0.92. Calibration is broadly sensible — the one `0.00` is
the empty investigation and the one low-bucket miss is the F6 anchor — but the
top bucket is overconfident: four of its 31 scenarios are not fully correct, and
three of those four are *not* diagnosis errors (they are budget, fixer format,
and infrastructure). Confidence measures the diagnosis, not the delivery path,
so it should not be read as an end-to-end success probability.

## 7. Tokens and time per fault kind

Per-scenario usage from the results file (mean/median; controls excluded):

| kind | n | tokens mean | tokens median | wall mean | wall median |
|---|---:|---:|---:|---:|---:|
| `column_added` | 4 | 24,033 | 23,916 | 36.4 s | 36.5 s |
| `column_removed` | 4 | 16,445 | 17,709 | 130.6 s | 51.3 s |
| `column_rename` | 4 | 27,874 | 30,474 | 203.1 s | 187.9 s |
| `duplicate_rows` | 6 | 39,994 | 40,910 | 74.6 s | 70.8 s |
| `late_batch` | 4 | 33,142 | 31,277 | 66.2 s | 38.2 s |
| `null_spike` | 7 | 25,962 | 26,309 | 61.1 s | 53.6 s |
| `orphan_keys` | 5 | 40,317 | 38,400 | 112.9 s | 55.6 s |
| `type_change` | 3 | 25,469 | 26,787 | 87.8 s | 67.6 s |
| `control` | 6 | 0 | 0 | 22.7 s | 22.7 s |

The means are pulled up by individual outliers (`f1-orders-status` 331 s inside
`column_rename`, `f2-customers-email` 380 s inside `column_removed`,
`f5-payments-order-35` 256.7 s inside `orphan_keys`). Duplicate-row and
orphan-key scenarios are the most token-hungry because they need several SQL
probes and, for duplicates, a fix attempt. Controls spend no model tokens (they
stop at the monitor).

## 8. Harness-level findings (what Stage 2 addresses)

1. **The dbt compile failure was invisible and unretried.** A native CPython
   crash left only three header lines; `LineageError` is not infrastructure by
   classification. Stage 2 keeps the last 30 lines + exit code, retries once,
   and records a persistent failure as `infra_error` (outside a benchmark it
   stays an explicit error).
2. **The fixer ceiling is too low for reasoning models.** The only cut-off in
   this run burned 8,192 completion tokens, 4,132 of them reasoning, and got no
   plan. Stage 2 raises `FIXER_MAX_OUTPUT_TOKENS` to 16,384, records
   `finish_reason`/reasoning tokens on failed calls too, and adds code-owned
   feedback for the retry (“your reply was cut off; return only a short plan
   with minimal edits”).
3. **The 6,000-token prompt warning is wrong for high-TPM runs.** Run 3 emitted
   it for 8 scenarios while using `--tpm-limit 200000`; those calls are within
   the configured rate. Stage 2 makes the threshold `PROMPT_TOKEN_WARN`, with a
   24,000 default when the TPM limit is above 10,000 and 6,000 otherwise.
4. **Wall-clock is not enforceable inside a provider call.** `f2-customers-email`
   spent 353.5 s in one call; the budget only noticed on return. This is a
   pre-existing limitation, not changed here.
5. **The column-removal fixture is ambiguous.** The physical column remains and
   is NULL, so SQL evidence contradicts the header-only check. Two runs have now
   misdiagnosed it (`f2-customers-email` in run 2, `f2-payments-amount` in run
   3). It is a fixture/tool-presentation issue, not a verifier or fixer-context
   flaw; it stays out of scope and is recorded in DECISIONS.
6. **`route_after_verification` still checks `budget_exceeded` before
   `verification_passed`.** It did not cost a PR in run 3 (`f6-order-items-15`
   used 9 tools), but it remains latent. Not changed here.

## 9. Bottom line

- The seven report lines are six scenarios. Two are diagnosis misses
  (`f2-customers-email` timed out empty; `f2-payments-amount` followed the
  fixture's NULL evidence), one is a wall-clock casualty (`f1`), two are fixer
  anchor-formatting failures (`f6-orders-03`, `f6-order-items-05`), and one is a
  native dbt/CPython crash (`f6-order-items-03`).
- **No verifier flaw and no fixer-context flaw were found.** The exact-match
  rejections are correct; the candidate models were the right files and their
  SQL was supplied verbatim. The F6 failures are fixer capability, so Stage 2
  may proceed without touching the prompt, verifier, scorer, scenarios, oracle or
  injectors.
- The Stage 2 changes are evidence-driven: the crash needs capture + retry +
  infra classification; the cut-off needs a larger ceiling and explicit
  feedback; the TPM warning needs to follow the configured rate.
