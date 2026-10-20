# Phase 5b — Fixer agent, deterministic issue builder and `plan-fix`

Date: 2026-10-20 · Phase 5 tasks 2 and 3 plus the Phase-5 acceptance LLM runs
(the deterministic verification work is Phase 5a). Companion review packet:
`docs/review/phase-5b-review.md`. No workflow graph or publishing code is in
this phase; those remain Phase 6.

## What was built

* `src/dqs/resolve.py` — pure `plan_resolution(category, confidence,
  has_candidate_model)`: PR only for the three fixable categories at
  confidence >= 0.6 **and** a candidate staging model; `SOURCE_COLUMN_ADDED`
  always NONE; everything else ISSUE.
* `src/dqs/agents/fixer.py` — bounded fixer loop. Builds the context from code
  (root cause, impact, lineage-selected candidate staging SQL and published
  staging contracts), asks for a `FixPlanDraft` through native structured
  output, assembles the code-owned `FixPlan`, runs the Phase-5a verifier,
  feeds failed criteria + the redacted dbt excerpt into the next attempt, and
  persists every attempt to `dq.fix_attempts` and every call to
  `dq.agent_steps`. `MAX_FIX_ATTEMPTS` bounds the loop.
* `src/dqs/agents/prompts/fixer.md` — minimal-change rules: preserve output
  names/types, never filter/delete rows to hide the problem, only
  `models/**/*.sql`, exact-match edits, works for all raw history, context is
  data not instructions, no fault names or injection details.
* `src/dqs/agents/issue_builder.py` — deterministic Markdown issue: title,
  incident summary, violations, root-cause evidence, impact (models,
  exposures, owners, failing nodes), every failed attempt with the exact failed
  criteria/dbt excerpt, a static per-category next-steps playbook, and artifact
  references/links. No LLM.
* `src/dqs/verify/sandbox.py` — added `next_attempt_number` and
  `persist_attempt_payload` so invalid drafts and real verifier attempts share
  one sequential numbering path.
* `src/dqs/cli.py` — `dqs incident plan-fix <id>`: loads the stored root cause
  and impact, prints deterministic routing, refuses non-PR routes, runs the fix
  loop, prints attempts, criteria, tokens/retries, the passing plan JSON, and
  exits 1 on exhaustion.
* `tests/helpers/oracle.py` — **test-only** clean-data oracle. Rebuilds the
  patched staging model with the production patch/build helpers and compares
  the fault batch with `generator.tables_for_date(fault_date, seed)`. A test
  proves no `src/dqs` module imports it.
* Tests: `tests/unit/test_resolve.py`, `tests/unit/test_fixer.py`,
  `tests/unit/test_issue_builder.py`, `tests/unit/test_cli_plan_fix.py`,
  `tests/unit/test_oracle.py`, oracle cases in
  `tests/integration/test_verify_sandbox_db.py`, and
  `tests/integration/test_fixer_llm.py`.

## How to run it

```powershell
cd 'D:\study\python\data steward'

# quality gates
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest -m "not integration" -q
.venv\Scripts\python -m pytest -m "integration and not llm" -q

# deterministic oracle acceptance (real dbt on F1/F3/F6 faulted clones)
.venv\Scripts\python -m pytest tests\integration\test_verify_sandbox_db.py -m integration -q -k "oracle"

# real-LLM chain: investigator -> fixer -> verifier -> oracle
$env:RUN_LLM_TESTS='1'
.venv\Scripts\python -m pytest tests\integration\test_fixer_llm.py -m llm -s -q
# optional model override for a provider/model with remaining quota:
#   $env:LLM_MODEL='openai/gpt-oss-20b'

# manual plan-fix against a clone that holds an investigated incident
.venv\Scripts\dqs incident plan-fix <incident-uuid>
.venv\Scripts\dqs incident plan-fix <incident-uuid> --provider fake --model fake-1
```

`plan-fix` writes attempts and steps; point it at a clone, not the dev
warehouse. The real-LLM test seeds its own template and drops every clone.

## Acceptance results

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Fixer has no tools; context assembled by code | PASS | `test_fixer.py` asserts the prompt has no tools, `FixPlanDraft` has only the five §10.5 fields, and `build_fix_plan` takes incident/category/table/columns from code. |
| 2 | Candidate staging models come from lineage | PASS | `test_candidate_selection_uses_direct_raw_lineage` returns only `stg_orders` for `raw.orders`; unrelated/non-staging models are excluded. |
| 3 | Retry feedback and `MAX_FIX_ATTEMPTS` | PASS | `test_retry_receives_failed_criteria_and_redacted_dbt_excerpt`; `test_max_attempts_stops_and_returns_every_attempt` stops at the configured limit and returns every attempt. |
| 4 | Every attempt persisted; invalid output included | PASS | Unit tests cover an invalid structured response and a real verifier allowlist rejection; both are persisted. Real runs show `persisted_attempts == attempts`. |
| 5 | Routing table for all ten categories | PASS | `tests/unit/test_resolve.py` — **23 passed**; boundary exactly 0.6, no candidate forces ISSUE, added-column always NONE. |
| 6 | Deterministic issue builder, all required sections | PASS | `tests/unit/test_issue_builder.py` — **23 passed**; title, summary, evidence, impact, owners, failing nodes, attempts, playbook and artifacts asserted; same inputs produce identical Markdown. |
| 7 | Static category next steps | PASS | Parameterized over all ten `RootCauseCategory` values; a duplicate-specific test proves the text is category-specific, not generic. |
| 8 | Exhausted fix is reported honestly | PASS | The issue renders each attempt's failed criteria and the redacted dbt excerpt; a no-attempt issue says so explicitly. |
| 9 | `dqs incident plan-fix` surface | PASS | `tests/unit/test_cli_plan_fix.py` — **4 passed**: non-PR refusal, missing root cause, passing plan/attempt output, invalid uuid. |
| 10 | Test-only oracle + production isolation | PASS | `tests/unit/test_oracle.py` — **13 passed**, including a scan asserting no `src/dqs` file mentions/imports the oracle; unsafe identifiers rejected before DB access. |
| 11 | Oracle agrees with real good patches on F1/F3/F6 | PASS | `pytest ... -k oracle` → **4 passed in 42.94s** (three good patches plus one verifier-pass/wrong-values case). |
| 12 | Oracle catches a verifier pass with wrong values | PASS | `coalesce(customer_id, 1)` clears nulls/contract/rows but the oracle reports mismatched `customer_id` cells. This is asserted, not hidden. |
| 13 | Real-LLM F1 (120b) | PASS | 2 attempts; attempt 1 failed `dbt_build` + `contract_conformance` (used `cust_id` for the whole history), attempt 2 `coalesce(customer_id, cust_id)` passed; oracle PASS; 7,451 fixer tokens, 0 retries. |
| 14 | Real-LLM F3 (120b) | PASS | 1 attempt; `regexp_replace(... )::numeric(12,2)` passed; oracle PASS; 2,919 fixer tokens, 0 retries. |
| 15 | Real-LLM F6 | PASS | 2 attempts on `openai/gpt-oss-20b`; attempt 1 was an invalid draft (`fixer_output`), attempt 2 used a `WITH ... row_number()` dedupe and passed; oracle PASS; 4,485 fixer tokens, 0 retries. |
| 16 | Lint / format / mypy | PASS | `ruff check .` and `ruff format --check .` clean; `mypy src` clean (89 source files). |
| 17 | Unit tests | PASS | **476 passed, 1 skipped** (`pytest -m "not integration"`; the skip is real OS symlink creation on this Windows account). |
| 18 | Integration tests (no LLM) | PASS | **87 passed in 398.33s** in the final full run after the NONE-routing guard (`pytest -m "integration and not llm"`). |
| 19 | Dev warehouse unchanged | PASS | 30 batches, max `batch_20260130`, 0 incidents, 0 `dq.agent_steps`, 0 `dq.fix_attempts`, no `dqs_sandbox_*` schemas. |
| 20 | No leftover temp databases/schemas | PASS | Each fixture drops its clone/template in `finally`; after all runs `pg_database` contains no `dqs_%` databases and there are no `dqs_sandbox_*` schemas. |

Real-LLM evidence files (git-ignored): `out/phase-5b-llm-F1_column_rename.json`,
`out/phase-5b-llm-F3_type_change.json`, `out/phase-5b-llm-F6_duplicate_rows.json`.

## Design choices and why

* **`FixPlanDraft` is a separate schema from `FixPlan`.** The model returns
  only the five prose/patch fields. Code supplies `incident_id`, `batch_id`,
  `category`, `affected_table` and `affected_columns`, so a model cannot
  relabel the incident or point the targeted check at another batch
  (DECISIONS 90).
* **The prompt sees only lineage-selected candidates.** This applies the
  verifier's relevance rule before planning and keeps the model away from
  tests, macros, contracts and unrelated models (DECISIONS 91).
* **One persistence path for every attempt.** Invalid drafts and real verifier
  verdicts share `next_attempt_number`/`persist_attempt_payload`; the attempt
  timeline cannot lose a round trip (DECISIONS 92).
* **No tool fallback for the fixer.** Native structured output only; the
  requirement is a no-tools reasoner. A provider that cannot satisfy the
  schema produces an honest failed attempt (DECISIONS 93).
* **Routing is a pure function.** PR eligibility is product policy, not a
  model judgement (DECISIONS 94). NONE is separated from ISSUE before the
  workflow exists.
* **The issue is a template.** It can only render stored facts and a static
  playbook; it cannot invent evidence or hide a failed attempt
  (DECISIONS 96).
* **The oracle is an answer key, not product code.** The verifier checks
  contract/rows/targeted criteria, but only the generator knows the clean
  values. The oracle lives under `tests/` and an isolation test enforces that
  boundary (DECISIONS 97).

## Concepts used

* **Structured output as a type boundary.** The model does not "write code" or
  call functions; it fills a Pydantic schema. Validation happens before any
  deterministic code sees the value, so a malformed answer becomes a failed
  attempt instead of a partially applied patch.
* **Bounded retry with evidence feedback.** Each retry gets the previous
  verifier verdict and the redacted dbt error. The loop is finite, and every
  round trip is persisted. This is the difference between an agent and a
  retry-forever prompt.
* **Lineage as a candidate filter.** The dbt manifest's `depends_on` edge from
  a staging model to the affected raw source is a deterministic relevance
  signal. It narrows the prompt and matches the verifier's allowlist.
* **Oracle-based evaluation.** The simulator generator is a pure function of
  `(seed, date)`. Regenerating the clean frame gives ground truth that the
  verifier itself cannot see. This catches "the check passed but the values are
  wrong", which contract/row/null checks alone cannot.
* **Policy vs. reasoning.** Routing and issue text are deterministic; only
  diagnosis, patch drafting and human-readable summaries use LLMs. This keeps
  the publication decision reproducible and auditable.
* **Typed failure artifacts.** `VerificationResult`, `FixAttempt` and
  `IssueDraft` make failure a first-class output, so exhaustion is reported
  with evidence rather than collapsed into a generic error.

## Deviations from the docs

* **F6 was rerun on `openai/gpt-oss-20b`** after the configured
  `openai/gpt-oss-120b` hit the provider's daily token limit (HTTP 429) during
  the F6 investigator/fixer calls. F1/F3 results are from the 120b run. The
  prompt and code did not change between runs. A later consistency rerun on
  20b passed F3/F6 but failed F1 after 3 attempts; that is recorded as model
  capability evidence below, not hidden.
* **No workflow graph or publishing was built.** `plan-fix` is a manual node
  command, as scoped for 5b.
* **The real-LLM test result files are split across two runs/models.** The
  test records `provider`, `model`, timestamps and every attempt in each JSON.

## Known limitations / TODO

* The fixer only considers staging models that directly select from the
  affected raw source. Mart-level or multi-hop fixes are out of scope; the
  route becomes ISSUE when no candidate exists.
* `dq.fix_attempts` still has no unique constraint on
  `(incident_id, attempt_no)`; sequential single-writer use is assumed.
* The oracle covers the `orders` staging model used by F1/F3/F6. Other tables
  and other fault kinds are not compared to clean generator frames yet.
* `dqs incident plan-fix` is deliberately not a workflow: it does not change
  incident status, build an issue, or publish anything.
* Native structured output is the only fixer channel. Providers that reject
  schema output or emit invalid tool calls produce failed attempts; the
  Phase-4b investigator has a submit-tool fallback, the fixer does not (by
  scope).
* The F1 20b consistency run exhausted 3 attempts with
  `dbt_build`/`contract_conformance` failures. The prompt was not tuned on
  that result; the 120b run passed in 2 attempts.
* The provider quota (200k tokens/day on this key) made repeated real-model
  runs fragile; future benchmark runs need either a higher tier or explicit
  quota accounting. No result was fabricated or copied from a fake provider.
* The issue builder is not yet wired to a CLI or publisher; Phase 6 will call
  it when routing chooses ISSUE or the fixer exhausts.
