# Phase 5a — Deterministic fix models, patching, sandbox verification and criteria

Date: 2026-10-20 · Phase 5 tasks 1 and 4 (the deterministic half; the fixer
agent and issue builder are 5b). Companion review packet:
`docs/review/phase-5a-review.md`. No LLM is used anywhere in this phase.

## What was built

* `src/dqs/models/fix.py` — strict `Edit`, `FixPlan`, `CriterionResult` and
  `VerificationResult` models. `FixPlan` carries the incident facts verification
  needs (`incident_id`, `batch_id`, `category`, `affected_table`,
  `affected_columns`) plus the §10.5 fields. Model-level limits: ≤3 edits,
  ≤4096 bytes of replacement text, no no-op edits, safe identifiers only.
* `src/dqs/verify/patch.py` — Windows-hardened allowlist, exact-match byte
  application, read-only manifest relevance rule, temp-project copy, sandbox
  schema naming and cleanup-name validation.
* `src/dqs/verify/criteria.py` — published-contract conformance (schema, type,
  null, unique, allowed values only), row preservation (95%, distinct-PK
  denominator for duplicates) and the category-specific targeted checks for
  `SOURCE_COLUMN_RENAMED`, `SOURCE_TYPE_CHANGED` and `DUPLICATE_RECORDS`.
* `src/dqs/verify/sandbox.py` — isolated temp dbt project with
  `--target-path`/`--log-path` inside the temp directory, minimal child
  environment, `shell=False`, timeout, redacted excerpt, always-drop cleanup,
  stale-schema cleanup, and `dq.fix_attempts` persistence.
* `src/dqs/cli.py` — `dqs verify <plan.json>` and `dqs verify cleanup`.
* `src/dqs/config.py` — `fix_verify_timeout_seconds` (default 300).
* `src/dqs/contracts/profiler.py` — staged profiling now accepts
  `dqs_sandbox_*` schemas (the dev `staging`/`marts` allowlist is unchanged).
* Tests — `tests/unit/test_fix_models.py`, `tests/unit/test_verify_patch.py`,
  `tests/unit/test_verify_sandbox.py`, `tests/unit/test_verify_criteria.py`,
  `tests/integration/test_verify_sandbox_db.py` (real dbt on faulted clones).

## How to run it

```powershell
# one-time environment
.venv\Scripts\python -m pip install -e ".[dev]"

# quality gates
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m mypy src\dqs
.venv\Scripts\python -m pytest -m "not integration" -q
.venv\Scripts\python -m pytest -m integration -q

# manual verification (plan JSON is a serialized FixPlan)
.venv\Scripts\dqs.exe verify path\to\plan.json
.venv\Scripts\dqs.exe verify cleanup
```

The real acceptance module seeds `dqs_verify_template` (30 raw days + one dbt
build), clones it per test, loads the fault day 2026-01-31 with `run_dbt=False`,
and drops every clone/sandbox afterwards.

## Acceptance results

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Good hand-written F1 (`coalesce(customer_id, cust_id)`) passes | PASS | `pytest ... -k "hand_written_good" -q` → **3 passed in 31.67s** (F1/F3/F6 together) |
| 2 | Good hand-written F3 (`regexp_replace` before cast) passes | PASS | same 3-test run; `targeted_check` F3 sees numeric values inside the raw-contract range |
| 3 | Good hand-written F6 (`row_number()` dedupe by PK, latest `_loaded_at`) passes | PASS | same 3-test run; distinct-PK row denominator and unique-PK targeted check both pass |
| 4 | `where false` / row deletion → row preservation | PASS | `test_bad_patch_that_deletes_rows_fails_row_preservation`; failure detail `0 staging rows ... minimum is 95%` |
| 5 | Renamed output column → contract conformance | PASS | `test_bad_patch_that_renames_an_output_column_fails_contract_conformance`; detail names missing `customer_id` and unexpected `customer_key`; the contract criterion is asserted |
| 6 | SQL syntax error → dbt failure with excerpt | PASS | `test_bad_patch_with_a_syntax_error_fails_dbt_build_with_an_excerpt`; non-zero `dbt_exit_code` and `dbt_log_excerpt` contains the compiler error |
| 7 | Plain filter hiding bad rows → row preservation or targeted check | PASS | `test_bad_filter_that_hides_bad_rows_fails_the_targeted_check`; the faulted batch has no staging rows, so `targeted_check` fails (the test asserts the targeted reason, not row preservation) |
| 8 | `schema.yml`, `../..`, outside `models/` → allowlist | PASS | `test_bad_paths_relevance_and_exact_match_are_rejected_before_dbt` (parameterized: `schema.yml`, `models/../macros/x.sql`, `macros/x.sql`) plus unit cases for absolute/UNC/drive/NUL/ADS/reserved names |
| 9 | Unrelated model → relevance rule | PASS | same parameterized test with `stg_products` vs `raw.orders`; detail says the model does not directly select from the affected table |
| 10 | `old` matches 0 or 2+ → exact-match error | PASS | same parameterized test (`found 0`, `found N`) |
| 11 | Oversized patch → size limit | PASS | `test_oversized_patch_is_rejected_by_the_model` → `ValidationError ... plan limit`; the plan cannot be constructed |
| 12 | Sandbox schemas dropped after a failing build | PASS | syntax-error test ends with `_assert_no_sandbox(engine) == []` |
| 13 | Sandbox schemas dropped after a killed run | PASS | `test_cleanup_happens_when_the_build_is_killed` simulates a schema created by a hung child, raises `TimeoutExpired`, then asserts no `dqs_sandbox_*` schema remains |
| 14 | Real `target/manifest.json` unchanged | PASS | `test_verification_does_not_touch_the_real_project_or_dev_layers` compares SHA-256 before/after a passing verification |
| 15 | Dev staging/marts row counts unchanged | PASS | same test compares every `staging.*` and `marts.*` table count before/after |
| 16 | No secrets in the dbt child env | PASS | `test_child_env_is_minimal_and_has_no_api_tokens`; polluted source with `LLM_API_KEY`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`GITHUB_TOKEN` yields none of those values, `PGDATABASE` comes from `Settings` |
| 17 | `dq.fix_attempts` persistence | PASS | good-case test reads `(attempt_no, passed) == (1, True)` for its incident |
| 18 | CLI `verify` and `verify cleanup` | PASS | `test_cli_verify_and_cleanup` invokes both through `CliRunner`; cleanup drops a seeded stale schema |
| 19 | Lint / mypy / tests | PASS | `ruff check .` all checks passed; `ruff format --check` clean (146 files); `mypy src\dqs` success on **88 files**; **404 unit passed, 1 skipped**; **83 integration passed, 3 skipped** (LLM-only), 349.44s |
| 20 | Dev warehouse unchanged | PASS | `dq.batches=30`, `dq.incidents=0`, `dq.agent_steps=0`, `dq.fix_attempts=0`, no `dqs_sandbox_*` schemas, no stray `dqs_%` databases |

Real command output summary:

```
tests/unit:        404 passed, 1 skipped, 86 deselected in 2.12s
tests/integration:  83 passed, 3 skipped, 405 deselected in 349.44s
good-patch subset:   3 passed in 31.67s
bad/edge subset:    15 passed in 46.42s
```

The one skipped unit test is the real OS symlink creation test: this Windows
session is not allowed to create symlinks without elevation. The same guard is
covered by a real directory **junction** test and by a monkeypatched
`Path.is_symlink` test that proves the component check (not just the resolved
destination check) rejects it.

## Design choices and why

DECISIONS 80–89 were added this phase. The main ones:

* Verification context is explicit in `FixPlan` (incident, batch, category,
  table, columns), not inferred from SQL.
* Limits and edit shape are enforced by the Pydantic model, so no code path can
  carry an over-limit plan.
* The allowlist is fail-closed for Windows forms and rejects reparse points.
* Relevance is a manifest graph query, never `dbt compile`; the real manifest is
  read-only during verification.
* The dbt child gets a temp copy, isolated target/log paths, `shell=False`, a
  timeout and a minimal environment.
* `staging.*+` replaces `<changed>+` because sibling staging models and their
  relationship tests are required for a self-contained build (DECISIONS 86).
* Contract conformance deliberately excludes volume/freshness/range; those are
  one-snapshot properties, not fixed rules a patch should preserve.
* A fix that deletes rows fails even if its SQL is valid, and duplicate fixes
  are measured against distinct raw primary keys, not raw row count.

## Concepts used

* **Isolation before execution.** A temp copy + a unique schema prefix means a
  verification run cannot write to the dev warehouse or overwrite the manifest
  lineage reads. Cleanup is part of the result, not an afterthought.
* **Exact-match patching is a safety property, not a convenience.** Requiring
  `old` to occur once makes a stale or ambiguous patch fail loudly instead of
  silently editing the wrong occurrence.
* **Contracts describe rules, not a snapshot.** Schema/type/null/unique/allowed
  values are durable expectations; volume, freshness and ranges depend on which
  rows/batches were profiled, so they are excluded from fix verification.
* **Row preservation is the anti-cheat denominator.** A valid but destructive
  `where false` patch would pass every schema check; comparing against raw rows
  (distinct PK for duplicates) rejects it.
* **Category-specific falsification.** Each root-cause category gets a SQL
  question that the original fault would answer "badly": nulls in the faulted
  batch, cast/range for a type change, uniqueness for duplicates.
* **Windows path canonicalization.** `resolve()` alone is not enough when
  separators, drive/UNC forms, short names, junctions and ADS are available;
  validate syntax, components and the resolved destination.

## Deviations from the docs

* §11 says `dbt build --select <changed>+`; this phase uses `staging.*+` because
  the narrower selector leaves sibling staging relations (and their tests)
  missing in the sandbox. Logged as DECISIONS 86.
* §10.5 lists only `summary/edits/rationale/expected_effect/risk`; the model has
  five additional incident-context fields. Logged as DECISIONS 80.
* The CLI is a single `dqs verify` command that recognizes the literal
  `cleanup` argument, because Typer consumes a group's positional argument
  before resolving a subcommand. Logged as DECISIONS 89.
* No fixer agent, issue builder or `plan-fix` command was added; those are 5b.

## Known limitations / TODO

* The real symlink-creation test is skipped on this Windows account; junction
  and simulated-symlink coverage pass. Re-run on a Developer-Mode/elevated
  machine to exercise the literal symlink path.
* `dq.fix_attempts` has no unique constraint on `(incident_id, attempt_no)`.
  Sequential single-writer use is safe; concurrent verifications of the same
  incident can race. Sandbox schema names are incident-derived, so concurrent
  runs for the same incident would also collide; `verify cleanup` is a manual
  recovery tool, not a concurrency primitive.
* Contract conformance is checked for the changed staging table only. Edits are
  restricted to staging, but a semantic change that breaks a downstream mart's
  contract is not separately checked yet.
* Exact-match strings are byte-sensitive: a plan copied from a CRLF file must
  use the same line endings. The dbt project is LF-normalized.
* The relevance rule trusts the manifest's direct `depends_on` source edge; it
  does not parse SQL to prove the source is in the `FROM` clause. A stale
  manifest can therefore approve a model shape that no longer matches the file.
* If `target/manifest.json` is absent, verification fails the relevance
  criterion rather than regenerating it (deliberate: regeneration can tear the
  manifest).
