# Phase 8a — fast reset, benchmark catalogue, deterministic scorer and runner

Date: 2026-10-23 · Scope: Phase 8 tasks 0–3 (backend). This phase builds the
fast warehouse reset, the investigator harness fixes, the benchmark catalogue,
the deterministic scorer, and the quota-aware runner/CLI/report. **No real
benchmark was run**: the `dev`/`holdout` measurement is Phase 8b, and the
Streamlit page is Phase 7 (not started). Companion review packet:
`docs/review/phase-8a-review.md`.

## What was built

**Fast reset (Task 0)**

* `src/dqs/sim/loader.py` — batch loading uses PostgreSQL `COPY` in one
  transaction instead of per-row `INSERT`; the schema evolution and `dq` records
  are unchanged. Same rows, same order, same bytes.
* `src/dqs/sim/pipeline.py` — `seed_baseline` loads all baseline days first and
  runs **one** dbt build at the end. `sim seed` and `demo` use it; the old path
  ran a whole-warehouse build after every day.
* `src/dqs/fast_reset.py` — the cached clean baseline:
  * `dqs_template` is built once (30 days, seed 42, `COPY` + one dbt build) and
    records `dq.template_meta` (content hash, seed, day count, timings, row
    counts).
  * `content_hash()` covers the generator, every `contracts/*.yml`, every file
    under the dbt `models/` and `macros/` trees, and `dbt_project.yml`, with
    repository-relative paths. A content change rebuilds automatically; no
    `--force` flag is exposed to consumers.
  * `clone_template()` uses `CREATE DATABASE … TEMPLATE`; demo and every
    scenario get an exact copy. `warehouse`, `postgres`, `template0/1`,
    `dqs_template` and non-`dqs_` names are refused as sources/targets.
* `src/dqs/demo.py` — `dqs demo` always clones a fresh `dqs_demo` from the
  template instead of seeding 30 days itself.
* `tests/unit/test_fast_reset.py` — name guards, demo clone path, and the
  "30 loads + exactly one build" assertion.
* `tests/integration/test_bench_fast_reset.py` — real template build, captured
  checksums, clone identity, hash invalidation, warehouse guard, and three
  fake-LLM end-to-end scenarios.

**Investigator harness (Task 0b)**

* `src/dqs/agents/investigator.py`:
  * registers a `RootCause` **final-answer tool** (same schema as
    `submit_root_cause`) in the normal loop; a model that finishes by calling
    it produces a final answer instead of a provider 400;
  * on an OpenAI-compatible `tool_use_failed`, extracts `failed_generation`,
    parses the observed gateway shapes (bare JSON, `{name, arguments}`,
    `<function=…>` wrapper, embedded object), and uses it only when it
    validates as `RootCause` (no second model call);
  * an invalid payload falls back to the existing final channel exactly once;
  * writes `provider_tool_use_failed` with `path=salvaged|fallback_final_channel`
    to `dq.agent_steps`;
  * accepts `incident`/`incident_record` as evidence provenance (both were
    already read-only sources); uncalled tools are still dropped.
* `src/dqs/agents/llm.py` — a provider **daily** quota (HTTP 429 plus quota
  markers) raises `DailyQuotaExceeded` instead of retrying; a per-minute 429
  keeps the existing bounded backoff.
* `src/dqs/bench/harness.py` — `HARNESS_VERSION="8a.1"` and SHA-256 hashes of the
  investigator/fixer prompts, recorded in every results file. No prompt wording
  changed in this phase.

**Scenario catalogue (Task 1)**

* `src/dqs/bench/scenarios.py` — **53 fault scenarios + 6 clean controls**
  (59 total), stable ids, explicit split, and complete `GroundTruth`
  (expected checks/category/resolution plus `acceptable_columns`). Ground truth
  is derived by applying the real injector to the clean 2026-01-31 batch, so it
  cannot drift from the mutation.
* Variants include null spikes 20/35/60%, duplicates 3/8/15%, scale ×100/×1000;
  F5 uses only the documented `FK_MAP` pairs; F9 covers the four non-dimension
  tables. Split: **43 dev / 16 holdout (30.2% of faults)**; all F7 and F9 plus
  one variant of every other kind are holdout, controls stay dev.
* `catalogue_problems()` is asserted empty by `tests/unit/test_bench_scenarios.py`.

**Deterministic scoring (Task 2)**

* `src/dqs/bench/scoring.py` — `Observation` → `ScenarioRun` → per-kind/overall
  `MetricBlock`; no LLM judge.
* Metrics: detected, root-cause category correct, root-cause column set correct,
  resolution correct, verifier passed, semantically correct (oracle),
  false-positive (controls), `verified_but_wrong`, tool calls, tokens in/out
  (including failed attempts), estimated cost, wall time.
* Denominators are explicit: faults for detection/category/columns/resolution,
  PR-expected faults for verifier/semantic, controls for false positives. Errors
  stay in every denominator as misses.
* `src/dqs/bench/oracle.py` — the clean-data oracle moved out of `tests/helpers`;
  `tests/unit/test_bench_isolation.py` proves agents/verify/workflow/publish/
  tools/checks never import `dqs.bench`.

**Runner, CLI and report (Task 3)**

* `src/dqs/bench/runner.py` — sequential per-scenario pipeline: clone template →
  load 2026-01-31 fault (dbt after load unless `--no-dbt`) → monitor → workflow
  (`auto_approve=True`, `GITHUB_MODE=dry_run`) → score → drop in `finally`.
  Results are rewritten after every scenario; resume skips completed records and
  refuses a model/provider/split mismatch; non-quota errors are recorded, never
  dropped.
* Quotas: local daily token cap (default 180,000) stops *before* the next
  scenario with `stop_reason`/`resume_hint`; a provider daily 429 discards the
  current scenario and stops (resume re-runs it); per-minute 429s use bounded
  retries (total wait default 90 s, recorded); a sliding `TokenThrottle`
  (default 8,000 TPM) spaces scenarios; prompts over 6,000 input tokens warn.
* Holdout protection: `--confirm-holdout` is required; the results metadata
  records whether prompt hashes changed since the newest earlier `dev` file.
* `src/dqs/bench/report.py` — strict result model, `latest.md` with provenance,
  overall/per-kind metrics, medians/p90, and the failing scenarios.
* `src/dqs/config.py` — `LLM_PRICE_TABLE` plus per-million overrides; an unpriced
  model reports 0.0 and `priced_model=false` (a zero is never silently "free").
* `src/dqs/cli.py` — `dqs bench run [--split] [--model] [--limit] [--ids]
  [--resume] [--no-dbt] [--daily-token-cap] [--tpm-limit] [--pause]` and
  `dqs bench report [FILE]`.

## How to run it

```powershell
cd 'D:\study\python\data steward'

# quality gates
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m ruff format --check src tests
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest -m "not integration" -q

# Phase 8a unit acceptance (no DB, no network)
.venv\Scripts\python -m pytest tests\unit\test_fast_reset.py tests\unit\test_bench_scenarios.py tests\unit\test_bench_scoring.py tests\unit\test_bench_report.py tests\unit\test_bench_runner.py tests\unit\test_bench_isolation.py tests\unit\test_investigator.py tests\unit\test_llm.py -q

# real Postgres/dbt acceptance (template, checksums, three fake-LLM scenarios)
.venv\Scripts\python -m pytest tests\integration\test_bench_fast_reset.py -m integration -q -s

# Phase 8b (NOT run in this phase): a real model on the dev split
.venv\Scripts\dqs bench run --split dev --model claude-sonnet-5-5
.venv\Scripts\dqs bench report
# interrupted earlier? resume the same file:
.venv\Scripts\dqs bench run --split dev --model claude-sonnet-5-5 --resume benchmarks\results\<file>.json
```

`dqs bench run` requires approved contracts and `GITHUB_MODE=dry_run`; it
never calls GitHub and never touches the `warehouse` database. The only expected
extra database after a benchmark is `dqs_demo` (the demo database) and the
`dqs_template` cache.

## Acceptance results

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Fast reset measured and recorded | PASS | Old per-day-build path **142.01 s** for 30 days; new split: generate 0.17 s, load 7.44 s, one dbt build 5.41 s. Cold template build (integration, 2026-10-23): **10.30 s total (load 4.48 s, dbt 5.82 s)**; rerun 10.28 s (load 4.84, dbt 5.44); latest module run 10.17 s (load 4.76, dbt 5.41). Row counts: customers 3,161; products 200; orders 13,891; order_items 34,845; payments 14,590. |
| 2 | New reset is byte-identical to the old baseline | PASS | `test_template_is_byte_identical_to_the_captured_baseline` compares per-table md5s and the set hash captured before the refactor: `raw.customers 22781d…`, `raw.products fbdd13…`, `raw.orders 1f4aba…`, `raw.order_items 0a9727…`, `raw.payments b40cc4…`, `dq.batches c73745…`, `dq.batch_tables ccf230…`; set hash `2e3a46…`. |
| 3 | Template cache invalidates on content change | PASS | `test_reused_template_is_not_rebuilt` (same `built_at`) and `test_changed_content_hash_forces_a_rebuild` (monkeypatched hash rebuilds the same bytes). |
| 4 | Warehouse is never touched | PASS | `test_warehouse_is_never_a_clone_source_or_target` refuses `warehouse` as source and target and refuses to drop it; the module's autouse guard compares warehouse checksums before/after and asserts no `dqs_bench_*` database remains. |
| 5 | RootCause finish tool and salvage path | PASS | `tests/unit/test_investigator.py`: `test_root_cause_tool_finishes_in_one_call`, `test_valid_failed_generation_is_salvaged_without_a_second_call`, `test_invalid_failed_generation_falls_back_to_the_final_channel_once`, `test_failed_generation_parser_accepts_gateway_shapes`, `test_incident_record_is_valid_evidence_provenance`, `test_root_cause_tool_evidence_for_uncalled_tools_is_still_dropped`. |
| 6 | Daily quota is not retried | PASS | `tests/unit/test_llm.py::test_daily_quota_429_stops_immediately_without_retry`, `test_is_daily_quota_error_distinguishes_rate_limits`, `test_daily_quota_marker_is_not_retried_even_with_wait_room`; runner test `test_daily_quota_discards_the_scenario_and_stops`. |
| 7 | ≥40 faults + 6 controls, complete split | PASS | 53 faults + 6 controls; `catalogue_problems() == []`; counts and per-kind holdout asserted in `tests/unit/test_bench_scenarios.py` (10 tests). |
| 8 | Scoring has negative cases and explicit denominators | PASS | `tests/unit/test_bench_scoring.py` (11 tests): wrong category, right category/wrong column, false positive on a control, error counted as a miss, F6/F8/F9 column N/A, table-prefix normalization, `verified_but_wrong`. |
| 9 | Runner writes incrementally, resumes, protects holdout and quotas | PASS | `tests/unit/test_bench_runner.py` (17 tests): write after every scenario; resume skips completed; model/split mismatch refused; holdout confirmation + prompt-change provenance; daily cap; daily-429 discard; non-quota errors kept; live GitHub refused; missing contracts refused; DB dropped even when the load fails; `--no-dbt` passed through. |
| 10 | Three fake-LLM scenarios end to end | PASS | `test_three_fake_llm_scenarios_run_end_to_end_and_leave_no_databases`: F1/F4/F6, all detected, non-zero tokens, one JSON + `latest.md`, no scenario database left (only the module template). Wall times in the latest run: F1 30.4 s, F4 22.7 s, F6 26.8 s (fake model, so no network latency). |
| 11 | Quality gates | PASS | `ruff check` clean, `ruff format --check` clean (164 files), `mypy src` clean (99 source files). Unit **626 passed, 1 skipped** (627 collected, 104 integration deselected). Integration **98 passed, 6 skipped in 471 s** (the 6 real-LLM tests are skipped by marker). |
| 12 | No leftover benchmark databases or sandbox schemas | PASS | After the full suite, `fast_reset.list_steward_databases` returns only `dqs_demo`; `dqs verify cleanup` prints `ok no sandbox schemas remained`; the warehouse checksums equal the captured baseline and it still has 30 batches, 0 incidents, 0 steps, 0 fix attempts. |

**Not in this phase:** the Phase 8 acceptance row "real run on `dev` with
`LLM_MODEL_BENCH`" is Phase 8b and was not run (owner scope). No benchmark
numbers are claimed in this report; the token projections in the review packet
are labelled prior single-scenario evidence.

## Design choices and why

DECISIONS entries 120–127 (all made in this phase):

* **120** — 30 loads then one dbt build; bulk `COPY`; byte-identical checksums.
* **121** — content-addressed `dqs_template`, cloned by demo/bench; the
  warehouse is never a source or target.
* **122** — catalogue is code, ground truth is derived by the injector, 53+6
  with a 30.2% holdout split.
* **123** — explicit scoring denominators, no LLM judge, oracle moved to
  `dqs.bench.oracle`, isolation test.
* **124** — sequential quota-aware runner, incremental writes, resume rules,
  per-minute vs daily 429 handling, TPM throttle, prompt-size warning.
* **125** — cost from a price table; unknown model → 0.0 + `priced_model=false`.
* **126** — `RootCause` finish tool + `failed_generation` salvage + provenance;
  frozen harness `8a.1` and prompt hashes.
* **127** — holdout is reporting-only; provenance is explicit (model/provider/
  date/git SHA/prompt hashes/settings snapshot minus secrets).

## Concepts used

1. **Copy-on-write database clones.** `CREATE DATABASE … TEMPLATE` copies a
   database at the file level and then gives each database its own pages. It is
   far cheaper than replaying 30 days of generation, loading, and dbt, and it
   guarantees every scenario starts from exactly the same bytes.
2. **Content-addressed caching.** A cache key derived from the *inputs*
   (generator, contracts, dbt models/macros) is more reliable than a manual
   "rebuild?" flag: if any input changes, the hash changes and the template
   rebuilds; if nothing changed, the existing template is reused. Keeping the
   key inside the artifact (`dq.template_meta`) also survives a process restart.
3. **Bulk `COPY` vs row inserts.** PostgreSQL's `COPY` protocol streams rows in
   a single round trip per table instead of one `INSERT` per row. The data and
   constraints are the same; the difference is network/statement overhead, which
   is why the load dropped from tens of seconds to a few seconds.
4. **Answer-key scoring with explicit denominators.** A benchmark is only
   meaningful if "correct" is defined before the run and the denominator is
   fixed. Here the answer key is generated by applying the same injector used to
   create the fault, and errors stay in the denominator as misses; otherwise a
   runner that failed half the scenarios could look better, not worse.
5. **Rate limit vs quota.** Both often arrive as HTTP 429. A rate limit means
   "wait a little" (retry with backoff); an exhausted daily quota means "stop for
   today" (retry burns budget and still fails). The code separates them by
   status plus provider text, and the runner records/discards accordingly.
6. **Tool-call protocols and salvage.** Function/tool calling is a provider
   protocol: the model emits a tool name and JSON arguments. Some gateways reject
   a malformed final tool call with a 400 but include the attempted JSON in
   `failed_generation`. Validating that payload is cheaper and more faithful than
   discarding it and paying for another call.

## Deviations from the docs

* The Phase 8 acceptance step "real run on `dev` with `LLM_MODEL_BENCH`" is
  deferred to Phase 8b by the owner; this phase verifies the pipeline with the
  fake model only. This is not a benchmark result.
* The Phase 8 task list mentions a UI page; Phase 7 (Streamlit) has not started,
  so no UI work was done in 8a.
* `--repeats` (mentioned in `ARCHITECTURE.md` §14/§15 as future variance work)
  is not implemented.
* `HANDOFF.md` previously said the 30-day seed took "~18 minutes"; measured on
  this host it was **142.01 s** (30 builds), so the stale number was corrected.

## Known limitations / TODO

* **No real-model run yet.** All Phase 8a end-to-end evidence uses
  `FakeChatModel`; it proves wiring, scoring, cleanup and resume, not diagnostic
  quality.
* **Template hash scope is exactly the documented inputs.** It does not cover
  `sim/loader.py`, `contracts/parsing.py`, or the SQL guard. A change there could
  in principle change loaded bytes without invalidating the cache; drop
  `dqs_template` manually if you change the loader (the hash contract is
  "generator + contracts + dbt content").
* **Quota detection is provider-text based.** The markers cover the observed
  OpenAI-compatible/Anthropic/Google messages, but a provider with different
  wording will be treated as a normal 429 and retried until the retry budget is
  exhausted.
* **A daily quota discards the in-flight scenario.** Its partial tokens are
  recorded in the stopped run but the scenario is not scored, so a resumed run
  pays for it again. This is deliberate (a partial investigation is not a
  result), but it is a real cost.
* **One run per scenario, no repeats.** Percentages have no confidence interval;
  a single flaky scenario moves the aggregate. `--repeats` is future work.
* **Sequential only.** Wall-clock totals are meaningful, but a 53-scenario dev
  run is long; parallel scenarios were rejected because they would contend for
  the same rate limit and distort cost/time.
* **Holdout provenance is file-based.** The runner compares prompt hashes with
  the newest earlier `dev` JSON; it cannot detect a changed tool implementation
  or scoring rule. `HARNESS_VERSION` must be bumped manually when the plumbing
  changes.
* **Controls consume wall time but no LLM tokens.** They still clone, load, run
  dbt and monitor; the daily-cap projection therefore counts only fault
  scenarios.
* **A hard process kill can leave one `dqs_bench_*` database.** The runner drops
  it in `finally` for normal exceptions; after a native crash the next run (or a
  manual drop) is needed. The integration teardown asserts none remain.
