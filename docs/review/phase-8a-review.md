# Phase 8a review packet

Date: 2026-10-23 · Scope: Phase 8 tasks 0–3 (backend) — fast warehouse reset,
investigator harness fixes, the 53-fault + 6-control catalogue, deterministic
scoring, and the quota-aware runner/CLI/report. **No real benchmark was run**
(Phase 8b); the Streamlit page (Phase 7) is not started. Companion:
`docs/phase-notes/phase-8a.md`.

## What to review first

1. `src/dqs/fast_reset.py` — the content hash, `dq.template_meta`, the clone/
   drop name guards, and the "warehouse is never a source or target" rule.
2. `src/dqs/sim/loader.py` + `src/dqs/sim/pipeline.py` — bulk `COPY` and
   "30 loads, one dbt build"; the byte-identity test is the contract.
3. `src/dqs/agents/investigator.py` — the `RootCause` finish tool, the
   `failed_generation` parser/salvage order, and `INCIDENT_PROVENANCE_TOOLS`.
4. `src/dqs/bench/scenarios.py` — the split table and `catalogue_problems()`;
   ground truth is derived by the injector, not hand-written.
5. `src/dqs/bench/scoring.py` — denominators, error-as-miss, `verified_but_wrong`,
   and the `None` column score for F6/F8/F9.
6. `src/dqs/bench/runner.py` — clone → fault → monitor → workflow → score →
   drop; incremental writes, resume refusals, daily vs per-minute 429 handling,
   and the holdout confirmation.
7. `src/dqs/bench/report.py` + `src/dqs/cli.py` — the strict results model,
   `latest.md`, and `dqs bench run|report`.
8. `tests/unit/test_bench_runner.py` — the acceptance-style quota/resume/cleanup
   tests; `tests/integration/test_bench_fast_reset.py` — the real template,
   checksums, and three fake-LLM scenarios.

## Verdict table

| Area | Verdict | Evidence |
|---|---|---|
| 30-day reset is fast | PASS | 142.01 s → **10.17–10.30 s** cold template; clone **0.056–0.237 s**; details below |
| Reset is byte-identical | PASS | per-table md5s + set hash match the pre-refactor capture; `test_template_is_byte_identical_to_the_captured_baseline` |
| Cache invalidates by content | PASS | reuse test (`created=False`) and forced-hash rebuild test |
| Warehouse isolation | PASS | clone/drop refusal tests + autouse checksum guard |
| RootCause finish tool | PASS | `test_root_cause_tool_finishes_in_one_call` |
| `failed_generation` salvage | PASS | valid payload used with no second call; invalid payload falls back once; gateway shapes parsed |
| Evidence provenance | PASS | uncalled-tool citations dropped; `incident`/`incident_record` accepted |
| Daily quota is not retried | PASS | `DailyQuotaExceeded` tests + runner discard/stop test |
| Catalogue size and split | PASS | 53 faults + 6 controls; 16 holdout (30.2%); all F7/F9 + one of each other kind |
| Ground truth completeness | PASS | every fault has expected checks/category/resolution/columns; `catalogue_problems() == []` |
| Scoring negative cases | PASS | wrong category, wrong column set, control false positive, error-as-miss, `verified_but_wrong` |
| Incremental results / resume | PASS | JSON rewritten after each scenario; resume skips completed, refuses model/split/provider mismatch |
| Holdout protection | PASS | `--confirm-holdout`; prompt-hash comparison with the newest dev file |
| Daily token cap | PASS | stops before the next scenario with `stop_reason` + `--resume` hint |
| Daily 429 discard | PASS | scenario discarded (not scored), run stops, resume re-runs it |
| Per-minute 429 | PASS | bounded retry with recorded max wait; TPM throttle |
| Non-quota errors | PASS | recorded in the result file, never dropped |
| Cost estimate | PASS | price table + overrides; unpriced model → 0.0 + `priced_model=false` |
| Fake-LLM end-to-end | PASS | F1/F4/F6 detected, JSON + `latest.md`, no scenario DB left |
| Quality gates | PASS | ruff check + format clean (164 files); mypy clean (99 source files); unit **626 passed, 1 skipped**; integration **98 passed, 6 skipped in 471 s** (6 real-LLM skipped) |
| No leftover databases/schemas | PASS | only `dqs_demo` (and the transient test template during the run); no `dqs_bench_*`, no `dqs_sandbox_*` |

**Not claimed:** diagnostic quality, cost, or accuracy of a real model. The real
`dev` run is Phase 8b and needs `--model` with a funded key; the fake-model
end-to-end run proves plumbing only.

## Reset timings (real, this host)

Captured 2026-10-23 on the Docker Postgres 16 instance (host port 5433) and the
installed dbt 1.12.

| Step | Old path | New path |
|---|---|---|
| 30-day seed | **142.01 s** (generate + load + full dbt build per day) | **10.17–10.30 s** cold template (all loads, then one build) |
| Generate 30 days | included above | 0.17 s |
| Load 30 days (`COPY`) | included above | 4.48–4.84 s |
| dbt build (once) | 30 builds inside the 142 s | 5.41–5.82 s |
| Per-scenario reset | re-seed (minutes) | `CREATE DATABASE … TEMPLATE dqs_demo` clone: **0.056–0.237 s** (three measured runs of a 31-day DB; a 30-day template is the same order) |

Two independent cold builds: 10.30 s (load 4.48, dbt 5.82) and 10.28 s (load
4.84, dbt 5.44); the integration module's latest run measured 10.17 s (load 4.76,
dbt 5.41). Row counts are stable: customers 3,161; products 200; orders 13,891;
order_items 34,845; payments 14,590.

The per-table checksums used as the identity contract are in the phase note
(`raw.customers 22781d…`, set hash `2e3a46…`). The old "~18 min" number in
`HANDOFF.md` was stale and is corrected to 142.01 s.

## Catalogue (59 cases)

Structural summary: **53 faults + 6 controls**, **43 dev / 16 holdout**,
holdout share **30.2%**. All F7 (scale) and F9 (missing table) are holdout, plus
one variant of every other kind. Controls are all dev. Variants required by the
plan are present: null 20/35/60%, duplicate 3/8/15%, scale ×100/×1000.

| id | kind | split | fault spec | acceptable columns |
|---|---|---|---|---|
| `f1-orders-customer-id` | column_rename | holdout | `column_rename:orders.customer_id:new_name=cust_id` | customer_id, cust_id |
| `f1-orders-status` | column_rename | dev | `column_rename:orders.status:new_name=order_status` | status, order_status |
| `f1-payments-method` | column_rename | dev | `column_rename:payments.method:new_name=payment_method` | method, payment_method |
| `f1-customers-email` | column_rename | dev | `column_rename:customers.email:new_name=email_address` | email, email_address |
| `f1-order-items-quantity` | column_rename | dev | `column_rename:order_items.quantity:new_name=qty` | quantity, qty |
| `f2-orders-status` | column_removed | holdout | `column_removed:orders.status` | status |
| `f2-customers-email` | column_removed | dev | `column_removed:customers.email` | email |
| `f2-payments-amount` | column_removed | dev | `column_removed:payments.amount` | amount |
| `f2-order-items-quantity` | column_removed | dev | `column_removed:order_items.quantity` | quantity |
| `f2-order-items-unit-price` | column_removed | dev | `column_removed:order_items.unit_price` | unit_price |
| `f3-orders-total-amount` | type_change | holdout | `type_change:orders.total_amount:fraction=1.0` | total_amount |
| `f3-payments-amount` | type_change | dev | `type_change:payments.amount:fraction=0.8` | amount |
| `f3-order-items-unit-price` | type_change | dev | `type_change:order_items.unit_price:fraction=0.5` | unit_price |
| `f3-order-items-quantity` | type_change | dev | `type_change:order_items.quantity:fraction=1.0` | quantity |
| `f4-orders-status-20` | null_spike | dev | `null_spike:orders.status:fraction=0.2` | status |
| `f4-orders-status-35` | null_spike | holdout | `null_spike:orders.status:fraction=0.35` | status |
| `f4-customers-email-60` | null_spike | dev | `null_spike:customers.email:fraction=0.6` | email |
| `f4-customers-state-35` | null_spike | dev | `null_spike:customers.state:fraction=0.35` | state |
| `f4-payments-method-20` | null_spike | dev | `null_spike:payments.method:fraction=0.2` | method |
| `f4-order-items-quantity-60` | null_spike | dev | `null_spike:order_items.quantity:fraction=0.6` | quantity |
| `f4-orders-customer-20` | null_spike | dev | `null_spike:orders.customer_id:fraction=0.2` | customer_id |
| `f4-order-items-unit-price-35` | null_spike | dev | `null_spike:order_items.unit_price:fraction=0.35` | unit_price |
| `f5-orders-customer-35` | orphan_keys | dev | `orphan_keys:orders.customer_id:fraction=0.35` | customer_id |
| `f5-orders-customer-15` | orphan_keys | holdout | `orphan_keys:orders.customer_id:fraction=0.15` | customer_id |
| `f5-order-items-order-35` | orphan_keys | dev | `orphan_keys:order_items.order_id:fraction=0.35` | order_id |
| `f5-payments-order-20` | orphan_keys | dev | `orphan_keys:payments.order_id:fraction=0.2` | order_id |
| `f5-payments-order-35` | orphan_keys | dev | `orphan_keys:payments.order_id:fraction=0.35` | order_id |
| `f5-order-items-product-35` | orphan_keys | dev | `orphan_keys:order_items.product_id:fraction=0.35` | product_id |
| `f6-orders-03` | duplicate_rows | dev | `duplicate_rows:orders:fraction=0.03` | — |
| `f6-orders-08` | duplicate_rows | holdout | `duplicate_rows:orders:fraction=0.08` | — |
| `f6-order-items-05` | duplicate_rows | dev | `duplicate_rows:order_items:fraction=0.05` | — |
| `f6-order-items-03` | duplicate_rows | dev | `duplicate_rows:order_items:fraction=0.03` | — |
| `f6-payments-15` | duplicate_rows | dev | `duplicate_rows:payments:fraction=0.15` | — |
| `f6-customers-08` | duplicate_rows | dev | `duplicate_rows:customers:fraction=0.08` | — |
| `f6-order-items-15` | duplicate_rows | dev | `duplicate_rows:order_items:fraction=0.15` | — |
| `f7-orders-total-100` | scale_shift | holdout | `scale_shift:orders.total_amount:factor=100` | total_amount |
| `f7-orders-total-1000` | scale_shift | holdout | `scale_shift:orders.total_amount:factor=1000` | total_amount |
| `f7-payments-amount-100` | scale_shift | holdout | `scale_shift:payments.amount:factor=100` | amount |
| `f7-order-items-unit-price-1000` | scale_shift | holdout | `scale_shift:order_items.unit_price:factor=1000` | unit_price |
| `f8-orders-24` | late_batch | holdout | `late_batch:orders:hours=24` | — |
| `f8-customers-30` | late_batch | dev | `late_batch:customers:hours=30` | — |
| `f8-payments-48` | late_batch | dev | `late_batch:payments:hours=48` | — |
| `f8-order-items-24` | late_batch | dev | `late_batch:order_items:hours=24` | — |
| `f8-order-items-30` | late_batch | dev | `late_batch:order_items:hours=30` | — |
| `f9-customers` | missing_table | holdout | `missing_table:customers` | — |
| `f9-orders` | missing_table | holdout | `missing_table:orders` | — |
| `f9-order-items` | missing_table | holdout | `missing_table:order_items` | — |
| `f9-payments` | missing_table | holdout | `missing_table:payments` | — |
| `f10-orders-coupon` | column_added | holdout | `column_added:orders:new_column=coupon_code` | coupon_code |
| `f10-customers-note` | column_added | dev | `column_added:customers:new_column=extra_note` | extra_note |
| `f10-payments-campaign` | column_added | dev | `column_added:payments:new_column=campaign_id` | campaign_id |
| `f10-order-items-note` | column_added | dev | `column_added:order_items:new_column=extra_note` | extra_note |
| `f10-orders-source` | column_added | dev | `column_added:orders:new_column=source_system` | source_system |
| `clean-01` … `clean-06` | clean | dev | — (no fault) | — |

Per-kind counts: F1 5, F2 5, F3 4, F4 8, F5 6, F6 7, F7 4, F8 5, F9 4, F10 5,
controls 6. Per-kind holdout: F7 4, F9 4, and one each of F1–F6/F8/F10.

## Three weakest points

1. **No real-model evidence in this phase.** Every end-to-end number here comes
   from `FakeChatModel`; it proves wiring, scoring, cleanup and resume, but says
   nothing about whether the investigator can diagnose 53 scenarios. The only
   real token/cost estimates are three prior Phase-5b runs (below), not a
   benchmark.
2. **Provenance is partly manual.** Holdout safety compares prompt-file hashes
   with the newest `dev` file; it cannot detect a change to a tool, the scoring
   code, or the injector. `HARNESS_VERSION` is bumped by hand. With one run per
   scenario there is no variance estimate, so a single flaky scenario moves the
   aggregate.
3. **Quota handling is heuristic and loses the in-flight scenario.** A daily 429
   is recognised by status + provider-text markers; different wording would be
   retried as a transient rate limit. When a daily quota is recognised, the
   scenario is discarded (deliberately — a partial investigation is not a
   result), so the resumed run pays for it again. The local daily cap stops
   before a scenario, which avoids that cost but makes the cap a session limit,
   not a hard budget.

## Token evidence and projections (prior real runs, not Phase 8a)

These are the only real-model totals available in the repo. They are single
runs from Phase 4b/5b (`openai/gpt-oss-120b`, 2026-10-20); they are **not** a
benchmark and were produced before the Phase 8a harness (`8a.1`) with the same
prompt wording. Do not quote them as Phase 8a results (AGENTS §7.9).

| Fault kind | Evidence | Tokens | Scope |
|---|---|---:|---|
| `column_rename` (F1) | `out/phase-5b-llm-F1_column_rename.json` | 27,524 | investigator 20,073 + fixer 7,451, 2 attempts |
| `type_change` (F3) | `out/phase-5b-llm-F3_type_change.json` | 18,060 | investigator 15,141 + fixer 2,919, 1 attempt |
| `duplicate_rows` (F6) | `out/phase-5b-llm-F6_duplicate_rows.json` | 22,815 | investigator 18,330 + fixer 4,485, 2 attempts (`gpt-oss-20b`) |
| `null_spike` (F4) | `out/phase-4b-llm-smoke-F4_null_spike.json` | 10,827 | investigator only; **not comparable** to the full chain |
| Full-chain average (3 points) | F1/F3/F6 | **≈22,800** | mean of the three full-chain totals |

Projection at a **200,000-token/day** cap, using 22,800 tokens/scenario and
counting fault scenarios only (controls make no LLM calls):

| Split | Faults | Tokens | Arithmetic days | Cap-limited sessions |
|---|---:|---:|---:|---:|
| `dev` | 37 | 843,600 | 4.2 | 5 |
| `holdout` | 16 | 364,800 | 1.8 | 2 |
| all faults | 53 | 1,208,400 | 6.0 | 7 |

Caveats: the three totals are single runs with no retries/failures and no
repeats; the average hides the F4 investigator-only point; the 200K cap is a
local policy, not a provider guarantee. Treat 4.2/1.8 days as an order-of-
magnitude planning number, not a measurement. The `dev` 43-scenario run also
spends wall time on six controls (clone, load, dbt, monitor) with zero LLM
tokens.

## What the reviewer should poke at

* **Byte-identity scope.** The checksums cover raw business tables and
  `dq.batches`/`dq.batch_tables`. They do not cover every staging/mart row; the
  template's dbt build is the same command as before, so this is an intentional
  boundary, not a claim that all marts were diffed.
* **Template hash scope.** It covers generator + contracts + dbt content, as
  specified. A change to `sim/loader.py` or `contracts/parsing.py` would not
  invalidate the template; drop `dqs_template` manually after changing the
  loader.
* **Holdout discipline.** The implementation enforces the flag and records
  prompt changes, but the process is still human: do not tune prompts after
  looking at holdout results. `dev` and `holdout` results are separate files by
  construction, but nothing prevents a human from reading both.
* **Scoring semantics.** `detected` requires a violation on the expected table
  with one of the expected check names; a model that reports the right category
  via a different check is a detection miss by design. `rc_columns_correct` is
  `None` for F6/F8/F9 because those faults are not column-scoped.
* **Resume is per scenario.** A scenario that finished with a wrong answer is
  "complete" and will not be retried; only `error is None` records are skipped.
  Delete the record (or use a new file) to re-run one deliberately.
